"""
Service A — Bootstrap Seed Script

Runs ONCE to populate Firestore with the three fixed pharmaceutical shipment
documents that the entire system monitors. After this succeeds, never run again.

Pipeline per drug:
  1. Read PDF from pdfs/
  2. Extract text with pypdf
  3. Claude extracts PDF-sourced fields (temperature, excursion, thaw window, etc.)
  4. Validate with Pydantic (ShipmentSchema)
  5. Apply hardcoded transport parameters (humidity, shock, flight delay, contacts)
  6. Write completed document to Firestore /shipments/{drug_id}

Authentication:
  Uses service account impersonation — no JSON key file required.
  Your personal gcloud credentials (application default) impersonate
  the service-a-seed SA. This is required because the org policy
  iam.disableServiceAccountKeyCreation blocks JSON key creation.

  Prerequisites:
    1. gcloud auth application-default login
    2. gcloud iam service-accounts add-iam-policy-binding \\
         service-a-seed@PROJECT.iam.gserviceaccount.com \\
         --member="user:YOU@gmail.com" \\
         --role="roles/iam.serviceAccountTokenCreator"

Flight delay thresholds (demo values):
  pfizer-001  : 120 min (2h) — delayed_2h fires, delayed_6h fires
  moderna-001 : 240 min (4h) — delayed_2h safe, delayed_6h fires
  jynneos-001 : 480 min (8h) — delayed_2h safe, delayed_6h safe

Usage:
  cd services/service_a
  python seed.py

Prerequisites (.env):
  ANTHROPIC_API_KEY
  SERVICE_ACCOUNT_EMAIL
  GOOGLE_CLOUD_PROJECT
  FIRESTORE_DATABASE
"""

import logging
import os
import sys
from pathlib import Path

import pypdf
from dotenv import load_dotenv
from google.auth import impersonated_credentials, default as google_auth_default
from google.cloud import firestore

from agents.intake_agent import IntakeAgent, MAX_RETRIES

# Explicitly load .env from repo root regardless of where seed.py is run from
_repo_root = Path(__file__).resolve().parent.parent.parent
load_dotenv(dotenv_path=_repo_root / ".env")

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
# Drug labels never publish humidity limits, G-force ratings, delay tolerances,
# or contact details. All of these are set here per shipment.
#
# contact_email / contact_phone — placeholder demo values.
# Service E will read these from Firestore to send breach notifications.
# Replace with real operator contacts before production use.
# ---------------------------------------------------------------------------
TRANSPORT_OVERRIDES: dict[str, dict] = {

    "pfizer-001": {
        "max_humidity_percent": 75.0,
        "humidity_alert_message": (
            "Humidity above 75% RH — condensation risk on ultra-cold vials at thaw. "
            "Inspect all vials before administration."
        ),
        "max_shock_g": 25.0,
        "shock_alert_message": (
            "Shock event exceeded 25G — ultra-cold vials may have shifted in packaging. "
            "Integrity check required before administering."
        ),
        "max_flight_delay_minutes": 120,
        "flight_delay_spoilage_note": (
            "Pfizer COMIRNATY stores at -90°C to -60°C with a 30-minute temperature "
            "excursion window. A 2-hour flight delay means ambient exposure almost "
            "certainly exceeded the safe excursion limit. Verify cold chain logs "
            "immediately and prepare contingency cold storage at the receiving facility."
        ),
        # Notification contacts — consumed by Service E for breach alerts
        "contact_email": "rohinv@umd.edu",
        "contact_phone": "+12408798960",
    },

    "moderna-001": {
        "max_humidity_percent": 75.0,
        "humidity_alert_message": (
            "Humidity above 75% RH — condensation risk on frozen mRNA vaccine. "
            "Do not refreeze thawed vaccine."
        ),
        "max_shock_g": 25.0,
        "shock_alert_message": (
            "Shock event exceeded 25G — frozen vials may have cracked or shifted. "
            "Integrity check required before administering."
        ),
        "max_flight_delay_minutes": 240,
        "flight_delay_spoilage_note": (
            "Moderna Spikevax stores at -50°C to -15°C. A 4-hour flight delay "
            "warrants cold chain assessment — ambient exposure risk is real at this "
            "duration. Once confirmed thawed, the vaccine is viable refrigerated at "
            "2-8°C for up to 30 days. Verify whether dry ice or active refrigeration "
            "was maintained throughout the delay before accepting the shipment."
        ),
        # Notification contacts — consumed by Service E for breach alerts
        "contact_email": "dan0003@umd.edu",
        "contact_phone": "+12404137654",
    },

    "jynneos-001": {
        "max_humidity_percent": 60.0,
        "humidity_alert_message": (
            "Humidity above 60% RH — JYNNEOS is a lyophilised vaccine. "
            "Moisture absorption degrades the freeze-dried cake and reduces potency. "
            "Inspect carton seals and cold chain packaging immediately."
        ),
        "max_shock_g": 15.0,
        "shock_alert_message": (
            "Shock event exceeded 15G — JYNNEOS lyophilised vials are fragile. "
            "Lyophilised cake fracture or glass cracking is possible. "
            "Visual inspection of all vials required before use."
        ),
        "max_flight_delay_minutes": 480,
        "flight_delay_spoilage_note": (
            "JYNNEOS has an 8-week viability window after confirmed thaw at 2-8°C, "
            "making it the most delay-tolerant of the three shipments. An 8-hour "
            "threshold reflects this resilience. Note: JYNNEOS is lyophilised — "
            "humidity exposure during the delay is the primary risk. Inspect packaging "
            "integrity even when the delay threshold has not been exceeded."
        ),
        # Notification contacts — consumed by Service E for breach alerts
        "contact_email": "sumi0309@umd.edu",
        "contact_phone": "+12027601163",
    },
}

PDFS_DIR             = Path(__file__).parent / "pdfs"
FIRESTORE_COLLECTION = "shipments"

# Firestore scopes required for impersonated credentials
FIRESTORE_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def build_impersonated_credentials():
    """
    Build impersonated credentials using the local gcloud application default
    credentials as the source, targeting the service-a-seed SA.

    Requires:
      - gcloud auth application-default login has been run
      - The caller has roles/iam.serviceAccountTokenCreator on the target SA
    """
    sa_email = os.getenv("SERVICE_ACCOUNT_EMAIL")

    # Load gcloud application default credentials as the source identity
    source_credentials, _ = google_auth_default(scopes=FIRESTORE_SCOPES)

    # Impersonate the service account
    target_credentials = impersonated_credentials.Credentials(
        source_credentials=source_credentials,
        target_principal=sa_email,
        target_scopes=FIRESTORE_SCOPES,
        lifetime=3600,  # 1 hour — more than enough for seed.py
    )

    logger.info("Impersonating service account: %s", sa_email)
    return target_credentials


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

    if not os.getenv("SERVICE_ACCOUNT_EMAIL"):
        errors.append("SERVICE_ACCOUNT_EMAIL is not set in .env")

    if not os.getenv("GOOGLE_CLOUD_PROJECT"):
        errors.append("GOOGLE_CLOUD_PROJECT is not set in .env")

    if not os.getenv("FIRESTORE_DATABASE"):
        errors.append("FIRESTORE_DATABASE is not set in .env")

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

    # Build impersonated credentials and connect to Firestore
    credentials = build_impersonated_credentials()
    db = firestore.Client(
        project=os.getenv("GOOGLE_CLOUD_PROJECT"),
        database=os.getenv("FIRESTORE_DATABASE"),
        credentials=credentials,
    )

    logger.info(
        "Connected to Firestore project=%s database=%s",
        os.getenv("GOOGLE_CLOUD_PROJECT"),
        os.getenv("FIRESTORE_DATABASE"),
    )

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

            overrides = TRANSPORT_OVERRIDES[drug_id]
            doc_data.update(overrides)

            doc_data["drug_id"]    = drug_id
            doc_data["pdf_source"] = pdf_file
            doc_data["seeded_at"]  = firestore.SERVER_TIMESTAMP
            doc_data["status"]     = "active"

            db.collection(FIRESTORE_COLLECTION).document(drug_id).set(doc_data)

            logger.info("Written → /shipments/%s — %s", drug_id, schema.drug_name)
            logger.info("  [PDF]  temp:     %.1f°C to %.1f°C | excursion: %d min",
                        schema.temp_min_celsius, schema.temp_max_celsius,
                        schema.max_excursion_duration_minutes)
            logger.info("  [HARD] humidity: %.0f%% | shock: %.0fG | delay: %d min | "
                        "contact: %s",
                        overrides["max_humidity_percent"],
                        overrides["max_shock_g"],
                        overrides["max_flight_delay_minutes"],
                        overrides["contact_email"])

            results["success"].append(drug_id)

        except Exception as exc:
            logger.error("FAILED for %s: %s", drug_id, exc)
            results["failed"].append({"drug_id": drug_id, "error": str(exc)})

    logger.info("=" * 60)
    logger.info("Seeding complete — %d succeeded, %d failed",
                len(results["success"]), len(results["failed"]))

    if results["failed"]:
        for item in results["failed"]:
            logger.error("  FAILED %s: %s", item["drug_id"], item["error"])
        sys.exit(1)

    logger.info("")
    logger.info("Firestore documents ready:")
    logger.info("  /shipments/pfizer-001   temp: -90 to -60°C | delay: 120 min (2h)")
    logger.info("  /shipments/moderna-001  temp: -50 to -15°C | delay: 240 min (4h)")
    logger.info("  /shipments/jynneos-001  temp: -25 to -15°C | delay: 480 min (8h)")
    logger.info("")
    logger.info("Flight delay dropdown behaviour:")
    logger.info("  on_time    (  0 min) → no drug triggers")
    logger.info("  delayed_2h (120 min) → Pfizer fires,         Moderna OK, JYNNEOS OK")
    logger.info("  delayed_6h (360 min) → Pfizer fires, Moderna fires,      JYNNEOS OK")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
