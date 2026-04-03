"""
Intake Agent — Service A's core AI component.

Responsibilities:
1. Accept raw text extracted from a pharmaceutical drug label PDF
2. Call Claude API with a carefully engineered extraction prompt
3. Parse and validate the returned JSON against ShipmentSchema
4. Retry once with error feedback if validation fails

This is NOT agentic (no tool-use loop) — it is a single-shot AI extraction call.
The agentic intelligence lives in Service D/E. Service A is a workflow step.
"""

import json
import logging
import os
from typing import Optional

import anthropic
from pydantic import ValidationError

from schemas.shipment import ShipmentSchema

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 2048
MAX_RETRIES = 2

SYSTEM_PROMPT = """You are a pharmaceutical data extraction specialist.
You will be given raw text extracted from an official drug label, prescribing information,
or storage-and-handling summary PDF for a pharmaceutical product.

Your job is to extract structured shipment monitoring data from this text and return it
as a single valid JSON object. You must follow these rules strictly:

1. Return ONLY valid JSON. No preamble, no explanation, no markdown code fences.
2. If a field cannot be found in the text, use null for optional fields.
3. For temperature values, always use Celsius (convert from Fahrenheit if needed).
4. For max_excursion_duration_minutes: if the label says "2 hours", return 120.
   If no explicit excursion window is given, use conservative defaults:
   - ultra_cold / deep_frozen: 30 minutes
   - refrigerated: 120 minutes (2 hours)
   - room_temp / controlled_room: 480 minutes (8 hours)
5. For do_not_freeze: set true if the label explicitly warns against freezing.
6. For shake_sensitive: set true only if the label mentions protein aggregation
   risk from shaking or vibration (common in monoclonal antibodies).
7. For iata_handling_codes: use standard codes — PIL (pharmaceuticals),
   ACT (active temp control), CRT (controlled room temp), PIP (passive insulated
   packaging), EMD (electronic monitoring device).
8. cargo_category must be one of: vaccine, biologic, insulin, chemotherapy,
   immunotherapy, clinical_trial, general_pharma
9. temp_classification must be one of: ultra_cold, deep_frozen, refrigerated,
   room_temp, controlled_room

Required JSON structure:
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
  "origin_airport_iata": null,
  "destination_airport_iata": null,
  "consignee_name": null,
  "consignee_contact": null,
  "responsible_person": null,
  "regulatory_framework": "string",
  "iata_handling_codes": [],
  "special_instructions": "string or null"
}"""


# ---------------------------------------------------------------------------
# Main extraction function
# ---------------------------------------------------------------------------

class IntakeAgent:
    """
    Wraps the Claude API call for PDF data extraction.
    Instantiate once at app startup (stored on app.state).
    """

    def __init__(self):
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise EnvironmentError(
                "ANTHROPIC_API_KEY environment variable is not set. "
                "Add it to your .env file."
            )
        self.client = anthropic.Anthropic(api_key=api_key)
        logger.info("IntakeAgent initialised with model %s", MODEL)

    def extract(
        self,
        raw_text: str,
        filename: str,
        prior_error: Optional[str] = None,
    ) -> ShipmentSchema:
        """
        Extract shipment data from raw PDF text.

        Args:
            raw_text: Full text extracted from the PDF by pypdf.
            filename: Original filename — used in logs and error messages.
            prior_error: If this is a retry, pass the previous Pydantic error here
                         so Claude can correct its output.

        Returns:
            Validated ShipmentSchema instance.

        Raises:
            ValueError: If extraction or validation fails after all retries.
        """
        if len(raw_text.strip()) < 50:
            raise ValueError(
                f"PDF '{filename}' yielded fewer than 50 characters of text. "
                "It may be a scanned image PDF. Please upload a text-based PDF."
            )

        user_message = self._build_user_message(raw_text, filename, prior_error)

        logger.info("Calling Claude API for extraction of '%s'", filename)

        response = self.client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )

        raw_json_text = response.content[0].text.strip()

        logger.debug(
            "Claude response for '%s': %d input tokens, %d output tokens",
            filename,
            response.usage.input_tokens,
            response.usage.output_tokens,
        )

        # Parse and validate
        try:
            data = json.loads(raw_json_text)
            shipment = ShipmentSchema(**data)
            logger.info("Extraction successful for '%s': drug=%s", filename, shipment.drug_name)
            return shipment

        except (json.JSONDecodeError, ValidationError) as exc:
            error_detail = str(exc)
            logger.warning(
                "Validation failed for '%s': %s", filename, error_detail
            )
            raise ValueError(
                f"Claude returned invalid data for '{filename}': {error_detail}"
            ) from exc

    def _build_user_message(
        self,
        raw_text: str,
        filename: str,
        prior_error: Optional[str],
    ) -> str:
        """Build the user message, optionally including a retry correction prompt."""

        # Truncate very long PDFs to avoid hitting context limits.
        # Drug labels are typically 2,000–15,000 chars; prescribing info can be 100k+.
        max_chars = 40_000
        truncated = raw_text[:max_chars]
        if len(raw_text) > max_chars:
            truncated += "\n\n[TEXT TRUNCATED — first 40,000 characters shown]"

        base = (
            f"Extract the pharmaceutical shipment monitoring data from the following "
            f"drug label text (source file: {filename}).\n\n"
            f"--- BEGIN PDF TEXT ---\n{truncated}\n--- END PDF TEXT ---"
        )

        if prior_error:
            correction = (
                f"\n\nIMPORTANT: Your previous response failed validation with this error:\n"
                f"{prior_error}\n"
                f"Please correct your JSON output to fix this issue."
            )
            return base + correction

        return base
