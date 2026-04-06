"""
Service B — Telemetry Ingestion API
Cloud Function entry point.

Triggered by HTTP POST from the UI simulator (browser).
Accepts a telemetry reading, validates it, writes live state to Firestore,
publishes to the telemetry-stream Pub/Sub topic, and returns a 200 ACK.

Environment variables (set at deploy time via --set-env-vars):
  GOOGLE_CLOUD_PROJECT   GCP project ID
  FIRESTORE_DATABASE     Firestore database name (default: cargo-monitor)
  TELEMETRY_TOPIC        Pub/Sub topic name (default: telemetry-stream)

Authentication:
  The function runs as the service-b-telemetry service account (set via
  --service-account at deploy time). ADC resolves automatically — no
  credentials code is needed.

CORS:
  The UI simulator runs in the browser (localhost or a deployed frontend).
  CORS headers are required on every response including the OPTIONS preflight.
  The Access-Control-Allow-Origin header is set from the ALLOWED_ORIGIN env var
  (defaults to * for local development — restrict to your frontend URL in prod).

Request body (JSON):
  {
    "drug_id":             "pfizer-001" | "moderna-001" | "jynneos-001",
    "temperature_celsius": float,
    "humidity_percent":    float,
    "shock_g":             float,
    "flight_delay_status": "on_time" | "delayed_2h" | "delayed_6h",
    "timestamp":           "2026-04-05T10:00:00Z",
    "excursion_minutes":   int   (default 0, tracked and sent by the UI)
  }

Response (200):
  {
    "status":     "published",
    "drug_id":    "pfizer-001",
    "message_id": "<pub/sub message id>"
  }

Error responses:
  400 — invalid or missing fields in the request body
  404 — drug_id not found in Firestore
  500 — Pub/Sub or Firestore infrastructure failure
"""

import json
import logging
import os
import sys

import functions_framework
from pydantic import ValidationError

from schema import TelemetryPayload
import firestore as fs
import pubsub

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("service_b")

# ---------------------------------------------------------------------------
# CORS — restrict ALLOWED_ORIGIN to your frontend URL in production
# ---------------------------------------------------------------------------
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "*")

CORS_HEADERS = {
    "Access-Control-Allow-Origin":  ALLOWED_ORIGIN,
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age":       "3600",
}


def _cors_response(status: int, body: dict) -> tuple:
    """Return a (response_body, status_code, headers) tuple with CORS headers."""
    return (json.dumps(body), status, {**CORS_HEADERS, "Content-Type": "application/json"})


# ---------------------------------------------------------------------------
# Cloud Function entry point
# ---------------------------------------------------------------------------

@functions_framework.http
def ingest_telemetry(request):
    """
    HTTP Cloud Function — Telemetry Ingestion API (Service B).

    Pipeline:
      1. Handle CORS preflight
      2. Parse and validate request body
      3. Confirm shipment exists in Firestore
      4. Write live_telemetry state to Firestore (dashboard real-time update)
      5. Publish validated payload to telemetry-stream Pub/Sub topic
      6. Return 200 ACK
    """

    # ------------------------------------------------------------------
    # 1. CORS preflight — browsers send OPTIONS before the real POST
    # ------------------------------------------------------------------
    if request.method == "OPTIONS":
        return ("", 204, CORS_HEADERS)

    if request.method != "POST":
        return _cors_response(405, {"error": "method_not_allowed", "detail": "Use POST"})

    # ------------------------------------------------------------------
    # 2. Parse and validate request body
    # ------------------------------------------------------------------
    try:
        body = request.get_json(silent=True)
        if body is None:
            return _cors_response(400, {
                "error": "invalid_body",
                "detail": "Request body must be valid JSON with Content-Type: application/json",
            })

        payload = TelemetryPayload(**body)

    except ValidationError as exc:
        errors = exc.errors()
        logger.warning("Validation failed: %s", errors)
        return _cors_response(400, {
            "error":   "validation_error",
            "detail":  "One or more fields failed validation",
            "errors":  [{"field": e["loc"], "message": e["msg"]} for e in errors],
        })

    except Exception as exc:
        logger.error("Unexpected parse error: %s", exc)
        return _cors_response(400, {"error": "parse_error", "detail": str(exc)})

    drug_id = payload.drug_id
    logger.info(
        "Received telemetry | drug_id=%s | temp=%.1f°C | humidity=%.1f%% | "
        "shock=%.1fG | delay=%s | excursion=%dmin | ts=%s",
        drug_id,
        payload.temperature_celsius,
        payload.humidity_percent,
        payload.shock_g,
        payload.flight_delay_status,
        payload.excursion_minutes,
        payload.timestamp,
    )

    # ------------------------------------------------------------------
    # 3. Confirm shipment exists in Firestore
    #    Prevents phantom drug_ids from polluting the Pub/Sub stream.
    # ------------------------------------------------------------------
    try:
        if not fs.shipment_exists(drug_id):
            logger.warning("Shipment not found in Firestore: %s", drug_id)
            return _cors_response(404, {
                "error":  "shipment_not_found",
                "detail": f"No shipment document found for drug_id '{drug_id}'",
            })
    except Exception as exc:
        logger.error("Firestore existence check failed: %s", exc)
        return _cors_response(500, {
            "error":  "firestore_error",
            "detail": f"Could not verify shipment: {exc}",
        })

    # ------------------------------------------------------------------
    # 4. Write live_telemetry to Firestore so the dashboard updates in
    #    real-time via onSnapshot
    # ------------------------------------------------------------------
    try:
        fs.write_live_telemetry(payload.model_dump())
    except Exception as exc:
        logger.error("Firestore live_telemetry write failed: %s", exc)
        return _cors_response(500, {
            "error":  "firestore_write_error",
            "detail": f"Failed to update live telemetry state: {exc}",
        })

    # ------------------------------------------------------------------
    # 5. Publish validated payload to telemetry-stream
    #    .result() inside publish_telemetry blocks until confirmed
    # ------------------------------------------------------------------
    try:
        message_id = pubsub.publish_telemetry(payload.model_dump())
    except Exception as exc:
        logger.error("Pub/Sub publish failed: %s", exc)
        return _cors_response(500, {
            "error":  "pubsub_error",
            "detail": f"Failed to publish telemetry event: {exc}",
        })

    # ------------------------------------------------------------------
    # 6. Return ACK
    # ------------------------------------------------------------------
    return _cors_response(200, {
        "status":     "published",
        "drug_id":    drug_id,
        "message_id": message_id,
    })
