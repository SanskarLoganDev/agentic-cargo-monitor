"""
Service B — Telemetry Ingestion API
Firestore helper.

Two responsibilities:
  1. Validate that a drug_id has a corresponding shipment document in Firestore
     before publishing to Pub/Sub — prevents garbage messages reaching Service C.
  2. Write the latest telemetry reading back to the shipment document as a
     live_telemetry map field so the React dashboard can update dials in
     real-time via Firestore onSnapshot listeners.

The Firestore client is initialised at module level so it is reused across
warm Cloud Function instances.

Write strategy:
  Uses set(..., merge=True) to update only the live_telemetry field.
  This is critical — a plain set() would overwrite the entire shipment
  document including all the parameters seeded by Service A.

Firestore document path:
  /shipments/{drug_id}

live_telemetry map written:
  {
    "temperature_celsius":  float,
    "humidity_percent":     float,
    "shock_g":              float,
    "flight_delay_status":  str,
    "excursion_minutes":    int,
    "last_updated":         SERVER_TIMESTAMP
  }
"""

import logging
import os

from google.cloud import firestore

logger = logging.getLogger(__name__)

FIRESTORE_DB         = os.environ.get("FIRESTORE_DATABASE", "cargo-monitor")
FIRESTORE_COLLECTION = "shipments"

# Module-level client — reused across warm instances
_db = firestore.Client(
    project=os.environ["GOOGLE_CLOUD_PROJECT"],
    database=FIRESTORE_DB,
)


def shipment_exists(drug_id: str) -> bool:
    """
    Return True if /shipments/{drug_id} exists in Firestore.
    A lightweight existence check — does not fetch the full document body.
    """
    doc_ref = _db.collection(FIRESTORE_COLLECTION).document(drug_id)
    return doc_ref.get(field_paths=["drug_id"]).exists


def write_live_telemetry(payload: dict) -> None:
    """
    Merge the latest telemetry reading into the shipment document.

    Uses merge=True so only the live_telemetry field is updated.
    All other fields set by Service A's seed script remain untouched.

    Args:
        payload: Dict representation of a validated TelemetryPayload.
    """
    drug_id = payload["drug_id"]

    doc_ref = _db.collection(FIRESTORE_COLLECTION).document(drug_id)

    doc_ref.set(
        {
            "live_telemetry": {
                "temperature_celsius": payload["temperature_celsius"],
                "humidity_percent":    payload["humidity_percent"],
                "shock_g":             payload["shock_g"],
                "flight_delay_status": payload["flight_delay_status"],
                "excursion_minutes":   payload["excursion_minutes"],
                "last_updated":        firestore.SERVER_TIMESTAMP,
            }
        },
        merge=True,
    )

    logger.info(
        "Firestore live_telemetry updated | drug_id=%s | temp=%.1f°C | "
        "humidity=%.1f%% | shock=%.1fG | delay=%s | excursion=%dmin",
        drug_id,
        payload["temperature_celsius"],
        payload["humidity_percent"],
        payload["shock_g"],
        payload["flight_delay_status"],
        payload["excursion_minutes"],
    )
