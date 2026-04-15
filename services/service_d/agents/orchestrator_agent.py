"""
Service D — Orchestrator Agent
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from google.cloud import firestore
from langchain.agents import AgentExecutor, create_tool_calling_agent
from langchain_anthropic import ChatAnthropic
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from schemas.pending_approval import (
    ApprovalStatus,
    PendingApprovalSchema,
    RecoveryAction,
)
from tools.calculate_spoilage import calculate_spoilage_time
from tools.draft_notification import draft_hospital_notification
from tools.find_alternative_carrier import find_alternative_carrier

logger = logging.getLogger(__name__)

FIRESTORE_COLLECTION_APPROVALS = "pending-approvals"
FIRESTORE_COLLECTION_SHIPMENTS = "shipments"
CLAUDE_MODEL = "claude-sonnet-4-5"

SYSTEM_PROMPT = """You are the Orchestrator Agent for AgenticTerps — an AI system that monitors
pharmaceutical cold-chain shipments in real time. You have just received a RISK ALERT from
Service C indicating that a shipment has experienced dangerous cold-chain breaches.

YOUR MISSION: Produce a comprehensive, actionable cascading recovery plan that a human operator
can review and approve in the AgenticTerps dashboard.

YOU HAVE ACCESS TO THREE TOOLS — call ALL THREE before formulating your final plan:
1. find_alternative_carrier — ALWAYS call this first. It fetches real-time flight telemetry
   and route data from the Airlabs API for the current shipment flight.
2. calculate_spoilage_time — Call this with the breach data to determine viable unit count,
   financial impact, and whether the shipment can survive to its destination.
3. draft_hospital_notification — Call this to generate pre-drafted emails and SMS messages
   for the hospital receiver, drug manufacturer, and sender-side operator. Service E will
   send these verbatim via SMTP and Twilio — so make them professional and complete.

AFTER calling all tools, produce a JSON recovery plan with this EXACT structure:
{{
  "agent_summary": "<Plain-English explanation in 3-5 sentences of WHY you chose this plan. This is what the human operator reads before clicking Approve. Be specific about the breach severity and your reasoning.>",
  "spoilage_assessment": "<The full output from calculate_spoilage_time tool>",
  "ui_summary": [
    "<Bullet 1: most critical fact/action — max 12 words>",
    "<Bullet 2>",
    "<Bullet 3>",
    "<Bullet 4>",
    "<Bullet 5 — optional>",
    "<Bullet 6 — optional>"
  ],
  "recovery_actions": [
    {{
      "step": 1,
      "action_type": "QUARANTINE",
      "title": "<Short UI title>",
      "description": "<Full explanation for the operator>",
      "recipient_name": null,
      "recipient_email": null,
      "recipient_phone": null,
      "email_subject": null,
      "email_body": null,
      "sms_body": null,
      "urgency": "CRITICAL",
      "metadata": {{}}
    }}
  ]
}}

ui_summary RULES:
- 3 to 6 items only. Each item is one short sentence (≤12 words).
- Cover: the breach severity, quarantine action, who gets notified, spoilage impact.
- This is what the judge/operator sees first on the UI dashboard before reading the full plan.
- Example: ["CRITICAL: 5 simultaneous cold-chain breaches detected",
             "Quarantine all 15,000 vials immediately — do not administer",
             "Temp breach: -50°C (spec: -90 to -60°C)",
             "Issue HOLD with American Airlines Cargo (AAL292)",
             "Notify AIIMS Delhi POC + Pfizer manufacturer support",
             "~1,500 vials estimated compromised"]

RECOVERY PLAN GUIDELINES:
- Step 1 is ALWAYS: QUARANTINE — instruct the operator to quarantine the shipment
- Step 2: CONTACT_CARRIER — issue a hold order with the current carrier
- Step 3: NOTIFY_RECEIVER — email + SMS to the hospital POC (use drafts from tool)
- Step 4: NOTIFY_MANUFACTURER — email to manufacturer support (use drafts from tool)
- Step 5: NOTIFY_SENDER — email to sender operator (use drafts from tool)
- Step 6: SPOILAGE_ASSESSMENT — document the spoilage analysis and viable unit count
- Step 7: LOG_COMPLIANCE — log the incident for GDP/FDA compliance trail
- Add additional steps if the flight data reveals rerouting opportunities

Be specific. Include all email/SMS content from the draft_hospital_notification tool output
into the relevant recovery_actions so Service E can execute without additional lookups.

CONTACT FALLBACK RULE — CRITICAL:
For every recovery_action that involves contacting someone (NOTIFY_*, CONTACT_CARRIER, etc.),
you MUST populate recipient_email and/or recipient_phone. If you do not have a specific contact
for that role, use whatever contact is available in the data (contact_email, receiver_poc_email,
manufacturer_support_email — in that order of preference). Never leave recipient_email or
recipient_phone as null for any action that sends a communication. Use the sender operator
contact (contact_email / contact_phone) as the universal fallback if nothing else is available.

IMPORTANT: Your final output must be ONLY the JSON object — no markdown, no explanation outside the JSON."""


class OrchestratorAgent:

    def __init__(self, db: firestore.Client):
        self.db = db

        self.tools = [
            find_alternative_carrier,
            calculate_spoilage_time,
            draft_hospital_notification,
        ]

        self.llm = ChatAnthropic(
            model=CLAUDE_MODEL,
            anthropic_api_key=os.environ["ANTHROPIC_API_KEY"],
            max_tokens=8192,
            temperature=0,
        )

        self.prompt = ChatPromptTemplate.from_messages([
            ("system", SYSTEM_PROMPT),
            ("human", "{input}"),
            MessagesPlaceholder(variable_name="agent_scratchpad"),
        ])

        agent = create_tool_calling_agent(self.llm, self.tools, self.prompt)
        self.executor = AgentExecutor(
            agent=agent,
            tools=self.tools,
            verbose=True,
            max_iterations=10,
            handle_parsing_errors=True,
            return_intermediate_steps=True,
        )

    def _fetch_firestore_metadata(self, drug_id: str) -> dict[str, Any]:
        doc_ref = self.db.collection(FIRESTORE_COLLECTION_SHIPMENTS).document(drug_id)
        doc = doc_ref.get()
        if not doc.exists:
            logger.warning("No Firestore document found for drug_id=%s", drug_id)
            return {}
        metadata = doc.to_dict()
        logger.info("Fetched Firestore metadata for %s (%d fields)", drug_id, len(metadata))
        return metadata

    def _build_agent_input(
        self,
        risk_event: dict[str, Any],
        firestore_metadata: dict[str, Any],
    ) -> str:
        return f"""
RISK ALERT RECEIVED FROM SERVICE C
====================================
Drug ID: {risk_event.get('drug_id')}

RISK EVENT (from risk-detected Pub/Sub topic):
{json.dumps(risk_event, indent=2, default=str)}

FIRESTORE SHIPMENT METADATA (full document from /shipments/{risk_event.get('drug_id')}):
{json.dumps(firestore_metadata, indent=2, default=str)}

INSTRUCTIONS:
1. Call find_alternative_carrier using flight_icao="{firestore_metadata.get('flight_icao', '')}"
   and the cargo details above.
2. Call calculate_spoilage_time using thaw_window_hours={firestore_metadata.get('thaw_window_hours', 720)},
   excursion_minutes from the telemetry, and all other fields above.
3. Call draft_hospital_notification using receiver_poc_name="{firestore_metadata.get('receiver_poc_name', '')}",
   receiver_poc_email="{firestore_metadata.get('receiver_poc_email', '')}",
   manufacturer_support_email="{firestore_metadata.get('manufacturer_support_email', '')}",
   contact_email="{firestore_metadata.get('contact_email', '')}",
   contact_phone="{firestore_metadata.get('contact_phone', '')}",
   and all other relevant fields.
4. After all tools are called, output ONLY the JSON recovery plan as specified in your instructions.
"""

    def _parse_agent_output(self, raw_output: Any) -> dict[str, Any]:
        # Normalise: langchain-anthropic may return a list of content blocks
        if isinstance(raw_output, list):
            parts = []
            for block in raw_output:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block["text"])
                elif isinstance(block, str):
                    parts.append(block)
            raw = "\n".join(parts).strip()
        elif isinstance(raw_output, str):
            raw = raw_output.strip()
        else:
            raise ValueError(f"Unexpected agent output type: {type(raw_output)}")

        # Strip markdown code fences if present
        if raw.startswith("```"):
            lines = raw.split("\n")
            start = 1
            end = len(lines) - 1 if lines[-1].strip() == "```" else len(lines)
            raw = "\n".join(lines[start:end]).strip()

        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.error(
                "Failed to parse agent JSON output: %s\nRaw output (first 500 chars): %s",
                exc, raw[:500],
            )
            raise ValueError(f"Agent output was not valid JSON: {exc}") from exc
        
    
    def _build_pending_approval(
        self,
        approval_id: str,
        risk_event: dict[str, Any],
        firestore_metadata: dict[str, Any],
        plan: dict[str, Any],
        flight_context: dict[str, Any],
    ) -> PendingApprovalSchema:
        raw_actions = plan.get("recovery_actions", [])
        recovery_actions = []
        for i, action_data in enumerate(raw_actions):
            try:
                recovery_actions.append(RecoveryAction(**action_data))
            except Exception as exc:
                logger.warning("Could not parse recovery action %d: %s", i, exc)

        return PendingApprovalSchema(
            approval_id=approval_id,
            drug_id=risk_event.get("drug_id", ""),
            drug_name=risk_event.get("drug_name", firestore_metadata.get("drug_name", "")),
            manufacturer=risk_event.get("manufacturer", firestore_metadata.get("manufacturer", "")),
            risk_level=risk_event.get("risk_level", "HIGH"),
            overall_assessment=risk_event.get("overall_assessment", ""),
            breaches=risk_event.get("breaches", []),
            compound_risk_note=risk_event.get("compound_risk_note"),
            agent_summary=plan.get("agent_summary", "Recovery plan generated."),
            spoilage_assessment=plan.get("spoilage_assessment", ""),
            ui_summary=plan.get("ui_summary", []),          # ← NEW
            flight_context=flight_context,
            recovery_actions=recovery_actions,
            telemetry_snapshot=risk_event.get("telemetry", {}),
            contact_email=risk_event.get("contact_email") or firestore_metadata.get("contact_email"),
            contact_phone=risk_event.get("contact_phone") or firestore_metadata.get("contact_phone"),
            receiver_poc_name=firestore_metadata.get("receiver_poc_name"),
            receiver_poc_email=firestore_metadata.get("receiver_poc_email"),
            manufacturer_support_email=firestore_metadata.get("manufacturer_support_email"),
            total_units=firestore_metadata.get("total_units"),
            destination_facility_name=firestore_metadata.get("destination_facility_name"),
            current_carrier=firestore_metadata.get("current_carrier"),
            flight_icao=firestore_metadata.get("flight_icao"),
            status=ApprovalStatus.PENDING,                  # ← explicitly set, always written
            created_at=datetime.now(timezone.utc).isoformat(),
            source_service="service_d",
            detected_at=risk_event.get("detected_at"),
        )


    def write_approved_action(self, approval_id: str) -> None:
        """
        Called by Service E (or a Firestore trigger) after the human clicks Approve.
        Copies the approved document from /pending-approvals into /approved-actions
        and updates the status field to 'approved' in both collections.
        """
        APPROVED_COLLECTION = "approved-actions"

        # Fetch the approved document from pending-approvals
        src_ref = self.db.collection(FIRESTORE_COLLECTION_APPROVALS).document(approval_id)
        doc = src_ref.get()
        if not doc.exists:
            logger.error("Cannot find /pending-approvals/%s to copy to approved-actions", approval_id)
            return

        data = doc.to_dict()
        data["status"] = "approved"
        data["approved_at"] = datetime.now(timezone.utc).isoformat()

        # Write to /approved-actions/{approval_id}
        dst_ref = self.db.collection(APPROVED_COLLECTION).document(approval_id)
        dst_ref.set(data)

        # Update status in the original pending-approvals document
        src_ref.update({"status": "approved"})

        logger.info(
            "Approval %s moved to /approved-actions and status updated to 'approved'",
            approval_id,
        )
        

    def _write_to_firestore(self, approval: PendingApprovalSchema) -> None:
        """
        Write to BOTH collections simultaneously:

        1. /pending-approvals/{approval_id}  — full document, status: pending
        (Frontend listens here for HITL panel)

        2. /approved-actions/{approval_id}   — placeholder document, status: pending
        Service E will call .update() on this document to add:
            - status: "approved"
            - approved_at: <timestamp>
            - executed_actions: [...results of each action]
        No need to create the document — it already exists.
        """
        APPROVED_ACTIONS_COLLECTION = "approved-actions"

        full_data = approval.to_firestore_dict()

        # ── 1. Write full plan to pending-approvals ───────────────────────────
        pending_ref = self.db.collection(FIRESTORE_COLLECTION_APPROVALS).document(
            approval.approval_id
        )
        pending_ref.set(full_data)
        logger.info(
            "Written to /pending-approvals/%s (drug=%s, risk=%s)",
            approval.approval_id,
            approval.drug_id,
            approval.risk_level,
        )

        # ── 2. Write placeholder to approved-actions ──────────────────────────
        # Only identity + status fields — Service E fills in the rest on approval
        placeholder = {
            "approval_id":   approval.approval_id,
            "drug_id":       approval.drug_id,
            "drug_name":     approval.drug_name,
            "risk_level":    approval.risk_level,
            "flight_icao":   approval.flight_icao,
            "total_units":   approval.total_units,
            "contact_email": approval.contact_email,
            "contact_phone": approval.contact_phone,
            "status":        "pending",          # Service E updates this to "approved"
            "approved_at":   None,               # Service E fills this in
            "approved_by":   None,               # Service E / frontend fills this in
            "executed_actions": [],              # Service E appends results here
            "created_at":    approval.created_at,
            "detected_at":   approval.detected_at,
            "source_service": "service_d",
        }

        approved_ref = self.db.collection(APPROVED_ACTIONS_COLLECTION).document(
            approval.approval_id
        )
        approved_ref.set(placeholder)
        logger.info(
            "Written placeholder to /approved-actions/%s — awaiting Service E execution",
            approval.approval_id,
        )

    def run(self, risk_event: dict[str, Any]) -> str:
        drug_id = risk_event.get("drug_id")
        if not drug_id:
            raise ValueError("risk_event must contain 'drug_id'")

        timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")

        safe_drug_id = (risk_event.get("drug_id") or "unknown").replace(" ", "-").replace("/", "-")
        
        approval_id = f"{safe_drug_id}_{timestamp_str}"

        logger.info(
            "OrchestratorAgent.run | approval_id=%s | drug_id=%s | risk=%s",
            approval_id, drug_id, risk_event.get("risk_level"),
        )

        firestore_metadata = self._fetch_firestore_metadata(drug_id)
        agent_input = self._build_agent_input(risk_event, firestore_metadata)

        logger.info("Starting LangChain agent executor for %s", drug_id)
        agent_result = self.executor.invoke({"input": agent_input})
        raw_output = agent_result.get("output", "")
        intermediate_steps = agent_result.get("intermediate_steps", [])

        plan = self._parse_agent_output(raw_output)

        flight_context: dict[str, Any] = {}
        for action, observation in intermediate_steps:
            tool_name = getattr(action, "tool", "")
            if tool_name == "find_alternative_carrier":
                try:
                    flight_context = json.loads(observation)
                except Exception:
                    flight_context = {"raw": str(observation)}

        approval = self._build_pending_approval(
            approval_id=approval_id,
            risk_event=risk_event,
            firestore_metadata=firestore_metadata,
            plan=plan,
            flight_context=flight_context,
        )

        self._write_to_firestore(approval)

        logger.info("OrchestratorAgent complete | approval_id=%s", approval_id)
        return approval_id