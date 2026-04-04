"""
Intake Agent — Service A's Claude extraction component.

Responsibilities:
  1. Accept raw text extracted from a pharmaceutical drug label PDF
  2. Call Claude API with a carefully engineered extraction prompt
  3. Parse and validate the returned JSON against ShipmentSchema
  4. Retry up to MAX_RETRIES times, feeding the previous error back to
     Claude so it can self-correct

This is a single-shot AI extraction call — NOT an agentic loop.
The agentic intelligence lives in Services D and E.
"""

import json
import logging
import os
from typing import Optional

import anthropic
from pydantic import ValidationError

from schemas.shipment import ShipmentSchema

logger = logging.getLogger(__name__)

MODEL      = "claude-sonnet-4-6"
MAX_TOKENS = 2048
MAX_RETRIES = 2

# ---------------------------------------------------------------------------
# System prompt — instructs Claude on output format and extraction rules
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a pharmaceutical data extraction specialist.
You will be given raw text extracted from an official drug label, prescribing
information, or storage-and-handling summary PDF for a pharmaceutical product.

Your job is to extract structured shipment monitoring data from this text and
return it as a single valid JSON object. Follow these rules strictly:

1. Return ONLY valid JSON. No preamble, no explanation, no markdown code fences.
2. If a field cannot be found in the text, use null for optional fields.
3. For temperature values, always use Celsius (convert from Fahrenheit if needed:
   °C = (°F - 32) × 5/9).
4. For max_excursion_duration_minutes: derive from the label if stated.
   If not stated, use conservative defaults:
     ultra_cold / deep_frozen  → 30 minutes
     refrigerated              → 120 minutes (2 hours)
     room_temp / controlled_room → 480 minutes (8 hours)
5. For do_not_freeze: set true if the label explicitly warns against freezing
   (e.g. "Do NOT freeze", "Do not refreeze"). Set false for drugs designed to
   be stored frozen.
6. For shake_sensitive: set true ONLY if the label warns against shaking or
   mentions protein aggregation risk from vibration. Common in monoclonal
   antibodies. The label will say "DO NOT SHAKE" or "swirl gently".
7. For iata_handling_codes, use only standard IATA codes:
     PIL — pharmaceutical products
     ACT — active temperature control system
     CRT — controlled room temperature
     PIP — passive insulated packaging
     EMD — electronic monitoring device on shipment
8. cargo_category must be exactly one of:
     vaccine, biologic, insulin, chemotherapy, immunotherapy,
     clinical_trial, general_pharma
9. temp_classification must be exactly one of:
     ultra_cold, deep_frozen, refrigerated, room_temp, controlled_room

Required JSON structure (return exactly these keys):
{
  "drug_name": "string",
  "manufacturer": "string",
  "cargo_category": "string",
  "batch_numbers": [],
  "quantity_description": "string",
  "temp_classification": "string",
  "temp_min_celsius": number,
  "temp_max_celsius": number,
  "max_excursion_duration_minutes": integer,
  "do_not_freeze": boolean,
  "freeze_threshold_celsius": number or null,
  "max_humidity_percent": number or null,
  "light_sensitive": boolean,
  "shake_sensitive": boolean,
  "max_shock_g": number or null,
  "shelf_life_days": integer or null,
  "stability_note": "string or null",
  "regulatory_framework": "string",
  "iata_handling_codes": [],
  "special_instructions": "string or null"
}"""


class IntakeAgent:
    """
    Wraps the Claude API call for PDF data extraction.
    Instantiate once and reuse across multiple extract() calls.
    """

    def __init__(self) -> None:
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise EnvironmentError(
                "ANTHROPIC_API_KEY is not set. Add it to your .env file."
            )
        self.client = anthropic.Anthropic(api_key=api_key)
        logger.info("IntakeAgent ready — model: %s", MODEL)

    def extract(
        self,
        raw_text: str,
        filename: str,
        prior_error: Optional[str] = None,
    ) -> ShipmentSchema:
        """
        Extract and validate shipment monitoring data from raw PDF text.

        Args:
            raw_text:    Full text extracted from the PDF by pypdf.
            filename:    Original filename — used in logs and error messages.
            prior_error: If this is a retry, the previous Pydantic/JSON error
                         is passed back to Claude so it can self-correct.

        Returns:
            Validated ShipmentSchema instance.

        Raises:
            ValueError: If text is too short, or extraction/validation fails.
        """
        if len(raw_text.strip()) < 50:
            raise ValueError(
                f"'{filename}' produced fewer than 50 characters of text. "
                "It may be a scanned image PDF — only digitally-created PDFs "
                "are supported."
            )

        user_message = self._build_user_message(raw_text, filename, prior_error)

        logger.info("Calling Claude API for '%s'", filename)
        response = self.client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )

        raw_json = response.content[0].text.strip()
        logger.info(
            "Claude responded for '%s' — input_tokens=%d output_tokens=%d",
            filename,
            response.usage.input_tokens,
            response.usage.output_tokens,
        )

        try:
            data = json.loads(raw_json)
            schema = ShipmentSchema(**data)
            logger.info(
                "Extraction validated for '%s' — drug: %s",
                filename,
                schema.drug_name,
            )
            return schema

        except (json.JSONDecodeError, ValidationError) as exc:
            logger.warning("Validation failed for '%s': %s", filename, exc)
            raise ValueError(
                f"Claude returned invalid data for '{filename}': {exc}"
            ) from exc

    def _build_user_message(
        self,
        raw_text: str,
        filename: str,
        prior_error: Optional[str],
    ) -> str:
        """
        Construct the user message for Claude.
        On retry, appends the previous error so Claude can self-correct.
        """
        max_chars = 40_000
        body = raw_text[:max_chars]
        if len(raw_text) > max_chars:
            body += "\n\n[TEXT TRUNCATED — first 40,000 characters shown]"

        message = (
            f"Extract the pharmaceutical shipment monitoring data from the "
            f"following drug label text (source file: {filename}).\n\n"
            f"--- BEGIN PDF TEXT ---\n{body}\n--- END PDF TEXT ---"
        )

        if prior_error:
            message += (
                f"\n\nIMPORTANT — your previous response failed validation "
                f"with this error:\n{prior_error}\n"
                f"Please fix your JSON output to resolve this error."
            )

        return message
