"""
Service E — Execution Agent
FastAPI microservice. Receives Pub/Sub PUSH from execute-actions,
generates notification content via Claude, executes all three notification
channels (email, SMS, voice), writes to BigQuery audit log, and updates
the Firestore approval status.

Pub/Sub push envelope (identical structure to Service C):
{
  "message": { "data": "<base64 JSON>", "messageId": "...", "publishTime": "..." },
  "subscription": "projects/.../subscriptions/execute-actions-sub"
}

Expected decoded payload (published by Service D after human approval):
{
  "approval_id":     "uuid",
  "approved_by":     "operator name",
  "approved_at":     "2026-04-05T10:30:00Z",
  "drug_id":         "pfizer-001",
  "drug_name":       "COMIRNATY (BNT162b2)",
  "manufacturer":    "Pfizer-BioNTech",
  "cargo_category":  "vaccine",
  "risk_level":      "CRITICAL",
  "overall_assessment": "...",
  "breaches":        [...],
  "compound_risk_note": "...",
  "recommended_actions": [...],
  "mitigation_plan": "Service D's generated recovery plan",
  "spoilage_likelihood": "PROBABLE",
  "estimated_viable_units_percent": 45,
  "regulatory_flags": [...],
  "contact_email":   "operator@example.com",
  "contact_phone":   "+12025551234",
  "telemetry":       {...},
  "thresholds":      {...},
  "iata_codes":      [...],
  "regulatory_framework": "EU GDP 2013/C 343/01"
}

Note: Service D is not yet implemented. All payload fields have defaults so
Service E can be tested by manually publishing to execute-actions before
Service D is built. See the testing section of the setup guide.

Environment variables (set at gcloud run deploy time via --set-env-vars):
  GOOGLE_CLOUD_PROJECT
  FIRESTORE_DATABASE       (default: cargo-monitor)
  ANTHROPIC_API_KEY
  SENDGRID_API_KEY
  SENDGRID_FROM_EMAIL      (default: alerts@agenticterps.com)
  TWILIO_ACCOUNT_SID
  TWILIO_AUTH_TOKEN
  TWILIO_PHONE_NUMBER
  ELEVENLABS_API_KEY
  ELEVENLABS_VOICE_ID      (default: 21m00Tcm4TlvDq8ikWAM = Rachel)
  VOICE_NOTES_BUCKET       (default: {PROJECT_ID}-voice-notes)

Run locally with:
  uvicorn main:app --reload --port 8080
"""

from dotenv import load_dotenv
load_dotenv()

import base64
import json
import logging
import sys
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import content_gen
import notifications
import compliance

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("service_e")

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(
    title="AgenticTerps — Service E: Execution Agent",
    description=(
        "Receives approved risk events from execute-actions Pub/Sub topic, "
        "generates notification content via Claude, and executes email, SMS, "
        "and voice notifications to the responsible party."
    ),
    version="1.0.0",
)

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class PubSubMessage(BaseModel):
    data:        str
    messageId:   Optional[str] = None
    publishTime: Optional[str] = None
    attributes:  Optional[dict] = None

class PubSubEnvelope(BaseModel):
    message:      PubSubMessage
    subscription: Optional[str] = None

class ExecutePayload(BaseModel):
    """
    Decoded execute-actions message. All fields have defaults because
    Service D is not yet implemented — tests may send partial payloads.
    """
    approval_id:                    str   = "unknown"
    approved_by:                    str   = "operator"
    approved_at:                    str   = ""
    drug_id:                        str   = "unknown"
    drug_name:                      str   = ""
    manufacturer:                   str   = ""
    cargo_category:                 str   = ""
    risk_level:                     str   = "CRITICAL"
    overall_assessment:             str   = ""
    breaches:                       list  = []
    compound_risk_note:             Optional[str] = None
    recommended_actions:            list  = []
    mitigation_plan:                str   = ""
    spoilage_likelihood:            str   = ""
    estimated_viable_units_percent: int   = 0
    regulatory_flags:               list  = []
    contact_email:                  str   = ""
    contact_phone:                  str   = ""
    telemetry:                      dict  = {}
    thresholds:                     dict  = {}
    iata_codes:                     list  = []
    regulatory_framework:           str   = ""
    special_instructions:           Optional[str] = None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health", tags=["System"])
async def health():
    return {"status": "ok", "service": "service_e_execution_agent"}


@app.post("/pubsub/execute", tags=["Pub/Sub"])
async def execute_actions(envelope: PubSubEnvelope, request: Request):
    """
    Pub/Sub push endpoint for the execute-actions topic.

    Must return HTTP 2xx to ACK the message. Non-2xx triggers redelivery.
    All errors are caught and ACK'd (with logged details) to prevent
    infinite redelivery of messages that cannot succeed (e.g. missing contacts).

    Pipeline:
      1. Decode and validate the Pub/Sub message
      2. Generate email + SMS + voice content via Claude (single call)
      3. Execute all three notification channels independently
      4. Write audit record to BigQuery
      5. Update Firestore approval status
      6. Return 200 ACK with execution summary
    """
    started_at = datetime.now(timezone.utc).isoformat()

    # ------------------------------------------------------------------
    # 1. Decode Pub/Sub base64 message
    # ------------------------------------------------------------------
    try:
        raw_data     = base64.b64decode(envelope.message.data).decode("utf-8")
        payload_dict = json.loads(raw_data)
        payload      = ExecutePayload(**payload_dict)
    except Exception as exc:
        logger.error("Failed to decode Pub/Sub execute-actions message: %s", exc)
        # ACK malformed messages — retrying won't fix a bad payload
        return JSONResponse(
            status_code=200,
            content={"error": "bad_message", "detail": str(exc)},
        )

    logger.info(
        "Received execute-actions | approval_id=%s | drug_id=%s | "
        "risk=%s | approved_by=%s",
        payload.approval_id,
        payload.drug_id,
        payload.risk_level,
        payload.approved_by,
    )

    payload_dict = payload.model_dump()

    # ------------------------------------------------------------------
    # 2. Generate notification content via Claude
    # ------------------------------------------------------------------
    try:
        content = content_gen.generate_notification_content(payload_dict)
    except Exception as exc:
        logger.error(
            "Content generation failed | drug_id=%s | error=%s",
            payload.drug_id, exc,
        )
        # ACK the message — Claude failure shouldn't block the pipeline indefinitely.
        # The BigQuery audit log will record the failure for traceability.
        content = {
            "email_subject": f"[{payload.risk_level}] Cold Chain Alert: {payload.drug_name or payload.drug_id}",
            "email_body":    (
                f"A {payload.risk_level} risk event has been approved for action.\n\n"
                f"Drug: {payload.drug_name or payload.drug_id}\n"
                f"Assessment: {payload.overall_assessment}\n\n"
                f"Please review the monitoring dashboard immediately."
            ),
            "sms_text":     f"[{payload.risk_level}] Cold chain alert: {payload.drug_name or payload.drug_id}. Check dashboard.",
            "voice_script": f"Urgent alert. Cold chain risk confirmed for {payload.drug_name or payload.drug_id}. Immediate action required.",
        }
        logger.warning(
            "Using fallback notification content | drug_id=%s",
            payload.drug_id,
        )

    # ------------------------------------------------------------------
    # 3. Execute all three notification channels independently
    # ------------------------------------------------------------------
    notification_results = notifications.execute_all_channels(
        payload=payload_dict,
        content=content,
    )

    # ------------------------------------------------------------------
    # 4. Write audit record to BigQuery
    # ------------------------------------------------------------------
    audit_result = compliance.write_audit_log(
        payload=payload_dict,
        notification_results=notification_results,
    )

    # ------------------------------------------------------------------
    # 5. Update Firestore approval status
    # ------------------------------------------------------------------
    firestore_result = compliance.update_approval_status(
        approval_id=payload.approval_id,
        notification_results=notification_results,
    )

    # ------------------------------------------------------------------
    # 6. Build and return execution summary
    # ------------------------------------------------------------------
    channels_succeeded = [
        ch for ch, res in notification_results.items()
        if res.get("success")
    ]
    channels_failed = [
        ch for ch, res in notification_results.items()
        if not res.get("success")
    ]

    logger.info(
        "Execution complete | drug_id=%s | approval_id=%s | "
        "succeeded=%s | failed=%s | audit=%s | firestore=%s",
        payload.drug_id,
        payload.approval_id,
        channels_succeeded,
        channels_failed,
        audit_result.get("success"),
        firestore_result.get("success"),
    )

    return JSONResponse(
        status_code=200,
        content={
            "drug_id":              payload.drug_id,
            "approval_id":          payload.approval_id,
            "risk_level":           payload.risk_level,
            "channels_succeeded":   channels_succeeded,
            "channels_failed":      channels_failed,
            "audit_log_written":    audit_result.get("success", False),
            "firestore_updated":    firestore_result.get("success", False),
            "started_at":           started_at,
            "completed_at":         datetime.now(timezone.utc).isoformat(),
        },
    )


# ---------------------------------------------------------------------------
# Local dev entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import os
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8080)),
        reload=False,
    )
