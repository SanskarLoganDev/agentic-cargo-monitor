"""
Pydantic schema for pharmaceutical shipment data extracted from drug label PDFs.

ShipmentSchema defines the exact structure Claude must return and that gets
written to Firestore. Every field maps to a real pharmaceutical shipping
or monitoring requirement.

Note: ShipmentRecord (the old SQLite-backed model with id, source_pdf_path,
etc.) has been removed. Firestore does not need those fields — the document
ID itself serves as the shipment identifier.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class TempClassification(str, Enum):
    ULTRA_COLD    = "ultra_cold"      # -90°C to -60°C  (e.g. Pfizer COMIRNATY)
    DEEP_FROZEN   = "deep_frozen"     # -50°C to -15°C  (e.g. Moderna Spikevax)
    REFRIGERATED  = "refrigerated"    # +2°C  to +8°C   (e.g. Herceptin, Insulin)
    ROOM_TEMP     = "room_temp"       # +15°C to +25°C
    CONTROLLED_ROOM = "controlled_room"  # +20°C to +25°C (USP CRT)


class CargoCategory(str, Enum):
    VACCINE        = "vaccine"
    BIOLOGIC       = "biologic"
    INSULIN        = "insulin"
    CHEMOTHERAPY   = "chemotherapy"
    IMMUNOTHERAPY  = "immunotherapy"
    CLINICAL_TRIAL = "clinical_trial"
    GENERAL_PHARMA = "general_pharma"


class ShipmentSchema(BaseModel):
    """
    Structured data extracted from a pharmaceutical drug label PDF by Claude.
    Pydantic validates this before it is written to Firestore.

    Used by:
      - seed.py         : validates Claude output, then writes to Firestore
      - Service C       : reads from Firestore to check telemetry thresholds
      - Service D       : reads from Firestore to build agent context
    """

    # --- Drug identity ---
    drug_name: str = Field(
        ..., description="Brand or generic name of the drug e.g. 'COMIRNATY (BNT162b2)'"
    )
    manufacturer: str = Field(
        ..., description="Pharmaceutical company name e.g. 'Pfizer-BioNTech'"
    )
    cargo_category: CargoCategory = Field(
        ..., description="Type of pharmaceutical cargo"
    )
    batch_numbers: list[str] = Field(
        default_factory=list,
        description="Lot/batch numbers if present in the label"
    )
    quantity_description: str = Field(
        ..., description="e.g. '195 vials, 0.3 mL each' or 'Not specified'"
    )

    # --- Temperature requirements ---
    temp_classification: TempClassification = Field(
        ..., description="Temperature storage category"
    )
    temp_min_celsius: float = Field(
        ..., description="Minimum storage temperature in Celsius"
    )
    temp_max_celsius: float = Field(
        ..., description="Maximum storage temperature in Celsius"
    )
    max_excursion_duration_minutes: int = Field(
        ...,
        description=(
            "Maximum cumulative time allowed outside the temperature range "
            "before spoilage risk is considered. Derive from label; use "
            "conservative defaults if not stated: ultra_cold/deep_frozen=30, "
            "refrigerated=120, room_temp=480."
        )
    )
    do_not_freeze: bool = Field(
        default=False,
        description=(
            "True if label explicitly warns that freezing causes irreversible "
            "damage (e.g. Herceptin, insulin). False for vaccines stored frozen."
        )
    )
    freeze_threshold_celsius: Optional[float] = Field(
        default=None,
        description=(
            "Temperature below which freeze damage occurs when do_not_freeze=True. "
            "Typically 0.0°C for most biologics."
        )
    )

    # --- Other environmental sensitivities ---
    max_humidity_percent: Optional[float] = Field(
        default=None,
        description="Maximum relative humidity (%) allowed during transport"
    )
    light_sensitive: bool = Field(
        default=False,
        description="True if drug degrades under light (label says 'protect from light')"
    )
    shake_sensitive: bool = Field(
        default=False,
        description=(
            "True if label warns against shaking — indicates protein aggregation "
            "risk from vibration (common in monoclonal antibodies like Herceptin)"
        )
    )
    max_shock_g: Optional[float] = Field(
        default=None,
        description=(
            "Maximum G-force from OnAsset SENTRY shock sensor before an integrity "
            "check is required. Null if not specified in the label."
        )
    )

    # --- Spoilage and stability ---
    shelf_life_days: Optional[int] = Field(
        default=None,
        description="Total shelf life from manufacture date in days"
    )
    stability_note: Optional[str] = Field(
        default=None,
        description=(
            "Key stability information from the label e.g. "
            "'Discard 6 hours after dilution' or 'Stable 10 weeks refrigerated after thaw'"
        )
    )

    # --- Regulatory ---
    regulatory_framework: str = Field(
        default="EU GDP 2013/C 343/01",
        description="Primary regulatory framework governing this shipment"
    )
    iata_handling_codes: list[str] = Field(
        default_factory=list,
        description=(
            "IATA cargo handling codes. Use: PIL (pharmaceuticals), "
            "ACT (active temp control), CRT (controlled room temp), "
            "PIP (passive insulated packaging), EMD (electronic monitoring device)"
        )
    )
    special_instructions: Optional[str] = Field(
        default=None,
        description="Any additional handling instructions from the label"
    )

    # --- Validators ---

    @field_validator("temp_min_celsius", "temp_max_celsius")
    @classmethod
    def validate_temp_range(cls, v: float) -> float:
        if v < -200 or v > 60:
            raise ValueError(
                f"Temperature {v}°C is outside the plausible pharmaceutical "
                "range (-200°C to +60°C)"
            )
        return v

    @field_validator("max_excursion_duration_minutes")
    @classmethod
    def validate_excursion_minutes(cls, v: int) -> int:
        if v < 1 or v > 10080:  # 1 minute to 7 days
            raise ValueError(
                "max_excursion_duration_minutes must be between 1 and 10080 (7 days)"
            )
        return v

    class Config:
        use_enum_values = True

    def to_firestore_dict(self) -> dict:
        """
        Return a plain dict suitable for writing to Firestore.
        Firestore does not accept Pydantic enums — this converts them to strings.
        """
        return self.model_dump()
