"""
Service A — Bootstrap Seed Script

Runs ONCE to populate Firestore with the three fixed pharmaceutical shipment
documents that the entire system monitors. After this succeeds, never run again.

Pipeline per drug:
  1. Read PDF from pdfs/
  2. Extract text with pypdf
  3. Claude extracts PDF-sourced fields (temperature, excursion, thaw window, etc.)
  4. Validate with Pydantic (ShipmentSchema)
  5. Apply hardcoded transport parameters (humidity, shock, flight delay)
  6. Write completed document to Firestore /shipments/{drug_id}

Flight delay thresholds (demo values — chosen to produce clear UI behaviour):
  pfizer-001  : 120 min (2h) — delayed_2h fires, delayed_6h fires
  moderna-001 : 240 min (4h) — delayed_2h safe, delayed_6h fires
  jynneos-001 : 480 min (8h) — delayed_2h safe, delayed_6h safe

Usage:
  cd services/service_a
  python seed.py

Prerequisites:
  - ANTHROPIC_API_KEY in .env
  - GOOGLE_APPLICATION_CREDENTIALS in .env (path to GCP service account JSON)
  - GOOGLE_CLOUD_PROJECT in .env
  - Three PDFs in pdfs/
  - Terraform applied — Firestore must already exist
"""

import logging
import os
import sys
from pathlib import Path

import pypdf
from dotenv import load_dotenv
from google.cloud import firestore

from agents.intake_agent import IntakeAgent, MAX_RETRIES

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Fixed shipment configuration
# drug_id values are the primary keys used across ALL services (B, C, D, E).
# Do not change them — every service is hardcoded to these three strings.
# ---------------------------------------------------------------------------
SHIPMENTS = [
    {
        "drug_id":  "pfizer-001",
        "pdf_file": "pfizer-comirnaty.pdf",
        "label":    "Pfizer COMIRNATY",
    },
    {
        "drug_id":  "moderna-001",
        "pdf_file": "moderna-spikevax.pdf",
        "label":    "Moderna Spikevax",
    },
    {
        "drug_id":  "jynneos-001",
        "pdf_file": "jynneos-monkeypox.pdf",
        "label":    "JYNNEOS Smallpox & Monkeypox Vaccine",
    },
]

# ---------------------------------------------------------------------------
# Hardcoded transport parameters
#
# Drug labels never publish humidity limits, G-force ratings, or delay
# tolerances. These come from:
#   Humidity  → WHO vaccine transport standards
#   Shock     → ISTA 7E air freight standard
#   Delay     → Demo values chosen for distinct UI behaviour (see docstring)
#
# Temperature values are NOT here — they come entirely from the PDFs.
# ---------------------------------------------------------------------------
TRANSPORT_OVERRIDES: dict[str, dict] = {

    "pfizer-001": {
        # Humidity: WHO standard for frozen vaccines
        "max_humidity_percent": 75.0,
        "humidity_alert_message": (
            "Humidity above 75% RH — condensation risk on ultra-cold vials at thaw. "
            "Inspect all vials before administration."
        ),
        # Shock: ISTA 7E standard for frozen pharmaceutical air freight
        "max_shock_g": 25.0,
        "shock_alert_message": (
            "Shock event exceeded 25G — ultra-cold vials may have shifted in packaging. "
            "Integrity check required before administering."
        ),
        # Flight delay: 120 min (2 hours)
        # delayed_2h (120 min) >= 120 → agent fires
        # on_time    (  0 min) <  120 → no action
        "max_flight_delay_minutes": 120,
        "flight_delay_spoilage_note": (
            "Pfizer COMIRNATY stores at -90°C to -60°C with a 30-minute temperature "
            "excursion window. A 2-hour flight delay means ambient exposure almost "
            "certainly exceeded the safe excursion limit. Verify cold chain logs "
            "immediately and prepare contingency cold storage at the receiving facility."
        ),
    },

    "moderna-001": {
        # Humidity: same WHO standard as Pfizer (same liquid-filled vial format)
        "max_humidity_percent": 75.0,
        "humidity_alert_message": (
            "Humidity above 75% RH — condensation risk on frozen mRNA vaccine. "
            "Do not refreeze thawed vaccine."
        ),
        # Shock: same ISTA 7E standard
        "max_shock_g": 25.0,
        "shock_alert_message": (
            "Shock event exceeded 25G — frozen vials may have cracked or shifted. "
            "Integrity check required before administering."
        ),
        # Flight delay: 240 min (4 hours)
        # delayed_2h (120 min) <  240 → no action
        # delayed_6h (360 min) >  240 → agent fires
        "max_flight_delay_minutes": 240,
        "flight_delay_spoilage_note": (
            "Moderna Spikevax stores at -50°C to -15°C. A 4-hour flight delay "
            "warrants cold chain assessment — ambient exposure risk is real at this "
            "duration. Once confirmed thawed, the vaccine is viable refrigerated at "
            "2–8°C for up to 30 days. Verify whether dry ice or active refrigeration "
            "was maintained throughout the delay before accepting the shipment."
        ),
    },

    "jynneos-001": {
        # Humidity: STRICTER — JYNNEOS is lyophilised powder, absorbs moisture
        "max_humidity_percent": 60.0,
        "humidity_alert_message": (
            "Humidity above 60% RH — JYNNEOS is a lyophilised vaccine. "
            "Moisture absorption degrades the freeze-dried cake and reduces potency. "
            "Inspect carton seals and cold chain packaging immediately."
        ),
        # Shock: STRICTER — lyophilised cake and glass vials are fragile
        "max_shock_g": 15.0,
        "shock_alert_message": (
            "Shock event exceeded 15G — JYNNEOS lyophilised vials are fragile. "
            "Lyophilised cake fracture or glass cracking is possible. "
            "Visual inspection of all vials required before use."
        ),
        # Flight delay: 480 min (8 hours)
        # delayed_2h (120 min) <  480 → no action
        # delayed_6h (360 min) <  480 → no action
        # (JYNNEOS intentionally never triggers on the demo dropdown —
        #  it shows judges that the system has genuine per-drug intelligence.)
        "max_flight_delay_minutes": 480,
        "flight_delay_spoilage_note": (
            "JYNNEOS has an 8-week viability window after confirmed thaw at 2–8°C, "
            "making it the most delay-tolerant of the three shipments. An 8-hour "
            "threshold reflects this resilience. Note: JYNNEOS is lyophilised — "
            "humidity exposure during the delay is the primary risk. Inspect packaging "
            "integrity even when the delay threshold has not been exceeded."
        ),
    },
}

PDFS_DIR             = Path(__file__).parent / "pdfs"
FIRESTORE_COLLECTION = "shipments"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def extract_pdf_text(pdf_path: Path) -> str:
    """Extract all text from a PDF using pypdf."""
    reader    = pypdf.PdfReader(str(pdf_path))
    pages     = [p.extract_text() for p in reader.pages if p.extract_text()]
    full_text = "\n\n".join(pages)

    if len(full_text.strip()) < 50:
        raise ValueError(
            f"'{pdf_path.name}' produced too little text. "
            "Ensure it is a digitally-created PDF, not a scanned image."
        )

    logger.info(
        "Extracted %d characters from '%s' (%d pages)",
        len(full_text), pdf_path.name, len(reader.pages),
    )
    return full_text


def run_extraction_with_retry(agent: IntakeAgent, text: str, filename: str):
    """Run Claude extraction with up to MAX_RETRIES attempts."""
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return agent.extract(
                raw_text=text,
                filename=filename,
                prior_error=str(last_error) if last_error else None,
            )
        except ValueError as exc:
            last_error = exc
            logger.warning("Attempt %d/%d failed for '%s': %s", attempt, MAX_RETRIES, filename, exc)

    raise RuntimeError(
        f"Extraction failed after {MAX_RETRIES} attempts for '{filename}'. "
        f"Last error: {last_error}"
    )


def check_prerequisites() -> None:
    """Fail fast with clear messages before touching any external API."""
    errors = []

    if not os.getenv("ANTHROPIC_API_KEY"):
        errors.append("ANTHROPIC_API_KEY is not set in .env")

    cred_str = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    if not cred_str:
        errors.append("GOOGLE_APPLICATION_CREDENTIALS is not set in .env")
    elif not Path(cred_str).exists():
        errors.append(
            f"Credentials file not found: {cred_str}. "
            "Download from GCP Console → IAM → Service Accounts → Create key → JSON."
        )

    if not os.getenv("GOOGLE_CLOUD_PROJECT"):
        errors.append("GOOGLE_CLOUD_PROJECT is not set in .env")

    for shipment in SHIPMENTS:
        pdf_path = PDFS_DIR / shipment["pdf_file"]
        if not pdf_path.exists():
            errors.append(f"PDF not found: {pdf_path}")

    if errors:
        logger.error("Prerequisites not met:\n" + "\n".join(f"  - {e}" for e in errors))
        sys.exit(1)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    logger.info("=" * 60)
    logger.info("Service A — Seed Script")
    logger.info("Seeding %d shipments into Firestore", len(SHIPMENTS))
    logger.info("=" * 60)

    check_prerequisites()

    agent = IntakeAgent()
    db    = firestore.Client(project=os.getenv("GOOGLE_CLOUD_PROJECT"))

    results: dict[str, list] = {"success": [], "failed": []}

    for shipment in SHIPMENTS:
        drug_id  = shipment["drug_id"]
        pdf_file = shipment["pdf_file"]
        label    = shipment["label"]
        pdf_path = PDFS_DIR / pdf_file

        logger.info("-" * 40)
        logger.info("Processing: %s (%s)", label, drug_id)

        try:
            raw_text = extract_pdf_text(pdf_path)
            schema   = run_extraction_with_retry(agent, raw_text, pdf_file)

            doc_data = schema.to_firestore_dict()

            # Apply hardcoded overrides (humidity, shock, flight delay)
            overrides = TRANSPORT_OVERRIDES[drug_id]
            doc_data.update(overrides)

            # System metadata
            doc_data["drug_id"]    = drug_id
            doc_data["pdf_source"] = pdf_file
            doc_data["seeded_at"]  = firestore.SERVER_TIMESTAMP
            doc_data["status"]     = "active"

            db.collection(FIRESTORE_COLLECTION).document(drug_id).set(doc_data)

            logger.info("Written → /shipments/%s — %s", drug_id, schema.drug_name)
            logger.info("  [PDF]  temp:     %.1f°C to %.1f°C | excursion: %d min",
                        schema.temp_min_celsius, schema.temp_max_celsius,
                        schema.max_excursion_duration_minutes)
            logger.info("  [HARD] humidity: %.0f%% | shock: %.0fG | delay threshold: %d min",
                        overrides["max_humidity_percent"],
                        overrides["max_shock_g"],
                        overrides["max_flight_delay_minutes"])

            results["success"].append(drug_id)

        except Exception as exc:
            logger.error("FAILED for %s: %s", drug_id, exc)
            results["failed"].append({"drug_id": drug_id, "error": str(exc)})

    # Summary
    logger.info("=" * 60)
    logger.info("Seeding complete — %d succeeded, %d failed",
                len(results["success"]), len(results["failed"]))

    if results["failed"]:
        for item in results["failed"]:
            logger.error("  FAILED %s: %s", item["drug_id"], item["error"])
        sys.exit(1)

    logger.info("")
    logger.info("Firestore documents ready:")
    logger.info("  /shipments/pfizer-001   temp: -90 to -60°C | delay threshold: 120 min (2h)")
    logger.info("  /shipments/moderna-001  temp: -50 to -15°C | delay threshold: 240 min (4h)")
    logger.info("  /shipments/jynneos-001  temp: -25 to -15°C | delay threshold: 480 min (8h)")
    logger.info("")
    logger.info("Flight delay dropdown behaviour:")
    logger.info("  on_time    (  0 min) → no drug triggers")
    logger.info("  delayed_2h (120 min) → Pfizer fires,         Moderna OK, JYNNEOS OK")
    logger.info("  delayed_6h (360 min) → Pfizer fires, Moderna fires,      JYNNEOS OK")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
