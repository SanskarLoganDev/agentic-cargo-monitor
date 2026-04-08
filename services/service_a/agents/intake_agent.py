"""
Intake Agent — Service A's Claude extraction component.

Extracts all fields that come from the drug label PDF:
  - Drug identity (name, manufacturer, category)
  - Temperature thresholds (min, max, classification, excursion window)
  - Storage flags (do_not_freeze, light_sensitive)
  - Stability data (shelf_life_days, stability_note, thaw_window_hours)
  - Regulatory (iata_handling_codes, special_instructions)

Does NOT extract or invent:
  - max_humidity_percent  — from WHO/ISTA transport standards (seed.py)
  - max_shock_g           — from ISTA 7E air freight standard (seed.py)
  - max_flight_delay_minutes — derived from thaw_window_hours (seed.py)

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

MODEL       = "claude-sonnet-4-6"
MAX_TOKENS  = 2048
MAX_RETRIES = 2

SYSTEM_PROMPT = """You are a pharmaceutical data extraction specialist.
You will be given raw text extracted from an official drug label, prescribing
information, or CDC storage-and-handling summary PDF for a pharmaceutical product.

Your job is to extract structured shipment monitoring data and return it as a
single valid JSON object. Follow these rules strictly:

1. Return ONLY valid JSON. No preamble, no explanation, no markdown code fences.
2. If a field cannot be found in the text, use null for optional fields.
3. All temperatures must be in Celsius. Convert Fahrenheit if needed:
   °C = (°F − 32) × 5/9

4. For max_excursion_duration_minutes: look for language like "temperature excursion",
   "out of range", or explicit time limits during transport. If not stated, use:
     ultra_cold / deep_frozen  → 30 minutes
     refrigerated              → 120 minutes
     room_temp / controlled_room → 480 minutes

5. For do_not_freeze: set true ONLY if the label explicitly warns "Do NOT freeze"
   as a damage warning (e.g. for insulin, Herceptin). Set false for vaccines that
   ARE designed to be stored frozen (Pfizer, Moderna, JYNNEOS all store frozen).

6. For light_sensitive: set true if the label says "protect from light" or
   "store in original package" to shield from light.

7. For thaw_window_hours: this is the number of hours a vaccine remains viable
   AFTER being removed from frozen storage and placed in a refrigerator (2–8°C).
   Look for phrases like "may be stored refrigerated for up to X weeks/days/hours
   after thawing", "beyond-use date", or "must be used within X weeks of thawing".
   Convert to hours: weeks × 168, days × 24.
   If no thaw window is stated, return null.
   Examples:
     "up to 10 weeks" → 1680
     "up to 30 days"  → 720
     "up to 8 weeks"  → 1344
     "up to 12 hours" → 12

8. For cargo_category use exactly one of:
     vaccine, biologic, insulin, chemotherapy, immunotherapy,
     clinical_trial, general_pharma

9. For temp_classification use exactly one of:
     ultra_cold      (-90°C to -60°C)
     deep_frozen     (-50°C to -15°C)
     refrigerated    (+2°C to +8°C)
     room_temp       (+15°C to +25°C)
     controlled_room (+20°C to +25°C)

10. For iata_handling_codes use only:
     PIL  pharmaceutical products
     ACT  active temperature control
     PIP  passive insulated packaging
     EMD  electronic monitoring device

11. Do NOT fill max_humidity_percent, max_shock_g, max_flight_delay_minutes.
    Leave them out entirely — they are added by the system after extraction.

Required JSON structure (return exactly these keys, nothing more):
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
  "light_sensitive": boolean,
  "shelf_life_days": integer or null,
  "thaw_window_hours": integer or null,
  "stability_note": "string or null",
  "regulatory_framework": "string",
  "iata_handling_codes": [],
  "special_instructions": "string or null"
}"""


class IntakeAgent:
    """
    Wraps the Claude API call for PDF data extraction.
    Instantiate once and reuse across all three drug PDFs.
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
        Extract and validate PDF-sourced shipment fields.

        Hardcoded fields (humidity, shock, flight delay) are NOT extracted
        here — seed.py applies them after this method returns.

        Args:
            raw_text:    Full text extracted from the PDF by pypdf.
            filename:    Original filename — used in logs and error messages.
            prior_error: On retry, the previous validation error for self-correction.

        Returns:
            Partially-populated ShipmentSchema (hardcoded fields at schema defaults).

        Raises:
            ValueError: If text is too short or extraction/validation fails.
        """
        if len(raw_text.strip()) < 50:
            raise ValueError(
                f"'{filename}' produced fewer than 50 characters of text. "
                "It may be a scanned image PDF — only digital PDFs are supported."
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
                "Extraction validated for '%s' — drug: %s | temp: %.0f–%.0f°C | "
                "thaw window: %s hrs",
                filename,
                schema.drug_name,
                schema.temp_min_celsius,
                schema.temp_max_celsius,
                str(schema.thaw_window_hours) if schema.thaw_window_hours else "not stated",
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
