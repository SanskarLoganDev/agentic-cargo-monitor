"""
Service A — Bootstrap Seed Script

Runs ONCE to populate Firestore with the three fixed pharmaceutical shipment
documents that the rest of the system monitors. After this script succeeds,
Service A's job is done and never needs to run again for this project.

What it does:
  1. Reads three drug label PDFs from the pdfs/ directory
  2. Extracts text from each with pypdf
  3. Calls Claude API to extract structured monitoring thresholds
  4. Validates the result with Pydantic (ShipmentSchema)
  5. Writes one Firestore document per drug to /shipments/{drug_id}

Usage:
  cd services/service_a
  python seed.py

Prerequisites:
  - ANTHROPIC_API_KEY set in .env
  - GOOGLE_APPLICATION_CREDENTIALS set in .env (path to your GCP service account JSON)
  - GOOGLE_CLOUD_PROJECT set in .env
  - Three PDFs downloaded into pdfs/ (see README for download links)
  - Firestore already provisioned (terraform apply must have been run)
"""

import json
import logging
import os
import sys
from pathlib import Path

import pypdf
from dotenv import load_dotenv
from google.cloud import firestore

from agents.intake_agent import IntakeAgent, MAX_RETRIES

# ---------------------------------------------------------------------------
# Load environment variables
# ---------------------------------------------------------------------------
load_dotenv()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Fixed shipment configuration
# These IDs are used across ALL services — do not change them.
# Service C, D, and E all reference these exact document IDs.
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
        "drug_id":  "herceptin-001",
        "pdf_file": "herceptin-trastuzumab.pdf",
        "label":    "Herceptin (trastuzumab)",
    },
]

PDFS_DIR = Path(__file__).parent / "pdfs"
FIRESTORE_COLLECTION = "shipments"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def extract_pdf_text(pdf_path: Path) -> str:
    """Extract all text from a PDF using pypdf."""
    reader = pypdf.PdfReader(str(pdf_path))
    pages = []
    for page in reader.pages:
        text = page.extract_text()
        if text:
            pages.append(text)
    full_text = "\n\n".join(pages)

    if len(full_text.strip()) < 50:
        raise ValueError(
            f"'{pdf_path.name}' produced too little text. "
            "Ensure it is a digitally-created PDF, not a scanned image."
        )

    logger.info(
        "Extracted %d characters from '%s' (%d pages)",
        len(full_text),
        pdf_path.name,
        len(reader.pages),
    )
    return full_text


def run_extraction_with_retry(agent: IntakeAgent, text: str, filename: str):
    """
    Attempt extraction up to MAX_RETRIES times.
    On failure, passes the error back to Claude as correction context.
    """
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
            logger.warning(
                "Attempt %d/%d failed for '%s': %s",
                attempt, MAX_RETRIES, filename, exc,
            )

    raise RuntimeError(
        f"Extraction failed after {MAX_RETRIES} attempts for '{filename}'. "
        f"Last error: {last_error}"
    )


def check_prerequisites() -> None:
    """Fail fast with a clear message if environment or files are missing."""
    errors = []

    if not os.getenv("ANTHROPIC_API_KEY"):
        errors.append("ANTHROPIC_API_KEY is not set in .env")

    if not os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
        errors.append(
            "GOOGLE_APPLICATION_CREDENTIALS is not set in .env "
            "(path to your GCP service account JSON key file)"
        )
    else:
        cred_path = Path(os.getenv("GOOGLE_APPLICATION_CREDENTIALS"))
        if not cred_path.exists():
            errors.append(
                f"Credentials file not found: {cred_path}. "
                "Download it from GCP Console → IAM → Service Accounts."
            )

    if not os.getenv("GOOGLE_CLOUD_PROJECT"):
        errors.append("GOOGLE_CLOUD_PROJECT is not set in .env")

    for shipment in SHIPMENTS:
        pdf_path = PDFS_DIR / shipment["pdf_file"]
        if not pdf_path.exists():
            errors.append(
                f"PDF not found: {pdf_path}. "
                "Download it using the links in the README and place it in pdfs/."
            )

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

    # Initialise clients
    agent = IntakeAgent()
    db = firestore.Client(project=os.getenv("GOOGLE_CLOUD_PROJECT"))

    results = {"success": [], "failed": []}

    for shipment in SHIPMENTS:
        drug_id  = shipment["drug_id"]
        pdf_file = shipment["pdf_file"]
        label    = shipment["label"]
        pdf_path = PDFS_DIR / pdf_file

        logger.info("-" * 40)
        logger.info("Processing: %s (%s)", label, drug_id)

        try:
            # Step 1 — extract PDF text
            raw_text = extract_pdf_text(pdf_path)

            # Step 2 — Claude extraction with retry
            schema = run_extraction_with_retry(agent, raw_text, pdf_file)

            # Step 3 — build Firestore document
            doc_data = schema.to_firestore_dict()

            # Add metadata fields that are not in the schema
            doc_data["drug_id"]    = drug_id
            doc_data["pdf_source"] = pdf_file
            doc_data["seeded_at"]  = firestore.SERVER_TIMESTAMP
            doc_data["status"]     = "active"

            # Step 4 — write to Firestore (merge=False so it fully overwrites)
            doc_ref = db.collection(FIRESTORE_COLLECTION).document(drug_id)
            doc_ref.set(doc_data)

            logger.info(
                "Written to Firestore: /%s/%s — %s",
                FIRESTORE_COLLECTION,
                drug_id,
                schema.drug_name,
            )
            logger.info(
                "  temp range : %.1f°C to %.1f°C",
                schema.temp_min_celsius,
                schema.temp_max_celsius,
            )
            logger.info(
                "  excursion  : %d minutes max",
                schema.max_excursion_duration_minutes,
            )
            logger.info(
                "  do_not_freeze=%s  light_sensitive=%s  shake_sensitive=%s",
                schema.do_not_freeze,
                schema.light_sensitive,
                schema.shake_sensitive,
            )

            results["success"].append(drug_id)

        except Exception as exc:
            logger.error("FAILED for %s: %s", drug_id, exc)
            results["failed"].append({"drug_id": drug_id, "error": str(exc)})

    # ---------------------------------------------------------------------------
    # Summary
    # ---------------------------------------------------------------------------
    logger.info("=" * 60)
    logger.info(
        "Seeding complete — %d succeeded, %d failed",
        len(results["success"]),
        len(results["failed"]),
    )

    if results["success"]:
        logger.info("Firestore documents written:")
        for drug_id in results["success"]:
            logger.info("  /%s/%s", FIRESTORE_COLLECTION, drug_id)

    if results["failed"]:
        logger.error("Failed shipments:")
        for item in results["failed"]:
            logger.error("  %s: %s", item["drug_id"], item["error"])
        sys.exit(1)

    logger.info("=" * 60)
    logger.info("Service A is done. The following Firestore documents are ready:")
    logger.info("  /shipments/pfizer-001")
    logger.info("  /shipments/moderna-001")
    logger.info("  /shipments/herceptin-001")
    logger.info("Services B, C, D, E will read from these documents.")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
