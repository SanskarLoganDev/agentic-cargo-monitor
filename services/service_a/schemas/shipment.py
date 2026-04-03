"""
Pydantic schemas for shipment data extracted from pharmaceutical drug label PDFs.

These models define the exact structure that Claude must return and that gets
stored in SQLite. Every field maps to a real pharmaceutical shipping requirement.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class TempClassification(str, Enum):
    ULTRA_COLD = "ultra_cold"       # -90°C to -60°C  (e.g. Pfizer mRNA)
    DEEP_FROZEN = "deep_frozen"     # -25°C to -15°C  (e.g. Moderna)
    REFRIGERATED = "refrigerated"   # +2°C to +8°C    (e.g. Insulin, Herceptin)
    ROOM_TEMP = "room_temp"         # +15°C to +25°C  (e.g. some oral drugs)
    CONTROLLED_ROOM = "controlled_room"  # +20°C to +25°C (USP CRT)


class CargoCategory(str, Enum):
    VACCINE = "vaccine"
    BIOLOGIC = "biologic"
    INSULIN = "insulin"
    CHEMOTHERAPY = "chemotherapy"
    IMMUNOTHERAPY = "immunotherapy"
    CLINICAL_TRIAL = "clinical_trial"
    GENERAL_PHARMA = "general_pharma"


class ShipmentSchema(BaseModel):
    """
    Structured data extracted from a pharmaceutical drug label / prescribing info PDF.
    Claude populates this from the raw PDF text. Pydantic validates before DB write.
    """

    # --- Drug identity ---
    drug_name: str = Field(..., description="Brand or generic name of the drug")
    manufacturer: str = Field(..., description="Pharmaceutical company name")
    cargo_category: CargoCategory = Field(..., description="Type of pharmaceutical cargo")
    batch_numbers: list[str] = Field(default_factory=list, description="Lot/batch numbers if present")
    quantity_description: str = Field(..., description="e.g. '500 vials, 0.3 mL each'")

    # --- Temperature requirements ---
    temp_classification: TempClassification = Field(..., description="Temperature category")
    temp_min_celsius: float = Field(..., description="Minimum storage temperature in Celsius")
    temp_max_celsius: float = Field(..., description="Maximum storage temperature in Celsius")
    max_excursion_duration_minutes: int = Field(
        ...,
        description="Maximum cumulative time allowed outside temperature range before spoilage risk"
    )
    do_not_freeze: bool = Field(
        default=False,
        description="True if freezing causes irreversible damage (e.g. insulin, Herceptin)"
    )
    freeze_threshold_celsius: Optional[float] = Field(
        default=None,
        description="Temperature at which freeze damage occurs if do_not_freeze is True"
    )

    # --- Other environmental sensitivities ---
    max_humidity_percent: Optional[float] = Field(
        default=None,
        description="Maximum relative humidity percentage allowed"
    )
    light_sensitive: bool = Field(
        default=False,
        description="True if drug degrades under light exposure (protect from light)"
    )
    shake_sensitive: bool = Field(
        default=False,
        description="True if vibration/shaking causes protein aggregation (e.g. Keytruda)"
    )
    max_shock_g: Optional[float] = Field(
        default=None,
        description="Maximum G-force from OnAsset SENTRY shock sensor before integrity check needed"
    )

    # --- Spoilage and stability ---
    shelf_life_days: Optional[int] = Field(
        default=None,
        description="Total shelf life from manufacture date in days"
    )
    stability_note: Optional[str] = Field(
        default=None,
        description="Key stability information e.g. 'discard 6 hrs after dilution'"
    )

    # --- Route and parties (may be partial from drug label alone) ---
    origin_airport_iata: Optional[str] = Field(
        default=None,
        description="IATA airport code of departure e.g. FRA"
    )
    destination_airport_iata: Optional[str] = Field(
        default=None,
        description="IATA airport code of destination e.g. NBO"
    )
    consignee_name: Optional[str] = Field(
        default=None,
        description="Receiving hospital, clinic, or distributor name"
    )
    consignee_contact: Optional[str] = Field(
        default=None,
        description="Contact name, phone, or email of consignee"
    )
    responsible_person: Optional[str] = Field(
        default=None,
        description="GDP responsible person name"
    )

    # --- Regulatory ---
    regulatory_framework: str = Field(
        default="EU GDP 2013/C 343/01",
        description="Applicable regulatory framework for this shipment"
    )
    iata_handling_codes: list[str] = Field(
        default_factory=list,
        description="IATA handling codes e.g. ['PIL', 'ACT', 'EMD']"
    )
    special_instructions: Optional[str] = Field(
        default=None,
        description="Any additional handling instructions from the label"
    )

    @field_validator("temp_min_celsius", "temp_max_celsius")
    @classmethod
    def validate_temp_range(cls, v: float) -> float:
        if v < -200 or v > 100:
            raise ValueError(f"Temperature {v}°C is outside plausible pharmaceutical range")
        return v

    @field_validator("max_excursion_duration_minutes")
    @classmethod
    def validate_excursion_minutes(cls, v: int) -> int:
        if v < 1 or v > 10080:  # 1 minute to 1 week
            raise ValueError("Excursion duration must be between 1 minute and 7 days")
        return v

    class Config:
        use_enum_values = True


class ShipmentRecord(ShipmentSchema):
    """
    Full shipment record as stored in DB — adds system-generated fields.
    Returned to the frontend and downstream services.
    """
    id: int
    shipment_id: str = Field(..., description="UUID assigned at upload time")
    source_pdf_filename: str = Field(..., description="Original PDF filename")
    source_pdf_path: str = Field(..., description="Path to saved PDF in /uploads/")
    extraction_model: str = Field(..., description="Claude model used for extraction")
    created_at: datetime
    status: str = Field(default="active", description="active | completed | flagged")

    class Config:
        from_attributes = True
