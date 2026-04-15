"""
LangChain tool: draft_hospital_notification

Drafts email and SMS messages for every stakeholder: receiver POC,
sender operator, and drug manufacturer. Returns structured draft dicts
that are embedded directly in the RecoveryAction list in PendingApprovalSchema.
Service E will send these verbatim via SMTP and Twilio.
"""

from __future__ import annotations

import json

from langchain_core.tools import tool


@tool
def draft_hospital_notification(
    drug_name: str,
    drug_id: str,
    risk_level: str,
    overall_assessment: str,
    receiver_poc_name: str,
    receiver_poc_email: str,
    destination_facility_name: str,
    total_units: int,
    manufacturer_support_email: str,
    contact_email: str,
    contact_phone: str,
    breaches_summary: str,
    spoilage_assessment: str,
    flight_icao: str,
    current_carrier: str,
) -> str:
    """
    Draft all stakeholder notifications (email + SMS) for a cold-chain breach event.
    Returns a JSON string with three notification drafts:
      1. receiver_notification — to the hospital/facility POC
      2. manufacturer_notification — to the drug manufacturer support team
      3. sender_notification — to the sender-side cold-chain operator

    Args:
        drug_name: Full drug name.
        drug_id: Primary shipment key.
        risk_level: CRITICAL | HIGH | MEDIUM.
        overall_assessment: Service C overall risk summary.
        receiver_poc_name: Name of receiver POC.
        receiver_poc_email: Email of receiver POC.
        destination_facility_name: Receiving facility name.
        total_units: Number of vials.
        manufacturer_support_email: Manufacturer emergency email.
        contact_email: Sender-side operator email.
        contact_phone: Sender-side operator phone (E.164).
        breaches_summary: Comma-separated breach descriptions.
        spoilage_assessment: Output from calculate_spoilage_time tool.
        flight_icao: ICAO flight number.
        current_carrier: Current carrier name.

    Returns:
        JSON string with notification drafts.
    """

    # ── Receiver / Hospital notification ──────────────────────────────────────
    receiver_email_subject = (
        f"[{risk_level}] Cold-Chain Breach Alert — {drug_name} Shipment {drug_id}"
    )
    receiver_email_body = f"""Dear {receiver_poc_name},

We are writing to urgently notify you of a critical cold-chain breach affecting your incoming pharmaceutical shipment.

SHIPMENT DETAILS
----------------
Drug:              {drug_name}
Shipment ID:       {drug_id}
Flight:            {flight_icao} operated by {current_carrier}
Consigned Units:   {total_units:,} vials
Destination:       {destination_facility_name}
Risk Level:        {risk_level}

BREACH SUMMARY
--------------
{breaches_summary}

RISK ASSESSMENT
---------------
{overall_assessment}

SPOILAGE ANALYSIS
-----------------
{spoilage_assessment}

IMMEDIATE ACTIONS REQUIRED
--------------------------
1. Do NOT accept or administer any units from this shipment without conducting a full cold-chain integrity review.
2. Prepare contingency cold storage at your facility to receive any salvageable units.
3. Contact your local regulatory authority and document this breach in your pharmacovigilance records.
4. Await further communication from the carrier and cold-chain operations team.

Our orchestration team is actively coordinating a recovery plan and will update you within the next 30 minutes.

For urgent questions, contact cold-chain operations at: {contact_email} | {contact_phone}

This is an automated alert from the AgenticTerps Cargo Monitor System.
"""

    receiver_sms = (
        f"[{risk_level}] Cold-chain breach on {drug_name} shipment {drug_id}. "
        f"DO NOT administer. {total_units:,} vials affected. "
        f"Check email for full details."
    )[:160]

    # ── Manufacturer notification ──────────────────────────────────────────────
    manufacturer_email_subject = (
        f"[URGENT] Cold-Chain Integrity Compromise — {drug_name} Lot {drug_id}"
    )
    manufacturer_email_body = f"""To Whom It May Concern — Emergency Cold-Chain Support Team,

We are reporting a confirmed cold-chain integrity breach for the following shipment and require your immediate guidance on lot disposition.

LOT & SHIPMENT DETAILS
----------------------
Drug:              {drug_name}
Shipment/Lot ID:   {drug_id}
Carrier:           {current_carrier} (Flight: {flight_icao})
Risk Classification: {risk_level}

BREACH PARAMETERS
-----------------
{breaches_summary}

OVERALL ASSESSMENT
------------------
{overall_assessment}

SPOILAGE ANALYSIS
-----------------
{spoilage_assessment}

REQUEST FOR GUIDANCE
--------------------
1. Please advise on lot disposition: quarantine, accelerated stability testing, or destruction.
2. Confirm whether partial salvage is permissible given the excursion data above.
3. Provide reference for regulatory reporting obligations under your EUA/BLA conditions.
4. Advise on cold-chain resumption protocol if any units are deemed salvageable.

Please respond urgently to this notification. Full telemetry logs are available upon request.

AgenticTerps Cargo Monitor System
Sender Operations: {contact_email} | {contact_phone}
"""

    manufacturer_sms = (
        f"[URGENT] {drug_name} lot {drug_id} cold-chain breach — {risk_level}. "
        f"Email sent to {manufacturer_support_email}. Immediate response required."
    )[:160]

    # ── Sender / Operator notification ────────────────────────────────────────
    sender_email_subject = (
        f"[ACTION REQUIRED] Cold-Chain Breach Detected — {drug_name} {drug_id}"
    )
    sender_email_body = f"""Cold-Chain Operations Team,

Automated monitoring has detected a cold-chain breach on your shipment. Immediate intervention is required.

SHIPMENT: {drug_id} | {drug_name} | {total_units:,} units
FLIGHT:   {flight_icao} via {current_carrier}
RISK:     {risk_level}

BREACH DETAILS
--------------
{breaches_summary}

REQUIRED ACTIONS
----------------
1. Immediately contact {current_carrier} cargo operations to issue a HOLD order on this shipment.
2. Arrange emergency supplemental cold storage (dry ice / active refrigeration unit) at the point of delay.
3. Retrieve and preserve all digital data logger records for the full flight duration.
4. Coordinate with the receiving facility ({destination_facility_name}) to prepare contingency storage.
5. Submit a GDP deviation report within 24 hours per your QMS procedures.

Spoilage Assessment: {spoilage_assessment}

AgenticTerps Cargo Monitor — Automated Alert
"""

    sender_sms = (
        f"[{risk_level}] {drug_name} {drug_id}: cold-chain breach on {flight_icao}. "
        f"Issue HOLD with {current_carrier} immediately. Check email."
    )[:160]

    output = {
        "receiver_notification": {
            "recipient_name": receiver_poc_name,
            "recipient_email": receiver_poc_email,
            "email_subject": receiver_email_subject,
            "email_body": receiver_email_body,
            "sms_body": receiver_sms,
            "urgency": risk_level,
        },
        "manufacturer_notification": {
            "recipient_name": "Manufacturer Emergency Support",
            "recipient_email": manufacturer_support_email,
            "email_subject": manufacturer_email_subject,
            "email_body": manufacturer_email_body,
            "sms_body": manufacturer_sms,
            "urgency": "CRITICAL",
        },
        "sender_notification": {
            "recipient_name": "Sender Cold-Chain Operations",
            "recipient_email": contact_email,
            "email_subject": sender_email_subject,
            "email_body": sender_email_body,
            "sms_body": None,  # Phone-only for sender
            "urgency": "CRITICAL",
        },
    }

    return json.dumps(output, indent=2)