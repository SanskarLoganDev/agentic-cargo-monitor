"""
Service E — Execution Agent
Compliance and audit trail module.

Two responsibilities after notifications are sent:
  1. Write an immutable audit record to BigQuery compliance_trail.audit_log
     (GDP/FDA traceability requirement — every execution must be logged)
  2. Update the pending_approvals Firestore document with execution status
     so the UI dashboard reflects that the plan has been carried out

BigQuery schema (defined in IaC/main.tf):
  shipment_id : STRING  REQUIRED
  event_type  : STRING  REQUIRED
  actor       : STRING  NULLABLE
  details     : JSON    NULLABLE
  timestamp   : TIMESTAMP REQUIRED

Firestore write target:
  /pending_approvals/{approval_id}
  Fields set (merge=True): status, executed_at, channels_executed

All clients are initialised at module level for warm instance reuse.
"""

import json
import logging
import os
from datetime import datetime, timezone

from google.cloud import bigquery, firestore

logger = logging.getLogger(__name__)

PROJECT_ID    = os.environ["GOOGLE_CLOUD_PROJECT"]
FIRESTORE_DB  = os.environ.get("FIRESTORE_DATABASE", "cargo-monitor")
BQ_DATASET    = "compliance_trail"
BQ_TABLE      = "audit_log"
BQ_TABLE_REF  = f"{PROJECT_ID}.{BQ_DATASET}.{BQ_TABLE}"

# Module-level clients — reused across warm Cloud Run instances
_bq_client = bigquery.Client(project=PROJECT_ID)
_db        = firestore.Client(project=PROJECT_ID, database=FIRESTORE_DB)


# ---------------------------------------------------------------------------
# BigQuery audit log
# ---------------------------------------------------------------------------

def write_audit_log(
    payload:              dict,
    notification_results: dict,
) -> dict:
    """
    Insert an immutable audit record into BigQuery compliance_trail.audit_log.

    The details JSON field captures the full execution context:
    risk level, breaches, which notification channels succeeded/failed,
    and the approved_by/approved_at values for regulatory traceability.

    Returns:
        {"success": True, "rows_inserted": 1} on success.
        {"success": False, "error": str} on failure.
    """
    drug_id     = payload.get("drug_id", "unknown")
    approved_by = payload.get("approved_by", "unknown")

    details = {
        "drug_name":                      payload.get("drug_name", drug_id),
        "risk_level":                     payload.get("risk_level", "UNKNOWN"),
        "overall_assessment":             payload.get("overall_assessment", ""),
        "spoilage_likelihood":            payload.get("spoilage_likelihood", ""),
        "estimated_viable_units_percent": payload.get("estimated_viable_units_percent", 0),
        "breaches":                       payload.get("breaches", []),
        "regulatory_flags":               payload.get("regulatory_flags", []),
        "mitigation_plan":                payload.get("mitigation_plan", ""),
        "approval_id":                    payload.get("approval_id", "unknown"),
        "approved_at":                    payload.get("approved_at", ""),
        "notification_channels": {
            "email":      notification_results.get("email", {}),
            "sms":        notification_results.get("sms", {}),
            "voice_call": notification_results.get("voice_call", {}),
        },
    }

    row = {
        "shipment_id": drug_id,
        "event_type":  "execution_completed",
        "actor":       approved_by,
        "details":     json.dumps(details),
        "timestamp":   datetime.now(timezone.utc).isoformat(),
    }

    try:
        errors = _bq_client.insert_rows_json(BQ_TABLE_REF, [row])

        if errors:
            logger.error(
                "BigQuery insert had errors | drug_id=%s | errors=%s",
                drug_id, errors,
            )
            return {"success": False, "error": str(errors)}

        logger.info(
            "Audit log written to BigQuery | drug_id=%s | actor=%s | table=%s",
            drug_id, approved_by, BQ_TABLE_REF,
        )
        return {"success": True, "rows_inserted": 1}

    except Exception as exc:
        logger.error(
            "BigQuery audit log write failed | drug_id=%s | error=%s",
            drug_id, exc,
        )
        return {"success": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Firestore approval status update
# ---------------------------------------------------------------------------

def update_approval_status(
    approval_id:          str,
    notification_results: dict,
) -> dict:
    """
    Mark the pending_approvals Firestore document as executed.

    Uses merge=True so only the status fields are updated — all other
    fields set by Service D (the approval content, mitigation plan, etc.)
    remain intact.

    If approval_id is "unknown" or empty (e.g. during manual testing before
    Service D is implemented), skips the write and logs a warning rather
    than failing.

    Returns:
        {"success": True, "approval_id": str} on success.
        {"success": False, "error": str} on failure.
        {"success": True, "skipped": True} if no approval_id to update.
    """
    if not approval_id or approval_id == "unknown":
        logger.warning(
            "Skipping Firestore approval status update — "
            "approval_id is '%s' (Service D not yet implemented or test payload)",
            approval_id,
        )
        return {"success": True, "skipped": True}

    channels_executed = [
        channel
        for channel, result in notification_results.items()
        if result.get("success")
    ]

    try:
        doc_ref = _db.collection("pending_approvals").document(approval_id)
        doc_ref.set(
            {
                "status":            "executed",
                "executed_at":       firestore.SERVER_TIMESTAMP,
                "channels_executed": channels_executed,
            },
            merge=True,
        )
        logger.info(
            "Firestore approval status updated | approval_id=%s | "
            "channels_executed=%s",
            approval_id, channels_executed,
        )
        return {"success": True, "approval_id": approval_id}

    except Exception as exc:
        logger.error(
            "Firestore approval status update failed | approval_id=%s | error=%s",
            approval_id, exc,
        )
        return {"success": False, "error": str(exc)}
