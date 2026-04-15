"""
Service E — Local Test Script

Simulates a Pub/Sub push from Service D and sends it to the locally running
Service E uvicorn server.

Usage:
  # Terminal 1 — start the server
  cd services/service_e
  source venv/Scripts/activate        # Windows Git Bash
  # source venv/bin/activate          # Mac / Linux
  GOOGLE_APPLICATION_CREDENTIALS="$APPDATA/gcloud/application_default_credentials.json" \\
  uvicorn main:app --host 0.0.0.0 --port 8080 --reload

  # Terminal 2 — run this script
  cd services/service_e
  source venv/Scripts/activate
  python test_local.py

  # Optional: run with a custom payload file
  python test_local.py --payload path/to/your_payload.json

  # Optional: run against the deployed Cloud Run service
  python test_local.py --url https://service-e-execution-<hash>-uc.a.run.app

  # Optional: skip Firestore update (no approval_id needed)
  python test_local.py --no-firestore
"""

import argparse
import base64
import json
import sys
import textwrap
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    print("ERROR: 'requests' not installed. Activate the venv and run: pip install -r requirements.txt")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Test payload — mirrors what Service D publishes to execute-actions.
# The recovery_actions are taken directly from the example JSON provided
# by the Service D team. Top-level identification fields are added here.
# ---------------------------------------------------------------------------

FULL_TEST_PAYLOAD = {
    # ── Top-level fields added by Service D ─────────────────────────────────
    "drug_id":     "pfizer-001",
    "drug_name":   "COMIRNATY (BNT162b2)",
    "approval_id": "test-approval-001",   # change to a real approval_id to test Firestore
    "document_id": "unknown",
    "approved_by": "test-operator",
    "risk_level":  "CRITICAL",
    "contact_phone": "+12408798960",

    # ── Service D narrative ─────────────────────────────────────────────────
    "agent_summary": (
        "This shipment has experienced a catastrophic cascade of cold-chain failures: "
        "a 6-hour flight delay (4x the 120-minute threshold), 60 minutes of cumulative "
        "temperature excursion (2x the 30-minute maximum), current temperature of -50.0 C "
        "(10 C above the -60 C minimum), elevated humidity at 82% RH creating condensation "
        "risk, and a 28G shock event exceeding the 25G tolerance. Despite the severe "
        "breaches, spoilage assessment indicates 90% viability (13,500 of 15,000 vials) "
        "because the thaw window is 1,680 hours and only 420 minutes have been consumed."
    ),
    "spoilage_assessment": (
        "Thaw viability window: 1680 hours. Excursion consumed: 60 min (0.1%). "
        "Remaining viable window: 1673.0 hours. Estimated viable units: 90% (13,500 of 15,000 vials)."
    ),

    # ── Recovery actions from the Service D example ─────────────────────────
    "recovery_actions": [
        {
            "step": 1,
            "action_type": "QUARANTINE",
            "title": "Immediate Shipment Quarantine",
            "description": (
                "Quarantine the entire shipment of 15,000 vials immediately upon arrival. "
                "Do NOT distribute or administer any units. Isolate in ultra-cold storage "
                "(-90 C to -60 C) with continuous digital data logger monitoring."
            ),
            "recipient_name":  None,
            "recipient_email": None,
            "recipient_phone": None,
            "email_subject":   None,
            "email_body":      None,
            "sms_body":        None,
            "urgency":         "CRITICAL",
            "metadata": {
                "drug_id":     "pfizer-001",
                "total_units": 15000,
                "facility":    "All India Institute of Medical Sciences Central Pharmacy",
            },
        },
        {
            "step": 2,
            "action_type": "CONTACT_CARRIER",
            "title": "Issue Carrier Hold Order",
            "description": (
                "Contact American Airlines Cargo immediately and issue a HOLD order on "
                "flight AAL292. Arrange emergency supplemental dry ice at the next "
                "handling point and preserve all digital data logger records."
            ),
            "recipient_name":  "American Airlines Cargo Operations",
            "recipient_email": None,
            "recipient_phone": None,
            "email_subject":   None,
            "email_body":      None,
            "sms_body":        None,
            "urgency":         "CRITICAL",
            "metadata": {
                "flight_icao":    "AAL292",
                "flight_status":  "en-route",
                "departure":      "KJFK",
                "arrival":        "VIDP",
            },
        },
        {
            "step": 3,
            "action_type": "NOTIFY_RECEIVER",
            "title": "Notify Receiving Facility",
            "description": "Send immediate notification to receiving facility to prepare contingency cold storage.",
            "recipient_name":  "Dr. Sanskar Vidyarthi",
            "recipient_email": "svidyar1@umd.edu",
            "recipient_phone": None,
            "email_subject":   "[CRITICAL] Cold-Chain Breach Alert — Pfizer-BioNTech COVID-19 Vaccine Shipment pfizer-001",
            "email_body": (
                "Dear Dr. Sanskar Vidyarthi,\n\n"
                "We are writing to urgently notify you of a critical cold-chain breach "
                "affecting your incoming pharmaceutical shipment.\n\n"
                "SHIPMENT DETAILS\n"
                "----------------\n"
                "Drug:            Pfizer-BioNTech COVID-19 Vaccine (COMIRNATY) Bivalent\n"
                "Shipment ID:     pfizer-001\n"
                "Flight:          AAL292 operated by American Airlines Cargo\n"
                "Consigned Units: 15,000 vials\n"
                "Risk Level:      CRITICAL\n\n"
                "BREACH SUMMARY\n"
                "--------------\n"
                "- Flight delayed 6 hours (4x maximum allowable delay)\n"
                "- Cumulative out-of-range excursion 60 minutes (2x maximum)\n"
                "- Temperature -50.0 C (10 C above minimum specification)\n"
                "- Humidity 82.0% RH (7% above maximum)\n"
                "- Shock event 28.0 G (3G above maximum tolerance)\n\n"
                "IMMEDIATE ACTIONS REQUIRED\n"
                "--------------------------\n"
                "1. Do NOT accept or administer any units without full cold-chain integrity review.\n"
                "2. Prepare contingency cold storage at your facility.\n"
                "3. Await further communication from the cold-chain operations team.\n\n"
                "This is an automated alert from the AgenticTerps Cargo Monitor System."
            ),
            "sms_body": "[CRITICAL] Cold-chain breach on pfizer-001. DO NOT administer. Check email.",
            "urgency": "CRITICAL",
            "metadata": {
                "destination_facility": "All India Institute of Medical Sciences Central Pharmacy",
            },
        },
        {
            "step": 4,
            "action_type": "NOTIFY_MANUFACTURER",
            "title": "Notify Manufacturer Emergency Support",
            "description": "Send urgent notification to Pfizer-BioNTech requesting immediate guidance on lot disposition.",
            "recipient_name":  "Manufacturer Emergency Support",
            "recipient_email": "dan0003@umd.edu",
            "recipient_phone": None,
            "email_subject":   "[URGENT] Cold-Chain Integrity Compromise — Pfizer-BioNTech COVID-19 Vaccine Lot pfizer-001",
            "email_body": (
                "To Whom It May Concern — Emergency Cold-Chain Support Team,\n\n"
                "We are reporting a confirmed cold-chain integrity breach for the following "
                "shipment and require your immediate guidance on lot disposition.\n\n"
                "LOT & SHIPMENT DETAILS\n"
                "----------------------\n"
                "Drug:              Pfizer-BioNTech COVID-19 Vaccine (COMIRNATY) Bivalent\n"
                "Shipment/Lot ID:   pfizer-001\n"
                "Carrier:           American Airlines Cargo (Flight: AAL292)\n"
                "Risk Classification: CRITICAL\n\n"
                "BREACH PARAMETERS\n"
                "-----------------\n"
                "- Flight delayed 6 hours (4x maximum allowable delay)\n"
                "- Temperature -50.0 C (10 C above minimum specification)\n"
                "- Humidity 82.0% RH (7% above maximum)\n"
                "- Shock event 28.0 G (3G above maximum tolerance)\n\n"
                "REQUEST FOR GUIDANCE\n"
                "--------------------\n"
                "1. Please advise on lot disposition: quarantine, stability testing, or destruction.\n"
                "2. Confirm whether partial salvage is permissible.\n"
                "3. Provide reference for regulatory reporting obligations.\n\n"
                "AgenticTerps Cargo Monitor System"
            ),
            "sms_body": "[URGENT] pfizer-001 cold-chain breach CRITICAL. Email sent. Immediate response required.",
            "urgency": "CRITICAL",
            "metadata": {"manufacturer": "Pfizer-BioNTech"},
        },
        {
            "step": 5,
            "action_type": "NOTIFY_SENDER",
            "title": "Notify Sender Cold-Chain Operations",
            "description": "Alert the sender-side cold-chain operations team of the breach.",
            "recipient_name":  "Sender Cold-Chain Operations",
            "recipient_email": "rohinv@umd.edu",
            "recipient_phone": "+12408798960",
            "email_subject":   "[ACTION REQUIRED] Cold-Chain Breach Detected — pfizer-001",
            "email_body": (
                "Cold-Chain Operations Team,\n\n"
                "Automated monitoring has detected a cold-chain breach on your shipment. "
                "Immediate intervention is required.\n\n"
                "SHIPMENT: pfizer-001 | Pfizer COMIRNATY Bivalent | 15,000 units\n"
                "FLIGHT:   AAL292 via American Airlines Cargo\n"
                "RISK:     CRITICAL\n\n"
                "REQUIRED ACTIONS\n"
                "----------------\n"
                "1. Contact American Airlines Cargo and issue a HOLD order.\n"
                "2. Arrange emergency supplemental cold storage at the point of delay.\n"
                "3. Retrieve and preserve all digital data logger records.\n"
                "4. Coordinate with receiving facility for contingency storage.\n"
                "5. Submit a GDP deviation report within 24 hours.\n\n"
                "AgenticTerps Cargo Monitor — Automated Alert"
            ),
            "sms_body": None,
            "urgency": "CRITICAL",
            "metadata": {"contact_email": "rohinv@umd.edu", "contact_phone": "+12408798960"},
        },
        {
            "step": 6,
            "action_type": "SPOILAGE_ASSESSMENT",
            "title": "Document Spoilage Analysis and Viable Unit Count",
            "description": (
                "Record the spoilage assessment in the compliance log. 90% viability "
                "(13,500 of 15,000 vials). Conduct accelerated stability testing before "
                "releasing any units for clinical use."
            ),
            "recipient_name":  None,
            "recipient_email": None,
            "recipient_phone": None,
            "email_subject":   None,
            "email_body":      None,
            "sms_body":        None,
            "urgency":         "HIGH",
            "metadata": {
                "estimated_viable_units": 13500,
                "compromised_units":      1500,
                "viability_percent":      90,
                "thaw_window_hours":      1680,
            },
        },
        {
            "step": 7,
            "action_type": "LOG_COMPLIANCE",
            "title": "Log Incident for GDP/FDA Compliance Trail",
            "description": (
                "Create a formal incident report documenting this cold-chain breach "
                "for regulatory compliance per 21 CFR 211.180. Retain records for "
                "minimum 3 years."
            ),
            "recipient_name":  None,
            "recipient_email": None,
            "recipient_phone": None,
            "email_subject":   None,
            "email_body":      None,
            "sms_body":        None,
            "urgency":         "HIGH",
            "metadata": {
                "regulatory_framework": "FDA EUA / BLA",
                "retention_period_years": 3,
            },
        },
        {
            "step": 8,
            "action_type": "POTENCY_TESTING",
            "title": "Mandatory Potency Testing Before Release",
            "description": (
                "Before releasing any units from quarantine, conduct mandatory accelerated "
                "stability testing. Acceptance criteria: potency >= 90% of labeled claim. "
                "If potency < 90% or physical defects are observed, the entire shipment "
                "must be rejected and destroyed."
            ),
            "recipient_name":  None,
            "recipient_email": None,
            "recipient_phone": None,
            "email_subject":   None,
            "email_body":      None,
            "sms_body":        None,
            "urgency":         "CRITICAL",
            "metadata": {
                "testing_required":    True,
                "acceptance_criteria": ">=90% potency, no physical defects",
            },
        },
    ],
}


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _section(title: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print('=' * 60)

def _ok(msg: str) -> None:
    print(f"  [OK]   {msg}")

def _fail(msg: str) -> None:
    print(f"  [FAIL] {msg}")

def _info(msg: str) -> None:
    print(f"         {msg}")


# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------

def run_test(base_url: str, payload: dict) -> None:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # ── Step 0: health check ─────────────────────────────────────────────────
    _section("Step 0 — Health Check")
    try:
        r = requests.get(f"{base_url}/health", timeout=5)
        if r.status_code == 200:
            _ok(f"Server is up — {r.json()}")
        else:
            _fail(f"Unexpected status {r.status_code}")
            sys.exit(1)
    except requests.exceptions.ConnectionError:
        _fail(f"Cannot connect to {base_url}")
        _info("Start the server first:")
        _info("  uvicorn main:app --host 0.0.0.0 --port 8080 --reload")
        sys.exit(1)

    # ── Step 1: build Pub/Sub envelope ───────────────────────────────────────
    _section("Step 1 — Building Pub/Sub Envelope")
    payload["approved_at"] = timestamp

    encoded_data = base64.b64encode(
        json.dumps(payload).encode("utf-8")
    ).decode("utf-8")

    envelope = {
        "message": {
            "data":        encoded_data,
            "messageId":   "local-test-001",
            "publishTime": timestamp,
        },
        "subscription": "projects/local/subscriptions/execute-actions-sub",
    }

    _ok(f"Payload encoded | drug_id={payload['drug_id']} | actions={len(payload['recovery_actions'])}")
    _ok(f"approval_id={payload['approval_id']}")

    email_actions = [
        a for a in payload["recovery_actions"]
        if a.get("recipient_email") and a.get("email_subject") and a.get("email_body")
    ]
    logged_actions = [
        a for a in payload["recovery_actions"]
        if not (a.get("recipient_email") and a.get("email_subject") and a.get("email_body"))
    ]
    _info(f"Email actions:  {len(email_actions)} ({', '.join(str(a['step']) for a in email_actions)})")
    _info(f"Logged actions: {len(logged_actions)} ({', '.join(str(a['step']) for a in logged_actions)})")

    # ── Step 2: POST to /pubsub/execute ──────────────────────────────────────
    _section("Step 2 — Sending to /pubsub/execute")
    print(f"  POST {base_url}/pubsub/execute")

    try:
        response = requests.post(
            f"{base_url}/pubsub/execute",
            json=envelope,
            headers={"Content-Type": "application/json"},
            timeout=120,   # voice generation can take ~10s
        )
    except requests.exceptions.Timeout:
        _fail("Request timed out after 120s")
        sys.exit(1)
    except requests.exceptions.ConnectionError as exc:
        _fail(f"Connection error: {exc}")
        sys.exit(1)

    # ── Step 3: parse and display response ───────────────────────────────────
    _section("Step 3 — Response")
    print(f"  HTTP {response.status_code}")

    try:
        result = response.json()
    except Exception:
        _fail(f"Non-JSON response: {response.text[:200]}")
        sys.exit(1)

    if "error" in result:
        _fail(f"Service returned error: {result['error']}")
        _info(f"Detail: {result.get('detail', '')}")
        sys.exit(1)

    # ── Step 4: detailed results ─────────────────────────────────────────────
    _section("Step 4 — Execution Summary")

    _info(f"drug_id:      {result.get('drug_id')}")
    _info(f"document_id:  {result.get('document_id')}")
    _info(f"risk_level:   {result.get('risk_level')}")
    _info(f"actions_total:{result.get('actions_total')}")
    _info(f"emails_sent:  {result.get('emails_sent')}")
    _info(f"actions_logged:{result.get('actions_logged')}")
    _info(f"started_at:   {result.get('started_at')}")
    _info(f"completed_at: {result.get('completed_at')}")

    _section("Step 5 — Channel Results")

    # Voice
    voice = result.get("voice_notification", {})
    if voice.get("success"):
        _ok(f"Voice call placed | call_sid={voice.get('call_sid')}")
        _info(f"GCS URL: {voice.get('gcs_url')}")
    else:
        _fail(f"Voice call failed | {voice.get('error')}")

    # BigQuery
    if result.get("audit_log_written"):
        _ok("BigQuery audit log written")
    else:
        _fail("BigQuery audit log FAILED")

    # Firestore
    if result.get("firestore_completed"):
        _ok(f"Firestore document marked completed")
    else:
        fs = result.get("firestore_completed")
        if payload.get("approval_id") in ("unknown", "test-approval-001"):
            _info("Firestore update skipped — approval_id is a test value")
            _info("To test Firestore: set approval_id to a real pending_approvals document value")
        else:
            _fail("Firestore update FAILED")

    # ── Step 6: full JSON ────────────────────────────────────────────────────
    _section("Step 6 — Full JSON Response")
    print(json.dumps(result, indent=2))

    _section("Done")
    emails_ok  = result.get("emails_sent", 0)
    voice_ok   = result.get("voice_notification", {}).get("success", False)
    audit_ok   = result.get("audit_log_written", False)

    if emails_ok > 0 and voice_ok and audit_ok:
        print(f"\n  ALL CHECKS PASSED — {emails_ok} email(s) sent, voice call placed, audit log written.")
    else:
        issues = []
        if emails_ok == 0:
            issues.append("no emails sent")
        if not voice_ok:
            issues.append("voice call failed")
        if not audit_ok:
            issues.append("audit log failed")
        print(f"\n  PARTIAL — issues: {', '.join(issues)}")
        print("  Check the uvicorn logs in Terminal 1 for details.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Test Service E locally by simulating a Pub/Sub push from Service D."
    )
    parser.add_argument(
        "--url",
        default="http://localhost:8080",
        help="Base URL of the running Service E (default: http://localhost:8080)",
    )
    parser.add_argument(
        "--payload",
        default=None,
        help="Path to a JSON file to use as the payload instead of the built-in test payload. "
             "The file must be a raw Service D payload (will have top-level fields merged in).",
    )
    parser.add_argument(
        "--approval-id",
        default=None,
        help="Override the approval_id to target a specific pending_approvals Firestore document. "
             "Use 'unknown' to skip the Firestore update entirely.",
    )
    parser.add_argument(
        "--no-firestore",
        action="store_true",
        help="Set approval_id to 'unknown' to skip the Firestore update.",
    )
    args = parser.parse_args()

    # Build payload
    payload = dict(FULL_TEST_PAYLOAD)

    if args.payload:
        import os
        if not os.path.exists(args.payload):
            print(f"ERROR: payload file not found: {args.payload}")
            sys.exit(1)
        with open(args.payload) as f:
            custom = json.load(f)
        # Merge — custom file values take priority, defaults fill missing top-level fields
        for key, val in custom.items():
            payload[key] = val
        print(f"Loaded payload from: {args.payload}")

    if args.no_firestore:
        payload["approval_id"] = "unknown"

    if args.approval_id:
        payload["approval_id"] = args.approval_id

    run_test(base_url=args.url.rstrip("/"), payload=payload)
