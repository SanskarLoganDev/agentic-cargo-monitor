"""
Service C — Monitoring & Anomaly Agent
FastAPI microservice. Receives Pub/Sub PUSH from telemetry-stream,
fetches Firestore shipment metadata, asks Claude to evaluate all
thresholds, and publishes a rich payload to risk-detected if risk found.

Pub/Sub push envelope:
{
  "message": { "data": "<base64 JSON>", "messageId": "...", "publishTime": "..." },
  "subscription": "projects/.../subscriptions/telemetry-stream-sub"
}

Expected decoded telemetry payload (from Service B):
{
  "drug_id":             "pfizer-001",
  "temperature_celsius": -72.5,
  "humidity_percent":    68.0,
  "shock_g":             3.2,
  "flight_delay_status": "on_time",
  "timestamp":           "2026-04-04T10:00:00Z",
  "excursion_minutes":   0
}
"""

from dotenv import load_dotenv
load_dotenv()

import base64
import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Optional

import anthropic
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from google.cloud import firestore, pubsub_v1
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("service_c")

# ---------------------------------------------------------------------------
# Config — injected as Cloud Run environment variables
# ---------------------------------------------------------------------------
PROJECT_ID        = os.environ["GOOGLE_CLOUD_PROJECT"]
FIRESTORE_DB      = os.environ.get("FIRESTORE_DATABASE", "cargo-monitor")
RISK_TOPIC        = os.environ.get("RISK_TOPIC", "risk-detected")
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
CLAUDE_MODEL      = "claude-haiku-4-5-20251001"

FLIGHT_DELAY_MINUTES: dict[str, int] = {
    "on_time":    0,
    "delayed_2h": 120,
    "delayed_6h": 360,
}

# ---------------------------------------------------------------------------
# GCP clients — Cloud Run provides ADC automatically via the attached SA
# ---------------------------------------------------------------------------
db         = firestore.Client(project=PROJECT_ID, database=FIRESTORE_DB)
publisher  = pubsub_v1.PublisherClient()
topic_path = publisher.topic_path(PROJECT_ID, RISK_TOPIC)
claude     = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(
    title="AgenticTerps – Service C: Monitoring & Anomaly Agent",
    version="1.0.0",
)

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class TelemetryPayload(BaseModel):
    drug_id:             str
    temperature_celsius: float
    humidity_percent:    float
    shock_g:             float
    flight_delay_status: str = "on_time"
    timestamp:           str
    excursion_minutes:   int = 0

class PubSubMessage(BaseModel):
    data:        str
    messageId:   Optional[str] = None
    publishTime: Optional[str] = None
    attributes:  Optional[dict] = None

class PubSubEnvelope(BaseModel):
    message:      PubSubMessage
    subscription: Optional[str] = None

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fetch_shipment(drug_id: str) -> dict:
    """Fetch shipment metadata document from Firestore /shipments/{drug_id}."""
    doc = db.collection("shipments").document(drug_id).get()
    if not doc.exists:
        raise ValueError(f"Shipment document '{drug_id}' not found in Firestore.")
    return doc.to_dict()


def build_threshold_summary(meta: dict) -> str:
    """Compact human-readable threshold summary injected into the Claude prompt."""
    return f"""
SHIPMENT METADATA — {meta.get('drug_name', 'UNKNOWN')}
Manufacturer    : {meta.get('manufacturer', 'N/A')}
Cargo Category  : {meta.get('cargo_category', 'N/A')}
Temp Class      : {meta.get('temp_classification', 'N/A')}

=== THRESHOLDS ===
Temperature     : {meta.get('temp_min_celsius')}°C  to  {meta.get('temp_max_celsius')}°C
Max Excursion   : {meta.get('max_excursion_duration_minutes')} minutes cumulative out-of-range
Do Not Freeze   : {meta.get('do_not_freeze', False)}  (freeze threshold: {meta.get('freeze_threshold_celsius', 'N/A')}°C)
Humidity        : max {meta.get('max_humidity_percent')}% RH
Shock           : max {meta.get('max_shock_g')} G
Flight Delay    : max {meta.get('max_flight_delay_minutes')} minutes

=== STABILITY ===
Thaw Window     : {meta.get('thaw_window_hours', 'N/A')} hours post-thaw at 2–8°C
Stability Note  : {meta.get('stability_note', 'N/A')}
Shelf Life      : {meta.get('shelf_life_days', 'N/A')} days

=== ALERT MESSAGES ===
Humidity Alert  : {meta.get('humidity_alert_message', 'N/A')}
Shock Alert     : {meta.get('shock_alert_message', 'N/A')}
Delay Note      : {meta.get('flight_delay_spoilage_note', 'N/A')}

=== CONTACTS ===
Email  : {meta.get('contact_email', 'N/A')}
Phone  : {meta.get('contact_phone', 'N/A')}

=== REGULATORY ===
Framework       : {meta.get('regulatory_framework', 'N/A')}
IATA Codes      : {', '.join(meta.get('iata_handling_codes', []))}
Special Instrs  : {meta.get('special_instructions', 'N/A')}
""".strip()


def call_claude(telemetry: TelemetryPayload, meta: dict, delay_minutes: int) -> dict:
    """
    Ask Claude to evaluate all readings against all thresholds.
    Returns a parsed dict with the full risk assessment.
    """
    threshold_summary = build_threshold_summary(meta)

    prompt = f"""You are a pharmaceutical cold-chain risk analyst AI embedded in a real-time cargo monitoring system.

You have received a LIVE telemetry reading for a pharmaceutical shipment. Your job is to:
1. Compare every reading against the shipment thresholds below
2. Determine if a risk exists — be conservative, patient safety is paramount
3. Produce a structured JSON risk report

---
{threshold_summary}
---

LIVE TELEMETRY READING:
  Drug ID              : {telemetry.drug_id}
  Temperature          : {telemetry.temperature_celsius}°C
  Humidity             : {telemetry.humidity_percent}% RH
  Shock                : {telemetry.shock_g} G
  Flight Delay Status  : {telemetry.flight_delay_status} ({delay_minutes} minutes)
  Cumulative Excursion : {telemetry.excursion_minutes} minutes
  Timestamp            : {telemetry.timestamp}

---

EVALUATION RULES:
- Temperature breach  : reading outside [temp_min_celsius, temp_max_celsius]
- Excursion breach    : excursion_minutes > max_excursion_duration_minutes
- Freeze damage       : do_not_freeze=True AND temperature_celsius < freeze_threshold_celsius
- Humidity breach     : humidity_percent > max_humidity_percent
- Shock breach        : shock_g > max_shock_g
- Flight delay breach : delay_minutes >= max_flight_delay_minutes

For each triggered breach, calculate the exact deviation from threshold.
Use your pharmaceutical expertise to assess compounding risk when multiple parameters breach simultaneously.
Consider the drug's stability profile (thaw_window_hours, stability_note) when estimating viability.

Respond ONLY with a valid JSON object — no markdown, no explanation, just the JSON:
{{
  "risk_detected": true | false,
  "risk_level": "CRITICAL" | "HIGH" | "MEDIUM" | "LOW" | "NONE",
  "overall_assessment": "<2-3 sentence plain-English summary for a human operator>",
  "breaches": [
    {{
      "parameter": "<temperature|humidity|shock|flight_delay|excursion|freeze>",
      "reading": <numeric or string>,
      "threshold": <numeric>,
      "deviation": "<e.g. +12.5°C above max>",
      "severity": "CRITICAL" | "HIGH" | "MEDIUM" | "LOW",
      "message": "<specific alert message for this breach>"
    }}
  ],
  "compound_risk_note": "<Explain compounding effects if multiple breaches. null if single or no breach.>",
  "recommended_actions": [
    "<Specific actionable step 1>",
    "<Specific actionable step 2>",
    "<Specific actionable step 3>"
  ],
  "spoilage_likelihood": "CONFIRMED" | "PROBABLE" | "POSSIBLE" | "UNLIKELY" | "NONE",
  "estimated_viable_units_percent": <0-100>,
  "regulatory_flags": ["<Any GDP/IATA regulatory implications>"],
  "drug_name": "{meta.get('drug_name', telemetry.drug_id)}",
  "manufacturer": "{meta.get('manufacturer', 'N/A')}",
  "contact_email": "{meta.get('contact_email', '')}",
  "contact_phone": "{meta.get('contact_phone', '')}"
}}

If risk_detected is false, set: risk_level="NONE", breaches=[], spoilage_likelihood="NONE", estimated_viable_units_percent=100.
"""

    response = claude.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1800,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text.strip()

    # Strip markdown code fences if Claude wraps the JSON anyway
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else raw
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    return json.loads(raw)


def publish_risk_event(
    drug_id: str,
    telemetry: TelemetryPayload,
    meta: dict,
    assessment: dict,
) -> str:
    """
    Build and publish the complete risk payload to the risk-detected topic.
    Service D (Orchestrator) receives this and needs everything to build
    a recovery plan WITHOUT querying Firestore itself.
    """
    payload = {
        # ── Identity ──────────────────────────────────────────────────────
        "drug_id":        drug_id,
        "drug_name":      assessment.get("drug_name", meta.get("drug_name", drug_id)),
        "manufacturer":   assessment.get("manufacturer", meta.get("manufacturer", "N/A")),
        "cargo_category": meta.get("cargo_category", "N/A"),

        # ── Live reading snapshot ─────────────────────────────────────────
        "telemetry": {
            "temperature_celsius":  telemetry.temperature_celsius,
            "humidity_percent":     telemetry.humidity_percent,
            "shock_g":              telemetry.shock_g,
            "flight_delay_status":  telemetry.flight_delay_status,
            "excursion_minutes":    telemetry.excursion_minutes,
            "timestamp":            telemetry.timestamp,
        },

        # ── Full thresholds — Service D uses these for tool calculations ──
        "thresholds": {
            "temp_min_celsius":               meta.get("temp_min_celsius"),
            "temp_max_celsius":               meta.get("temp_max_celsius"),
            "max_excursion_duration_minutes": meta.get("max_excursion_duration_minutes"),
            "max_humidity_percent":           meta.get("max_humidity_percent"),
            "max_shock_g":                    meta.get("max_shock_g"),
            "max_flight_delay_minutes":       meta.get("max_flight_delay_minutes"),
            "do_not_freeze":                  meta.get("do_not_freeze", False),
            "freeze_threshold_celsius":       meta.get("freeze_threshold_celsius"),
            "thaw_window_hours":              meta.get("thaw_window_hours"),
            "stability_note":                 meta.get("stability_note"),
            "flight_delay_spoilage_note":     meta.get("flight_delay_spoilage_note"),
            "humidity_alert_message":         meta.get("humidity_alert_message"),
            "shock_alert_message":            meta.get("shock_alert_message"),
        },

        # ── Claude's full risk assessment ─────────────────────────────────
        "risk_level":                     assessment.get("risk_level"),
        "overall_assessment":             assessment.get("overall_assessment"),
        "breaches":                       assessment.get("breaches", []),
        "compound_risk_note":             assessment.get("compound_risk_note"),
        "recommended_actions":            assessment.get("recommended_actions", []),
        "spoilage_likelihood":            assessment.get("spoilage_likelihood"),
        "estimated_viable_units_percent": assessment.get("estimated_viable_units_percent"),
        "regulatory_flags":               assessment.get("regulatory_flags", []),

        # ── Contacts — Service E reads these to send notifications ────────
        "contact_email": meta.get("contact_email", ""),
        "contact_phone": meta.get("contact_phone", ""),

        # ── Provenance ────────────────────────────────────────────────────
        "source_service":        "service_c",
        "detected_at":           datetime.now(timezone.utc).isoformat(),
        "iata_codes":            meta.get("iata_handling_codes", []),
        "regulatory_framework":  meta.get("regulatory_framework", "N/A"),
        "special_instructions":  meta.get("special_instructions"),
    }

    message_bytes = json.dumps(payload).encode("utf-8")
    future = publisher.publish(
        topic_path,
        data=message_bytes,
        drug_id=drug_id,
        risk_level=assessment.get("risk_level", "UNKNOWN"),
        source="service_c",
    )
    message_id = future.result(timeout=10)
    logger.info(
        "Published to risk-detected | message_id=%s | drug_id=%s | risk=%s",
        message_id, drug_id, assessment.get("risk_level"),
    )
    return message_id


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "service": "service_c_monitoring_agent"}


@app.post("/pubsub/telemetry")
async def receive_telemetry(envelope: PubSubEnvelope, request: Request):
    """
    Pub/Sub push endpoint.
    Must return HTTP 2xx to ACK the message. Non-2xx triggers redelivery.
    """
    # Decode the base64 Pub/Sub data field
    try:
        raw_data = base64.b64decode(envelope.message.data).decode("utf-8")
        telemetry_dict = json.loads(raw_data)
        telemetry = TelemetryPayload(**telemetry_dict)
    except Exception as exc:
        logger.error("Failed to decode Pub/Sub message: %s", exc)
        # ACK bad messages so they don't clog the subscription
        return JSONResponse(
            status_code=200,
            content={"error": "bad_message", "detail": str(exc)},
        )

    drug_id = telemetry.drug_id
    logger.info(
        "Received telemetry | drug_id=%s | temp=%.1f°C | humidity=%.1f%% | shock=%.1fG | delay=%s | excursion=%dmin",
        drug_id, telemetry.temperature_celsius, telemetry.humidity_percent,
        telemetry.shock_g, telemetry.flight_delay_status, telemetry.excursion_minutes,
    )

    # Fetch shipment metadata from Firestore
    try:
        meta = fetch_shipment(drug_id)
    except ValueError as exc:
        logger.error("Firestore lookup failed: %s", exc)
        return JSONResponse(
            status_code=200,
            content={"error": "shipment_not_found", "detail": str(exc)},
        )

    delay_minutes = FLIGHT_DELAY_MINUTES.get(telemetry.flight_delay_status, 0)

    # Claude risk evaluation
    try:
        assessment = call_claude(telemetry, meta, delay_minutes)
    except json.JSONDecodeError as exc:
        logger.error("Claude returned non-JSON: %s", exc)
        raise HTTPException(status_code=500, detail=f"Claude JSON parse error: {exc}")
    except Exception as exc:
        logger.error("Claude evaluation failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Claude evaluation error: {exc}")

    risk_detected = assessment.get("risk_detected", False)
    risk_level    = assessment.get("risk_level", "NONE")

    logger.info(
        "Claude assessment | drug_id=%s | risk=%s | level=%s | spoilage=%s | breaches=%d",
        drug_id, risk_detected, risk_level,
        assessment.get("spoilage_likelihood"),
        len(assessment.get("breaches", [])),
    )

    # Publish to risk-detected only if Claude says risk exists
    message_id = None
    if risk_detected:
        try:
            message_id = publish_risk_event(drug_id, telemetry, meta, assessment)
        except Exception as exc:
            logger.error("Failed to publish to risk-detected: %s", exc)
            raise HTTPException(status_code=500, detail=f"Pub/Sub publish error: {exc}")

    return JSONResponse(
        status_code=200,
        content={
            "drug_id":       drug_id,
            "risk_detected": risk_detected,
            "risk_level":    risk_level,
            "message_id":    message_id,
            "breaches":      len(assessment.get("breaches", [])),
        },
    )


# ---------------------------------------------------------------------------
# Local dev entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8080)),
        reload=False,
    )