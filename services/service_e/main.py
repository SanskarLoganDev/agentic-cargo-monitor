"""
Service E — Execution Agent
FastAPI microservice. Receives Pub/Sub PUSH from execute-actions,
iterates over the recovery_actions list produced by Service D, sends emails
for actions that have pre-written recipient/subject/body, delivers a voice
notification to the primary contact, writes to BigQuery, and marks the
Firestore document as completed.

Pub/Sub push envelope:
{
  "message": { "data": "<base64 JSON>", "messageId": "...", "publishTime": "..." },
  "subscription": "projects/.../subscriptions/execute-actions-sub"
}

Expected decoded payload (published by Service D after human approval):
{
  "drug_id":            "pfizer-001",
  "drug_name":          "COMIRNATY (BNT162b2)",
  "document_id":        "firestore-doc-id",   <- pending_approvals document to mark completed
  "approval_id":        "uuid",               <- fallback if document_id absent
  "approved_by":        "operator name",
  "risk_level":         "CRITICAL",
  "contact_phone":      "+12408798960",        <- fallback for voice notification
  "agent_summary":      "...",                 <- Service D narrative summary
  "spoilage_assessment": "...",               <- Service D spoilage analysis
  "recovery_actions": [
    {
      "step":            1,
      "action_type":     "QUARANTINE" | "CONTACT_CARRIER" | "NOTIFY_RECEIVER" |
                         "NOTIFY_MANUFACTURER" | "NOTIFY_SENDER" |
                         "SPOILAGE_ASSESSMENT" | "LOG_COMPLIANCE" | "POTENCY_TESTING",
      "title":           "...",
      "description":     "...",
      "recipient_name":  "..." | null,
      "recipient_email": "..." | null,         <- present on NOTIFY_* actions
      "recipient_phone": "..." | null,
      "email_subject":   "..." | null,         <- pre-written by Service D
      "email_body":      "..." | null,         <- pre-written by Service D
      "sms_body":        "..." | null,         <- ignored (SMS channel disabled)
      "urgency":         "CRITICAL" | "HIGH",
      "metadata":        { ... }
    },
    ...
  ]
}

Execution logic per action:
  - Has recipient_email + email_subject + email_body  → send email via Gmail SMTP
  - No email fields                                   → log as acknowledged (no action needed)
  - SMS channel is DISABLED — sms_body fields are ignored entirely

After all actions are processed:
  - Send voice notification (ElevenLabs TTS → GCS → Twilio call) to the primary phone
    found in recovery_actions (first action with a non-null recipient_phone), or
    the top-level contact_phone field.
  - Write BigQuery audit log
  - Mark Firestore pending_approvals/{document_id} as status="completed"

Environment variables (--env-vars-file services/service_e/.env.yaml):
  GOOGLE_CLOUD_PROJECT
  FIRESTORE_DATABASE       (default: cargo-monitor)
  GMAIL_USER
  GMAIL_APP_PASSWORD
  TWILIO_ACCOUNT_SID
  TWILIO_AUTH_TOKEN
  TWILIO_PHONE_NUMBER
  ELEVENLABS_API_KEY
  ELEVENLABS_VOICE_ID      (default: 21m00Tcm4TlvDq8ikWAM)
  VOICE_NOTES_BUCKET       (default: {PROJECT_ID}-voice-notes)
"""

from dotenv import load_dotenv
load_dotenv()

import base64
import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

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
        "Receives approved recovery plans from Service D via execute-actions "
        "Pub/Sub topic. Iterates over recovery_actions, sends emails where "
        "pre-written content is provided, delivers a voice notification to the "
        "primary contact, writes a BigQuery audit record, and marks the Firestore "
        "pending_approvals document as completed."
    ),
    version="2.0.0",
)

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class RecoveryAction(BaseModel):
    """A single step in the recovery plan produced by Service D."""
    step:            int
    action_type:     str
    title:           str
    description:     str
    recipient_name:  Optional[str] = None
    recipient_email: Optional[str] = None
    recipient_phone: Optional[str] = None
    email_subject:   Optional[str] = None
    email_body:      Optional[str] = None
    sms_body:        Optional[str] = None   # ignored — SMS channel disabled
    urgency:         str = "CRITICAL"
    metadata:        dict = {}

class ExecutePayload(BaseModel):
    """
    Decoded execute-actions message from Service D.
    Top-level identification fields are expected alongside the recovery_actions list.
    All have defaults so the service degrades gracefully during testing.
    """
    # ── Identification ───────────────────────────────────────────────────────
    drug_id:            str  = "unknown"
    drug_name:          str  = ""
    document_id:        str  = "unknown"   # Firestore pending_approvals doc ID
    approval_id:        str  = "unknown"   # fallback if document_id absent
    approved_by:        str  = "operator"
    risk_level:         str  = "CRITICAL"
    contact_phone:      str  = ""          # fallback for voice notification

    # ── Service D content ────────────────────────────────────────────────────
    agent_summary:      str  = ""
    spoilage_assessment: str = ""
    recovery_actions:   list[RecoveryAction] = []

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

def _find_primary_phone(payload: ExecutePayload) -> str:
    """
    Return the best phone number available for the voice notification.
    Preference: first recovery_action with a non-null recipient_phone,
    then the top-level contact_phone field.
    """
    for action in payload.recovery_actions:
        if action.recipient_phone:
            return action.recipient_phone
    return payload.contact_phone


def _build_voice_script(payload: ExecutePayload) -> str:
    """
    Build a short spoken voice script (≤25 words) for the ElevenLabs TTS call.
    Derived from the payload's risk level and drug name — no Claude call needed.
    """
    drug  = payload.drug_name or payload.drug_id
    level = payload.risk_level.capitalize()
    return (
        f"{level} pharmaceutical alert. Cold chain breach confirmed on {drug} shipment. "
        f"Recovery plan is now being executed. Immediate action required."
    )


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
    All errors are caught and ACK'd to prevent infinite redelivery.

    Pipeline:
      1. Decode and validate the Pub/Sub message
      2. Execute each recovery_action (email where content is provided, log the rest)
      3. Send voice notification to the primary contact phone
      4. Write audit record to BigQuery
      5. Mark Firestore pending_approvals document as completed
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
        return JSONResponse(
            status_code=200,
            content={"error": "bad_message", "detail": str(exc)},
        )

    # Resolve document_id — prefer approval_id, fall back to document_id
    doc_id = payload.approval_id if payload.approval_id != "unknown" else payload.document_id

    logger.info(
        "Received execute-actions | drug_id=%s | doc_id=%s | risk=%s | "
        "approved_by=%s | actions=%d",
        payload.drug_id,
        doc_id,
        payload.risk_level,
        payload.approved_by,
        len(payload.recovery_actions),
    )

    # ------------------------------------------------------------------
    # 2. Execute each recovery action
    # ------------------------------------------------------------------
    action_results = notifications.execute_recovery_actions(payload.recovery_actions)

    email_count  = sum(1 for r in action_results if r.get("channel") == "email" and r.get("success"))
    logged_count = sum(1 for r in action_results if r.get("channel") == "logged")

    logger.info(
        "Recovery actions complete | emails_sent=%d | logged=%d | total=%d",
        email_count, logged_count, len(action_results),
    )

    # ------------------------------------------------------------------
    # 3. Send voice notification to the primary contact
    # ------------------------------------------------------------------
    primary_phone = _find_primary_phone(payload)
    voice_script  = _build_voice_script(payload)
    voice_result  = notifications.send_voice_notification(primary_phone, voice_script)

    # ------------------------------------------------------------------
    # 4. Write audit record to BigQuery
    # ------------------------------------------------------------------
    audit_result = compliance.write_audit_log(
        payload=payload.model_dump(),
        action_results=action_results,
        voice_result=voice_result,
    )

    # ------------------------------------------------------------------
    # 5. Mark Firestore document as completed
    # ------------------------------------------------------------------
    firestore_result = compliance.mark_completed(
        document_id=doc_id,
        action_results=action_results,
    )

    # ------------------------------------------------------------------
    # 6. Return execution summary
    # ------------------------------------------------------------------
    logger.info(
        "Execution complete | drug_id=%s | doc_id=%s | "
        "emails=%d | voice=%s | audit=%s | firestore=%s",
        payload.drug_id,
        doc_id,
        email_count,
        voice_result.get("success"),
        audit_result.get("success"),
        firestore_result.get("success"),
    )

    return JSONResponse(
        status_code=200,
        content={
            "drug_id":            payload.drug_id,
            "document_id":        doc_id,
            "risk_level":         payload.risk_level,
            "actions_total":      len(action_results),
            "emails_sent":        email_count,
            "actions_logged":     logged_count,
            "voice_notification": voice_result,
            "audit_log_written":  audit_result.get("success", False),
            "firestore_completed": firestore_result.get("success", False),
            "started_at":         started_at,
            "completed_at":       datetime.now(timezone.utc).isoformat(),
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
