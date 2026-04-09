"""
Pydantic schema for pharmaceutical shipment data stored in Firestore.

Field sources:

  [PDF]    — Claude extracts from the drug label PDF via intake_agent.py.
             Covers: drug identity, temperature thresholds, excursion window,
             do_not_freeze, light_sensitive, thaw_window_hours, stability notes,
             IATA codes.

  [HARD]   — seed.py writes these after Claude extraction using industry
             transport standards (WHO, ISTA 7E). Drug labels never publish
             humidity limits, shock G-force ratings, or flight delay thresholds.
             Covers: max_humidity_percent, max_shock_g, max_flight_delay_minutes,
             and their alert/spoilage messages.

  [MANUAL] — Set directly in TRANSPORT_OVERRIDES in seed.py.
             Covers all contact, logistics, cargo, and timing fields —
             none of these appear in drug label PDFs.

The 5 monitored parameters per shipment (UI -> Service B -> Service C):

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

Service D tool → field mapping:
  calculate_spoilage_time   : thaw_window_hours, final_destination_eta, excursion_minutes
  find_alternative_carrier  : flight_icao, current_carrier, destination_facility_name,
                               destination_address, total_units, total_weight_kg,
                               pallet_dimensions
  draft_hospital_notification: receiver_poc_name, receiver_poc_email,
                               manufacturer_support_email, destination_facility_name,
                               total_units
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class TempClassification(str, Enum):
    ULTRA_COLD      = "ultra_cold"       # -90C to -60C  (Pfizer COMIRNATY)
    DEEP_FROZEN     = "deep_frozen"      # -50C to -15C  (Moderna, JYNNEOS)
    REFRIGERATED    = "refrigerated"     # +2C  to +8C
    ROOM_TEMP       = "room_temp"        # +15C to +25C
    CONTROLLED_ROOM = "controlled_room"  # +20C to +25C (USP CRT)


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


# Lookup table — Service C converts dropdown string -> integer minutes
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
            "Free-text quantity from the label e.g. '195 vials, 0.3 mL each'. "
            "Null for CDC storage summary PDFs which omit quantity. "
            "Use total_units for machine-readable unit count."
        )
    )

    # ------------------------------------------------------------------
    # Sender-side operator contacts  [MANUAL]
    # The cold-chain operator responsible for dispatching this shipment.
    # Service E uses these to alert the sender-side party on breach.
    # ------------------------------------------------------------------
    contact_email: Optional[str] = Field(
        default=None,
        description=(
            "Email of the sender-side cold-chain operator. "
            "e.g. 'cold-chain-ops@pharmalogistics.com'"
        )
    )
    contact_phone: Optional[str] = Field(
        default=None,
        description=(
            "Phone of the sender-side operator in E.164 format. "
            "e.g. '+12025551234'"
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
            "Hours the vaccine remains viable after confirmed thaw at 2-8C. "
            "From PDF. Pfizer=1680 (10 wk), Moderna=720 (30 d), JYNNEOS=1344 (8 wk). "
            "Used by Service D calculate_spoilage_time — NOT the alert threshold."
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
    # Logistics & Routing  [MANUAL]
    # Used by Service D find_alternative_carrier tool.
    # ------------------------------------------------------------------
    flight_icao: Optional[str] = Field(
        default=None,
        description=(
            "ICAO flight number e.g. 'KAL82'. "
            "Service D uses this to query real-time flight APIs (FlightAware, "
            "AviationStack) for current position, ETA, and carrier contact details."
        )
    )
    destination_facility_name: Optional[str] = Field(
        default=None,
        description=(
            "Name of the receiving facility e.g. 'Johns Hopkins Central Pharmacy'. "
            "Used in carrier booking and in the hospital notification draft."
        )
    )
    destination_address: Optional[str] = Field(
        default=None,
        description=(
            "Full street address of the destination facility. "
            "Required by logistics APIs and last-mile carrier booking systems "
            "to generate routing and quotes."
        )
    )
    current_carrier: Optional[str] = Field(
        default=None,
        description=(
            "Name of the carrier currently holding the shipment e.g. 'Delta Cargo'. "
            "Service D needs this to issue a stop/hold order before rerouting."
        )
    )

    # ------------------------------------------------------------------
    # Cargo Specifications  [MANUAL]
    # Used by Service D find_alternative_carrier and financial impact tools.
    # ------------------------------------------------------------------
    total_units: Optional[int] = Field(
        default=None,
        description=(
            "Total number of vials in this shipment e.g. 15000. "
            "Used to tell the receiving hospital how many replacement doses to order, "
            "and to calculate financial loss (cost_per_vial * total_units)."
        )
    )
    total_weight_kg: Optional[float] = Field(
        default=None,
        description=(
            "Total shipment weight in kilograms e.g. 450.5. "
            "Required by carrier quoting APIs — a carrier cannot confirm capacity "
            "or price without knowing the payload weight."
        )
    )
    pallet_dimensions: Optional[str] = Field(
        default=None,
        description=(
            "Pallet dimensions as a string e.g. '48x40x60 inches'. "
            "Required by logistics quoting APIs alongside weight to confirm "
            "the shipment fits the alternative carrier's vehicle."
        )
    )

    # ------------------------------------------------------------------
    # Receiver-side stakeholder contacts  [MANUAL]
    # Used by Service D draft_hospital_notification tool and Service E.
    # Distinct from contact_email/contact_phone which are the sender-side operator.
    # ------------------------------------------------------------------
    receiver_poc_name: Optional[str] = Field(
        default=None,
        description=(
            "Full name of the point of contact at the receiving facility "
            "e.g. 'Dr. Sarah Jenkins'. Used to personalise the notification email — "
            "'Dear Dr. Jenkins' rather than 'Dear Receiving Facility'."
        )
    )
    receiver_poc_email: Optional[str] = Field(
        default=None,
        description=(
            "Email address of the receiver POC. "
            "Service E sends the hospital notification to this address via SendGrid."
        )
    )
    manufacturer_support_email: Optional[str] = Field(
        default=None,
        description=(
            "Emergency cold-chain support email for the drug manufacturer "
            "e.g. 'coldchain-emergencies@pfizer.com'. "
            "Service D drafts a secondary notification to the manufacturer "
            "reporting the compromised lot for regulatory and stability investigation."
        )
    )

    # ------------------------------------------------------------------
    # Timing Context  [MANUAL]
    # Used by Service D calculate_spoilage_time tool.
    # ------------------------------------------------------------------
    final_destination_eta: Optional[str] = Field(
        default=None,
        description=(
            "ISO 8601 UTC timestamp of the scheduled arrival at the destination facility "
            "e.g. '2026-04-06T18:00:00Z'. "
            "Service D compares this against the current telemetry timestamp plus "
            "flight_delay_status to calculate remaining transit time, then determines "
            "whether the remaining cold-chain life (thaw_window_hours minus excursion "
            "already consumed) can survive the rest of the journey."
        )
    )

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------

    @field_validator("contact_email", "receiver_poc_email", "manufacturer_support_email")
    @classmethod
    def validate_email(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        pattern = r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$"
        if not re.match(pattern, v):
            raise ValueError(
                f"'{v}' is not a valid email address. "
                "Expected format: name@domain.com"
            )
        return v

    @field_validator("contact_phone")
    @classmethod
    def validate_phone(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if not re.match(r"^\+[1-9]\d{6,14}$", v):
            raise ValueError(
                f"'{v}' is not a valid E.164 phone number. "
                "Expected format: +12025551234 (+ followed by 7-15 digits, no spaces)"
            )
        return v

    @field_validator("temp_min_celsius", "temp_max_celsius")
    @classmethod
    def validate_temp_range(cls, v: float) -> float:
        if v < -200 or v > 60:
            raise ValueError(f"Temperature {v}C is outside plausible range (-200C to +60C).")
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

    @field_validator("total_units")
    @classmethod
    def validate_total_units(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and v <= 0:
            raise ValueError("total_units must be a positive integer.")
        return v

    @field_validator("total_weight_kg")
    @classmethod
    def validate_weight(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and v <= 0:
            raise ValueError("total_weight_kg must be a positive number.")
        return v

    class Config:
        use_enum_values = True

    def to_firestore_dict(self) -> dict:
        """Return a plain dict suitable for Firestore. Enums serialised to strings."""
        return self.model_dump()
