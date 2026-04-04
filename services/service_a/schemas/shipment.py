"""
Pydantic schema for pharmaceutical shipment data stored in Firestore.

Field sources:

  [PDF]   — Claude extracts from the drug label PDF via intake_agent.py.
            Covers: drug identity, temperature thresholds, excursion window,
            do_not_freeze, light_sensitive, thaw_window_hours, stability notes,
            IATA codes.

  [HARD]  — seed.py writes these after Claude extraction using industry
            transport standards (WHO, ISTA 7E). Drug labels never publish
            humidity limits, shock G-force ratings, or flight delay thresholds.
            Covers: max_humidity_percent, max_shock_g, max_flight_delay_minutes,
            and their alert/spoilage messages.

The 5 monitored parameters per shipment (UI → Service B → Service C):

  1. temperature_celsius    slider    compared to temp_min/max_celsius      [PDF]
  2. humidity_percent       slider    compared to max_humidity_percent       [HARD]
  3. shock_g                slider    compared to max_shock_g                [HARD]
  4. excursion_minutes      computed  Service C accumulates out-of-range     [PDF threshold]
                                      time; fires when > max_excursion_duration_minutes
  5. flight_delay_status    dropdown  on_time / delayed_2h / delayed_6h     [HARD threshold]
                                      mapped to minutes via FLIGHT_DELAY_MINUTES,
                                      fires when delay_minutes >= max_flight_delay_minutes

Per-drug flight delay thresholds (set in seed.py TRANSPORT_OVERRIDES):
  pfizer-001  : 120 min (2h) — strictest, ultra-cold, 30-min excursion window
  moderna-001 : 240 min (4h) — moderate, 30-day post-thaw fallback
  jynneos-001 : 480 min (8h) — most tolerant, 8-week post-thaw window

UI dropdown behaviour with these thresholds:
  on_time    (  0 min) — no drug triggers
  delayed_2h (120 min) — Pfizer fires  (120 >= 120), Moderna OK, JYNNEOS OK
  delayed_6h (360 min) — Pfizer fires,  Moderna fires (360 > 240), JYNNEOS OK
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class TempClassification(str, Enum):
    ULTRA_COLD      = "ultra_cold"       # -90°C to -60°C  (Pfizer COMIRNATY)
    DEEP_FROZEN     = "deep_frozen"      # -50°C to -15°C  (Moderna, JYNNEOS)
    REFRIGERATED    = "refrigerated"     # +2°C  to +8°C
    ROOM_TEMP       = "room_temp"        # +15°C to +25°C
    CONTROLLED_ROOM = "controlled_room"  # +20°C to +25°C (USP CRT)


class CargoCategory(str, Enum):
    VACCINE        = "vaccine"
    BIOLOGIC       = "biologic"
    INSULIN        = "insulin"
    CHEMOTHERAPY   = "chemotherapy"
    IMMUNOTHERAPY  = "immunotherapy"
    CLINICAL_TRIAL = "clinical_trial"
    GENERAL_PHARMA = "general_pharma"


class FlightDelayStatus(str, Enum):
    """
    Discrete states sent from the UI flight delay dropdown.
    Service C maps these to minutes using FLIGHT_DELAY_MINUTES and compares
    against max_flight_delay_minutes (which differs per drug).
    """
    ON_TIME    = "on_time"     # 0 min   — never triggers any drug
    DELAYED_2H = "delayed_2h"  # 120 min — triggers Pfizer (120), not Moderna or JYNNEOS
    DELAYED_6H = "delayed_6h"  # 360 min — triggers Pfizer + Moderna (240), not JYNNEOS


# Lookup table — Service C converts dropdown string → integer minutes
FLIGHT_DELAY_MINUTES: dict[str, int] = {
    FlightDelayStatus.ON_TIME:    0,
    FlightDelayStatus.DELAYED_2H: 120,
    FlightDelayStatus.DELAYED_6H: 360,
}


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

class ShipmentSchema(BaseModel):
    """
    Complete shipment monitoring document written to Firestore by seed.py.
    Every /shipments/{drug_id} document matches this structure exactly.
    """

    # ------------------------------------------------------------------
    # Drug identity  [PDF]
    # ------------------------------------------------------------------
    drug_name: str = Field(
        ..., description="Brand/generic name e.g. 'COMIRNATY (BNT162b2)'"
    )
    manufacturer: str = Field(
        ..., description="Pharmaceutical company e.g. 'Pfizer-BioNTech'"
    )
    cargo_category: CargoCategory = Field(
        ..., description="Type of pharmaceutical cargo"
    )
    batch_numbers: list[str] = Field(
        default_factory=list,
        description="Lot/batch numbers if present in the label"
    )
    quantity_description: Optional[str] = Field(
        default=None,
        description=(
            "e.g. '195 vials, 0.3 mL each'. Null if not stated in the label — "
            "CDC storage summary PDFs typically do not include quantity information."
        )
    )

    # ------------------------------------------------------------------
    # Parameter 1 — Temperature  [PDF]
    # ------------------------------------------------------------------
    temp_classification: TempClassification = Field(
        ..., description="Storage temperature category"
    )
    temp_min_celsius: float = Field(
        ..., description="Minimum storage temperature in Celsius — from PDF"
    )
    temp_max_celsius: float = Field(
        ..., description="Maximum storage temperature in Celsius — from PDF"
    )
    max_excursion_duration_minutes: int = Field(
        ...,
        description=(
            "Max cumulative minutes outside temperature range before spoilage. "
            "From PDF; defaults if not stated: ultra_cold/deep_frozen=30, "
            "refrigerated=120, room_temp=480."
        )
    )
    do_not_freeze: bool = Field(
        default=False,
        description="True if label warns freezing causes irreversible damage."
    )
    freeze_threshold_celsius: Optional[float] = Field(
        default=None,
        description="Temperature below which freeze damage begins when do_not_freeze=True."
    )
    light_sensitive: bool = Field(
        default=False,
        description="True if label says 'protect from light' or 'store in original package'."
    )

    # ------------------------------------------------------------------
    # Stability  [PDF]
    # ------------------------------------------------------------------
    shelf_life_days: Optional[int] = Field(
        default=None, description="Total shelf life from manufacture in days."
    )
    thaw_window_hours: Optional[int] = Field(
        default=None,
        description=(
            "Hours the vaccine remains viable after confirmed thaw at 2-8°C. "
            "From PDF. Pfizer=1680 (10 wk), Moderna=720 (30 d), JYNNEOS=1344 (8 wk). "
            "Context for Service D spoilage reasoning — NOT the alert threshold."
        )
    )
    stability_note: Optional[str] = Field(
        default=None,
        description="Key stability text from the label e.g. 'Do NOT refreeze thawed vaccine'."
    )

    # ------------------------------------------------------------------
    # Parameter 2 — Humidity  [HARD]
    # ------------------------------------------------------------------
    max_humidity_percent: float = Field(
        default=75.0,
        description="Max relative humidity (% RH). From WHO/ISTA transport standards."
    )
    humidity_alert_message: str = Field(
        default="Humidity exceeds safe transport threshold",
        description="Shown in UI and passed to Service D when humidity is breached."
    )

    # ------------------------------------------------------------------
    # Parameter 3 — Shock  [HARD]
    # ------------------------------------------------------------------
    max_shock_g: float = Field(
        default=25.0,
        description="Max G-force from OnAsset SENTRY. From ISTA 7E air freight standard."
    )
    shock_alert_message: str = Field(
        default="Shock event exceeds safe handling threshold — integrity check required",
        description="Shown in UI and passed to Service D when shock is breached."
    )

    # ------------------------------------------------------------------
    # Parameter 4 — Excursion time
    # Threshold lives in max_excursion_duration_minutes above.
    # Service C tracks cumulative out-of-range minutes per shipment in
    # /monitoring_state/{drug_id} and fires breach.detected when exceeded.
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Parameter 5 — Flight delay  [HARD]
    # ------------------------------------------------------------------
    max_flight_delay_minutes: int = Field(
        default=240,
        description=(
            "Per-drug threshold set in seed.py. "
            "pfizer-001=120, moderna-001=240, jynneos-001=480. "
            "Service C compares FLIGHT_DELAY_MINUTES[status] against this value."
        )
    )
    flight_delay_spoilage_note: str = Field(
        default="Assess cold chain continuity and cumulative excursion time.",
        description="Drug-specific guidance fed to Service D agent on flight delay breach."
    )

    # ------------------------------------------------------------------
    # Regulatory  [PDF]
    # ------------------------------------------------------------------
    regulatory_framework: str = Field(
        default="EU GDP 2013/C 343/01",
        description="Primary regulatory framework for this shipment."
    )
    iata_handling_codes: list[str] = Field(
        default_factory=list,
        description=(
            "IATA codes: PIL (pharmaceuticals), ACT (active temp control), "
            "PIP (passive insulated packaging), EMD (electronic monitoring device)."
        )
    )
    special_instructions: Optional[str] = Field(
        default=None, description="Additional handling instructions from the label."
    )

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------

    @field_validator("temp_min_celsius", "temp_max_celsius")
    @classmethod
    def validate_temp_range(cls, v: float) -> float:
        if v < -200 or v > 60:
            raise ValueError(f"Temperature {v}°C is outside plausible range (-200°C to +60°C).")
        return v

    @field_validator("max_excursion_duration_minutes")
    @classmethod
    def validate_excursion(cls, v: int) -> int:
        if v < 1 or v > 10080:
            raise ValueError("max_excursion_duration_minutes must be 1-10080 (7 days).")
        return v

    @field_validator("max_flight_delay_minutes")
    @classmethod
    def validate_flight_delay(cls, v: int) -> int:
        if v < 0 or v > 1440:
            raise ValueError("max_flight_delay_minutes must be 0-1440 (24 hours).")
        return v

    class Config:
        use_enum_values = True

    def to_firestore_dict(self) -> dict:
        """Return a plain dict suitable for Firestore. Enums serialised to strings."""
        return self.model_dump()
