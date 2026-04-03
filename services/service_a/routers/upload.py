"""
Upload router — Service A's single HTTP endpoint.

POST /upload/pdf
  - Accepts a pharmaceutical drug label PDF (multipart/form-data)
  - Validates the file
  - Saves it to /uploads/
  - Extracts text with pypdf
  - Calls Claude to extract structured data
  - Validates with Pydantic
  - Persists to SQLite
  - Publishes shipment.created event to internal queue
  - Returns the full ShipmentRecord to the frontend
"""

import logging
import os
import uuid
from datetime import datetime
from pathlib import Path

import pypdf
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from sqlalchemy.orm import Session

from db.database import get_db
from db.models import Shipment
from schemas.shipment import ShipmentRecord
import events

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/upload", tags=["Service A — Intake"])

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

UPLOADS_DIR = Path(__file__).parent.parent / "uploads"
UPLOADS_DIR.mkdir(exist_ok=True)

MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB
ALLOWED_CONTENT_TYPES = {"application/pdf", "application/x-pdf"}
MAX_RETRIES = 2


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.post(
    "/pdf",
    response_model=ShipmentRecord,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a pharmaceutical drug label PDF for AI extraction",
    description=(
        "Upload a PDF of a pharmaceutical prescribing information document, drug label, "
        "or CDC storage-and-handling summary. The AI agent will extract all shipment "
        "monitoring parameters (temperature range, excursion limits, sensitivity flags) "
        "and return a structured shipment record ready for the monitoring dashboard."
    ),
)
async def upload_pdf(
    request: Request,
    file: UploadFile = File(..., description="Pharmaceutical drug label PDF"),
    db: Session = Depends(get_db),
):
    """
    Full pipeline: receive → validate → save → extract → persist → publish → return.
    """

    # ------------------------------------------------------------------
    # 1. File validation
    # ------------------------------------------------------------------
    _validate_file(file)

    # ------------------------------------------------------------------
    # 2. Read file bytes
    # ------------------------------------------------------------------
    file_bytes = await file.read()

    if len(file_bytes) > MAX_FILE_SIZE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File exceeds maximum allowed size of {MAX_FILE_SIZE_BYTES // (1024*1024)} MB.",
        )

    # ------------------------------------------------------------------
    # 3. Save to /uploads/ with unique filename
    # ------------------------------------------------------------------
    shipment_id = str(uuid.uuid4())
    safe_original = Path(file.filename).name  # strip any path components
    saved_filename = f"{shipment_id}_{safe_original}"
    saved_path = UPLOADS_DIR / saved_filename

    saved_path.write_bytes(file_bytes)
    logger.info("PDF saved: %s (%d bytes)", saved_path, len(file_bytes))

    # ------------------------------------------------------------------
    # 4. Extract raw text with pypdf
    # ------------------------------------------------------------------
    raw_text = _extract_pdf_text(saved_path, file.filename)

    # ------------------------------------------------------------------
    # 5. Claude extraction with retry
    # ------------------------------------------------------------------
    intake_agent = request.app.state.intake_agent
    shipment_schema = _run_extraction_with_retry(
        intake_agent, raw_text, file.filename
    )

    # ------------------------------------------------------------------
    # 6. Persist to SQLite
    # ------------------------------------------------------------------
    db_shipment = _save_to_db(
        db=db,
        shipment_id=shipment_id,
        schema=shipment_schema,
        saved_filename=saved_filename,
        saved_path=str(saved_path),
    )

    # ------------------------------------------------------------------
    # 7. Publish event to internal queue (picked up by Services B/C/D)
    # ------------------------------------------------------------------
    await events.publish(
        event_type="shipment.created",
        payload={
            "shipment_id": shipment_id,
            "drug_name": db_shipment.drug_name,
            "temp_min_celsius": db_shipment.temp_min_celsius,
            "temp_max_celsius": db_shipment.temp_max_celsius,
            "max_excursion_duration_minutes": db_shipment.max_excursion_duration_minutes,
            "do_not_freeze": db_shipment.do_not_freeze,
            "shake_sensitive": db_shipment.shake_sensitive,
        },
    )

    # ------------------------------------------------------------------
    # 8. Return full record to frontend
    # ------------------------------------------------------------------
    return ShipmentRecord(
        **{c.name: getattr(db_shipment, c.name) for c in db_shipment.__table__.columns}
    )


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _validate_file(file: UploadFile) -> None:
    """Raise HTTP 422 if the file is not a valid PDF upload."""
    if not file.filename:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No filename provided.",
        )

    suffix = Path(file.filename).suffix.lower()
    if suffix != ".pdf":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Only PDF files are accepted. Received file with extension '{suffix}'.",
        )

    # content_type check (browsers set this, but it can be spoofed — just a first check)
    if file.content_type and file.content_type not in ALLOWED_CONTENT_TYPES:
        logger.warning(
            "Unexpected content type '%s' for file '%s' — proceeding anyway",
            file.content_type,
            file.filename,
        )


def _extract_pdf_text(pdf_path: Path, original_filename: str) -> str:
    """Extract all text from the PDF using pypdf."""
    try:
        reader = pypdf.PdfReader(str(pdf_path))
        pages_text = []
        for i, page in enumerate(reader.pages):
            text = page.extract_text()
            if text:
                pages_text.append(text)

        full_text = "\n\n".join(pages_text)

        if len(full_text.strip()) < 50:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"'{original_filename}' appears to be a scanned image PDF — "
                    "pypdf could not extract readable text. "
                    "Please upload a text-based PDF (digitally created, not scanned)."
                ),
            )

        logger.info(
            "Extracted %d characters from '%s' (%d pages)",
            len(full_text),
            original_filename,
            len(reader.pages),
        )
        return full_text

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Could not read PDF '{original_filename}': {exc}",
        ) from exc


def _run_extraction_with_retry(agent, raw_text: str, filename: str):
    """
    Run extraction with up to MAX_RETRIES attempts.
    On failure, passes the error message back to Claude as correction context.
    """
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return agent.extract(
                raw_text=raw_text,
                filename=filename,
                prior_error=str(last_error) if last_error else None,
            )
        except ValueError as exc:
            last_error = exc
            logger.warning(
                "Extraction attempt %d/%d failed for '%s': %s",
                attempt, MAX_RETRIES, filename, exc,
            )

    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail=(
            f"AI extraction failed after {MAX_RETRIES} attempts for '{filename}'. "
            f"Last error: {last_error}. "
            "Ensure the PDF is a pharmaceutical drug label or prescribing information document."
        ),
    )


def _save_to_db(
    db: Session,
    shipment_id: str,
    schema,
    saved_filename: str,
    saved_path: str,
) -> Shipment:
    """Persist the validated extraction result to SQLite."""
    from agents.intake_agent import MODEL

    db_shipment = Shipment(
        shipment_id=shipment_id,
        source_pdf_filename=saved_filename,
        source_pdf_path=saved_path,
        extraction_model=MODEL,
        created_at=datetime.utcnow(),
        **schema.model_dump(),
    )
    db.add(db_shipment)
    db.commit()
    db.refresh(db_shipment)
    logger.info("Shipment saved to DB: id=%s drug=%s", shipment_id, schema.drug_name)
    return db_shipment
