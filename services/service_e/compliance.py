"""
Service E — Execution Agent
Compliance and audit trail module.

Two responsibilities after notifications are dispatched:
  1. Write an immutable audit record to BigQuery compliance_trail.audit_log
  2. Mark the pending_approvals Firestore document as status="completed"
     so the UI dashboard reflects that the recovery plan has been executed

BigQuery schema (IaC/main.tf):
  shipment_id : STRING    REQUIRED
  event_type  : STRING    REQUIRED
  actor       : STRING    NULLABLE
  details     : JSON      NULLABLE
  timestamp   : TIMESTAMP REQUIRED

Firestore write target:
  /pending_approvals  — queried by approval_id field, then updated via document reference
  Fields set (merge=True): status="completed", completed_at, actions_executed
"""

import json
import logging
import os
from datetime import datetime, timezone

from google.cloud import bigquery, firestore

logger = logging.getLogger(__name__)

PROJECT_ID   = os.environ["GOOGLE_CLOUD_PROJECT"]
FIRESTORE_DB = os.environ.get("FIRESTORE_DATABASE", "cargo-monitor")
BQ_TABLE_REF = f"{PROJECT_ID}.compliance_trail.audit_log"

# Module-level clients — reused across warm Cloud Run instances
_bq_client = bigquery.Client(project=PROJECT_ID)
_db        = firestore.Client(project=PROJECT_ID, database=FIRESTORE_DB)


# ---------------------------------------------------------------------------
# BigQuery audit log
# ---------------------------------------------------------------------------

def write_audit_log(
    payload:        dict,
    action_results: list,
    voice_result:   dict,
) -> dict:
    """
    Insert an immutable audit record into BigQuery compliance_trail.audit_log.

    The details JSON captures the full execution: per-action results,
    voice notification outcome, agent summary, spoilage assessment,
    and all regulatory metadata from Service D.

    Returns:
        {"success": True, "rows_inserted": 1} on success.
        {"success": False, "error": str} on failure.
    """
    drug_id     = payload.get("drug_id", "unknown")
    approved_by = payload.get("approved_by", "unknown")

    email_successes = [r for r in action_results if r.get("channel") == "email" and r.get("success")]
    email_failures  = [r for r in action_results if r.get("channel") == "email" and not r.get("success")]
    logged_actions  = [r for r in action_results if r.get("channel") == "logged"]

    details = {
        "drug_name":          payload.get("drug_name", drug_id),
        "risk_level":         payload.get("risk_level", "UNKNOWN"),
        "agent_summary":      payload.get("agent_summary", "")[:500],  # truncate for BQ
        "document_id":        payload.get("document_id", "unknown"),
        "approval_id":        payload.get("approval_id", "unknown"),
        "approved_by":        approved_by,
        "actions_total":      len(action_results),
        "emails_sent":        len(email_successes),
        "email_failures":     len(email_failures),
        "actions_logged":     len(logged_actions),
        "voice_notification": voice_result,
        "action_results":     action_results,
    }

    row = {
        "shipment_id": drug_id,
        "event_type":  "recovery_plan_executed",
        "actor":       approved_by,
        "details":     json.dumps(details),
        "timestamp":   datetime.now(timezone.utc).isoformat(),
    }

    try:
        errors = _bq_client.insert_rows_json(BQ_TABLE_REF, [row])
        if errors:
            logger.error("BigQuery insert errors | drug_id=%s | %s", drug_id, errors)
            return {"success": False, "error": str(errors)}

        logger.info(
            "Audit log written | drug_id=%s | actor=%s | emails=%d",
            drug_id, approved_by, len(email_successes),
        )
        return {"success": True, "rows_inserted": 1}

    except Exception as exc:
        logger.error("BigQuery write failed | drug_id=%s | error=%s", drug_id, exc)
        return {"success": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Firestore — mark document as completed
# ---------------------------------------------------------------------------

def mark_completed(document_id: str, action_results: list) -> dict:
    """
    Set status="completed" on the pending_approvals document whose
    approval_id field matches document_id.

    Queries the pending_approvals collection for a document WHERE
    approval_id == document_id, then updates via the document's own
    reference. This is correct regardless of whether Service D uses
    the approval_id as the Firestore document ID or as an internal field.

    Uses merge=True so only the status fields are updated — all other
    fields set by Service D (mitigation plan, risk assessment, etc.)
    remain intact.

    Skips the write if document_id is "unknown" (test payload without
    Service D).

    Args:
        document_id:    The approval_id value to search for in the
                        pending_approvals collection.
        action_results: List of per-action execution results.

    Returns:
        {"success": True,  "document_id": str, "firestore_doc_id": str} on success.
        {"success": True,  "skipped": True}    if no document_id to update.
        {"success": False, "error": str}       if document not found or write fails.
    """
    if not document_id or document_id == "unknown":
        logger.warning(
            "Skipping Firestore status update — approval_id is '%s' "
            "(expected once Service D is implemented)",
            document_id,
        )
        return {"success": True, "skipped": True}

    actions_executed = [
        {"step": r.get("step"), "action_type": r.get("action_type"), "title": r.get("title")}
        for r in action_results
        if r.get("success")
    ]

    try:
        from google.cloud.firestore_v1.base_query import FieldFilter

        # Query by the approval_id field — works whether Service D uses
        # approval_id as the document ID or as an internal field
        docs = list(
            _db.collection("pending_approvals")
               .where(filter=FieldFilter("approval_id", "==", document_id))
               .limit(1)
               .stream()
        )

        if not docs:
            logger.error(
                "No pending_approvals document found with approval_id='%s'",
                document_id,
            )
            return {
                "success": False,
                "error":   f"No pending_approvals document found with approval_id='{document_id}'",
            }

        doc_ref        = docs[0].reference
        firestore_doc_id = docs[0].id

        doc_ref.set(
            {
                "status":           "completed",
                "completed_at":     firestore.SERVER_TIMESTAMP,
                "actions_executed": actions_executed,
            },
            merge=True,
        )
        logger.info(
            "Firestore document marked completed | approval_id=%s | "
            "firestore_doc_id=%s | actions=%d",
            document_id, firestore_doc_id, len(actions_executed),
        )
        return {"success": True, "document_id": document_id, "firestore_doc_id": firestore_doc_id}

    except Exception as exc:
        logger.error(
            "Firestore mark_completed failed | approval_id=%s | error=%s",
            document_id, exc,
        )
        return {"success": False, "error": str(exc)}
