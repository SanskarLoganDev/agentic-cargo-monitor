"""
Pydantic schema for a document written to /pending-approvals/{approval_id}.

The frontend continuously listens to this Firestore collection.
When a new document appears, the UI pops up the HITL approval panel.
Service E is triggered when status changes from "pending" → "approved".
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class ApprovalStatus(str, Enum):
    PENDING = "pending"     # Waiting for human operator decision
    APPROVED = "approved"   # Human clicked Approve — triggers Service E
    REJECTED = "rejected"   # Human clicked Reject — logged, no execution


class RecoveryAction(BaseModel):
    """A single step in the cascading recovery plan."""
    step: int = Field(..., description="Ordered step number (1-based)")
    action_type: str = Field(
        ...,
        description=(
            "Machine-readable type. One of: QUARANTINE, CONTACT_CARRIER, "
            "REROUTE_SHIPMENT, NOTIFY_RECEIVER, NOTIFY_MANUFACTURER, "
            "NOTIFY_SENDER, SEND_SMS, LOG_COMPLIANCE, SPOILAGE_ASSESSMENT"
        ),
    )
    title: str = Field(..., description="Short human-readable action title for UI")
    description: str = Field(..., description="Full plain-English explanation for the operator")
    recipient_name: Optional[str] = Field(default=None, description="Name of person/entity to contact")
    recipient_email: Optional[str] = Field(default=None, description="Email address for Service E to send to")
    recipient_phone: Optional[str] = Field(default=None, description="Phone for Service E Twilio SMS")
    email_subject: Optional[str] = Field(default=None, description="Pre-drafted email subject line")
    email_body: Optional[str] = Field(default=None, description="Pre-drafted email body for Service E to send verbatim")
    sms_body: Optional[str] = Field(default=None, description="Pre-drafted SMS text (160 char limit)")
    urgency: str = Field(default="HIGH", description="CRITICAL | HIGH | MEDIUM | LOW")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Extra key-value data for Service E")


class PendingApprovalSchema(BaseModel):
    """
    Complete document written to /pending-approvals/{approval_id}.

    Fields consumed by:
    - Frontend: drug_id, drug_name, risk_level, summary, rationale,
                recovery_actions, spoilage_assessment, status
    - Service E: recovery_actions[*].recipient_email/phone/email_body/sms_body,
                 drug_id, drug_name, total_units, contact_email
    """

    # ── Identity ─────────────────────────────────────────────────────────────
    approval_id: str = Field(..., description="UUID — used as Firestore document ID")
    drug_id: str = Field(..., description="Primary key e.g. 'pfizer-001'")
    drug_name: str = Field(..., description="Human-readable drug name")
    manufacturer: str = Field(default="", description="Drug manufacturer name")
    
    # ── UI Summary (shown on HITL UI panel) ─────────────────────────────────
    ui_summary: list[str] = Field(
        default_factory=list,
        description=(
            "3-6 bullet points for the UI Command Center. Plain English. "
            "Each bullet is one key action or fact the operator must know before clicking Approve. "
            "Example: ['Quarantine 15,000 vials immediately', 'Temp breach: -50°C (spec: -90 to -60°C)', "
            "'Issue HOLD with American Airlines Cargo', 'Notify Dr. Vidyarthi at AIIMS Delhi', "
            "'~1,500 units likely compromised']"
        ),
    )

    # ── Risk Context (shown on HITL UI panel) ─────────────────────────────────
    risk_level: str = Field(..., description="CRITICAL | HIGH | MEDIUM | LOW")
    overall_assessment: str = Field(..., description="Plain-English risk summary from Service C")
    breaches: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Raw breach objects from Service C finalresponse.json"
    )
    compound_risk_note: Optional[str] = Field(
        default=None, description="Compound risk analysis from Service C"
    )

    # ── Agent Rationale (shown on HITL UI) ───────────────────────────────────
    agent_summary: str = Field(
        ...,
        description=(
            "Claude's plain-English explanation of WHY it chose this recovery plan. "
            "Displayed prominently in the UI Command Center so the operator understands "
            "the agent's reasoning before clicking Approve."
        ),
    )
    spoilage_assessment: str = Field(
        ...,
        description=(
            "Calculated spoilage risk: estimated viable units %, time-to-complete-spoilage, "
            "and financial impact estimate."
        ),
    )
    flight_context: dict[str, Any] = Field(
        default_factory=dict,
        description="Combined Airlabs flight + route data used by the agent"
    )

    # ── Recovery Plan ─────────────────────────────────────────────────────────
    recovery_actions: list[RecoveryAction] = Field(
        ...,
        description=(
            "Ordered list of actions for Service E to execute upon approval. "
            "Each action has enough data (email, phone, pre-drafted message) "
            "for Service E to execute without additional lookups."
        ),
    )

    # ── Telemetry Snapshot (shown on UI) ──────────────────────────────────────
    telemetry_snapshot: dict[str, Any] = Field(
        default_factory=dict,
        description="Raw telemetry at time of detection from Service C"
    )

    # ── Contacts (carried for Service E) ──────────────────────────────────────
    contact_email: Optional[str] = Field(default=None)
    contact_phone: Optional[str] = Field(default=None)
    receiver_poc_name: Optional[str] = Field(default=None)
    receiver_poc_email: Optional[str] = Field(default=None)
    manufacturer_support_email: Optional[str] = Field(default=None)
    total_units: Optional[int] = Field(default=None)
    destination_facility_name: Optional[str] = Field(default=None)
    current_carrier: Optional[str] = Field(default=None)
    flight_icao: Optional[str] = Field(default=None)

    # ── Workflow State ────────────────────────────────────────────────────────
    status: ApprovalStatus = Field(
        default=ApprovalStatus.PENDING,
        description="Firestore listens for changes from 'pending' → 'approved'/'rejected'"
    )

    created_at: Optional[str] = Field(
        default=None, description="ISO 8601 UTC timestamp when agent created this document"
    )
    source_service: str = Field(default="service_d")
    detected_at: Optional[str] = Field(
        default=None, description="Timestamp from Service C when risk was detected"
    )

    class Config:
        use_enum_values = True

    def to_firestore_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict for Firestore, with nested models serialized."""
        data = self.model_dump()
        # Serialize nested RecoveryAction list to plain dicts
        data["recovery_actions"] = [
            action.model_dump() for action in self.recovery_actions
        ]
        return data