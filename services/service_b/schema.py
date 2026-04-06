"""
Service B — Telemetry Ingestion API
Pydantic schema for incoming telemetry payloads.

This model is intentionally kept identical to Service C's TelemetryPayload so that
the JSON published to Pub/Sub can be deserialised by Service C without any
transformation. Any changes here must be mirrored in Service C's TelemetryPayload.

Valid drug_ids are fixed to the three shipments seeded by Service A:
  pfizer-001  — Pfizer COMIRNATY (ultra-cold, -90°C to -60°C)
  moderna-001 — Moderna Spikevax (deep-frozen, -50°C to -15°C)
  jynneos-001 — JYNNEOS Smallpox & Monkeypox Vaccine (deep-frozen, -25°C to -15°C)

Valid flight_delay_status values map to minutes in both Service B and C:
  on_time    →   0 minutes
  delayed_2h → 120 minutes
  delayed_6h → 360 minutes
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

VALID_DRUG_IDS: frozenset[str] = frozenset({
    "pfizer-001",
    "moderna-001",
    "jynneos-001",
})

VALID_FLIGHT_DELAY_STATUSES: frozenset[str] = frozenset({
    "on_time",
    "delayed_2h",
    "delayed_6h",
})


class TelemetryPayload(BaseModel):
    """
    Telemetry reading sent by the UI simulator.
    Mirrors Service C's TelemetryPayload exactly — do not add or remove fields
    without updating Service C.
    """
    drug_id:             str   = Field(..., description="Shipment identifier — must match a seeded Firestore document")
    temperature_celsius: float = Field(..., description="Current temperature reading in Celsius")
    humidity_percent:    float = Field(..., description="Current relative humidity in percent")
    shock_g:             float = Field(..., description="Current shock reading in G-force")
    flight_delay_status: str   = Field(default="on_time", description="on_time | delayed_2h | delayed_6h")
    timestamp:           str   = Field(..., description="ISO 8601 UTC timestamp of the reading")
    excursion_minutes:   int   = Field(default=0, description="Cumulative minutes outside temperature range, tracked by the UI")

    @field_validator("drug_id")
    @classmethod
    def validate_drug_id(cls, v: str) -> str:
        if v not in VALID_DRUG_IDS:
            raise ValueError(
                f"Unknown drug_id '{v}'. Must be one of: {sorted(VALID_DRUG_IDS)}"
            )
        return v

    @field_validator("flight_delay_status")
    @classmethod
    def validate_flight_delay_status(cls, v: str) -> str:
        if v not in VALID_FLIGHT_DELAY_STATUSES:
            raise ValueError(
                f"Unknown flight_delay_status '{v}'. Must be one of: {sorted(VALID_FLIGHT_DELAY_STATUSES)}"
            )
        return v

    @field_validator("temperature_celsius")
    @classmethod
    def validate_temperature(cls, v: float) -> float:
        if v < -200 or v > 60:
            raise ValueError(f"temperature_celsius {v} is outside plausible range (-200 to +60)")
        return v

    @field_validator("humidity_percent")
    @classmethod
    def validate_humidity(cls, v: float) -> float:
        if v < 0 or v > 100:
            raise ValueError(f"humidity_percent {v} must be between 0 and 100")
        return v

    @field_validator("shock_g")
    @classmethod
    def validate_shock(cls, v: float) -> float:
        if v < 0:
            raise ValueError(f"shock_g {v} cannot be negative")
        return v

    @field_validator("excursion_minutes")
    @classmethod
    def validate_excursion(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"excursion_minutes {v} cannot be negative")
        return v
