"""
Service E — Execution Agent
Notification delivery module.

Two active channels:
  1. Email  — Gmail SMTP via smtplib (stdlib — no dependency needed)
  2. Voice  — ElevenLabs TTS → GCS (public MP3) → Twilio outbound call

SMS channel is DISABLED. The sms_body field on recovery_actions is ignored entirely.

Execution model:
  execute_recovery_actions(actions) iterates over the list from Service D.
  For each action:
    - Has recipient_email + email_subject + email_body → send email
    - Otherwise                                        → log as acknowledged

  send_voice_notification(phone, script) is called once after all actions,
  independently of the per-action loop.

Note on client initialisation:
  GCS and Twilio clients are module-level (reused across warm instances).
  Gmail SMTP connection is opened per-send — SMTP connections are stateful
  and time out during Cloud Run idle periods.

Environment variables required:
  GOOGLE_CLOUD_PROJECT
  VOICE_NOTES_BUCKET
  GMAIL_USER
  GMAIL_APP_PASSWORD
  TWILIO_ACCOUNT_SID
  TWILIO_AUTH_TOKEN
  TWILIO_PHONE_NUMBER
  ELEVENLABS_API_KEY
  ELEVENLABS_VOICE_ID      (default: 21m00Tcm4TlvDq8ikWAM = Rachel)
"""

import logging
import os
import smtplib
import uuid
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

import requests
from google.cloud import storage
from twilio.rest import Client as TwilioClient
from twilio.twiml.voice_response import VoiceResponse

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
PROJECT_ID          = os.environ["GOOGLE_CLOUD_PROJECT"]
VOICE_NOTES_BUCKET  = os.environ.get("VOICE_NOTES_BUCKET", f"{PROJECT_ID}-voice-notes")

GMAIL_USER          = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD  = os.environ["GMAIL_APP_PASSWORD"]

TWILIO_ACCOUNT_SID  = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN   = os.environ["TWILIO_AUTH_TOKEN"]
TWILIO_PHONE_NUMBER = os.environ["TWILIO_PHONE_NUMBER"]

ELEVENLABS_API_KEY  = os.environ["ELEVENLABS_API_KEY"]
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")
ELEVENLABS_TTS_URL  = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"

# ---------------------------------------------------------------------------
# Module-level clients — reused across warm Cloud Run instances
# Gmail SMTP is NOT module-level — see send_email() for the reason.
# ---------------------------------------------------------------------------
_gcs_client    = storage.Client(project=PROJECT_ID)
_twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)


# ---------------------------------------------------------------------------
# Email via Gmail SMTP
# ---------------------------------------------------------------------------

def send_email(to_email: str, subject: str, body: str) -> dict:
    """
    Send a plain-text email via Gmail SMTP using an App Password.

    Opens a fresh SMTP connection per call — connections time out during
    Cloud Run idle periods so a persistent module-level connection is unsafe.

    Returns:
        {"success": True, "to": str, "subject": str} on success.
        {"success": False, "error": str} on failure.
    """
    if not to_email:
        return {"success": False, "error": "recipient email is empty"}

    try:
        msg = MIMEMultipart()
        msg["From"]    = GMAIL_USER
        msg["To"]      = to_email
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_USER, to_email, msg.as_string())

        logger.info(
            "Email sent | to=%s | subject='%s'",
            to_email, subject[:60],
        )
        return {"success": True, "to": to_email, "subject": subject}

    except smtplib.SMTPAuthenticationError as exc:
        logger.error(
            "Gmail SMTP auth failed | user=%s | "
            "Check GMAIL_APP_PASSWORD is a valid App Password with 2FA enabled | %s",
            GMAIL_USER, exc,
        )
        return {"success": False, "error": f"SMTP auth failed: {exc}"}

    except Exception as exc:
        logger.error("Gmail SMTP failed | to=%s | error=%s", to_email, exc)
        return {"success": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Recovery action executor
# ---------------------------------------------------------------------------

def execute_recovery_actions(actions: list) -> list:
    """
    Iterate over the recovery_actions list from Service D and execute each step.

    For actions that have recipient_email + email_subject + email_body:
        Send the pre-written email via Gmail SMTP.

    For all other actions (QUARANTINE, CONTACT_CARRIER, SPOILAGE_ASSESSMENT,
    LOG_COMPLIANCE, POTENCY_TESTING — no email fields):
        Log the action as acknowledged. These are operational/physical steps
        that a human must carry out; Service E records that the instruction
        was dispatched.

    Args:
        actions: List of dicts or RecoveryAction-like objects from the payload.

    Returns:
        List of result dicts, one per action, in step order.
        Each result:
          {"step": int, "action_type": str, "title": str,
           "channel": "email" | "logged",
           "success": bool, ...channel-specific fields}
    """
    results = []

    for action in actions:
        # Support both Pydantic model objects and plain dicts
        if hasattr(action, "model_dump"):
            a = action.model_dump()
        elif hasattr(action, "__dict__"):
            a = vars(action)
        else:
            a = action

        step        = a.get("step", 0)
        action_type = a.get("action_type", "UNKNOWN")
        title       = a.get("title", "")
        email       = a.get("recipient_email")
        subject     = a.get("email_subject")
        body        = a.get("email_body")

        has_email_content = bool(email and subject and body)

        if has_email_content:
            # Send the pre-written email from Service D
            logger.info(
                "Executing email action | step=%d | type=%s | to=%s",
                step, action_type, email,
            )
            result = send_email(to_email=email, subject=subject, body=body)
            result.update({"step": step, "action_type": action_type,
                           "title": title, "channel": "email"})
        else:
            # Log as acknowledged — physical/operational step for human execution
            logger.info(
                "Acknowledging action (no email required) | step=%d | type=%s | title=%s",
                step, action_type, title,
            )
            result = {
                "step":        step,
                "action_type": action_type,
                "title":       title,
                "channel":     "logged",
                "success":     True,
            }

        results.append(result)

    return results


# ---------------------------------------------------------------------------
# Voice notification (ElevenLabs TTS → GCS → Twilio outbound call)
# ---------------------------------------------------------------------------

def _generate_voice_note(script: str) -> Optional[bytes]:
    """Call ElevenLabs TTS and return raw MP3 bytes."""
    try:
        response = requests.post(
            ELEVENLABS_TTS_URL,
            json={
                "text":     script,
                "model_id": "eleven_multilingual_v2",
                "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
            },
            headers={
                "xi-api-key":   ELEVENLABS_API_KEY,
                "Content-Type": "application/json",
                "Accept":       "audio/mpeg",
            },
            timeout=30,
        )
        response.raise_for_status()
        logger.info(
            "ElevenLabs TTS generated | voice_id=%s | bytes=%d",
            ELEVENLABS_VOICE_ID, len(response.content),
        )
        return response.content
    except Exception as exc:
        logger.error("ElevenLabs TTS failed | error=%s", exc)
        return None


def _upload_to_gcs(audio_bytes: bytes, filename: str) -> Optional[str]:
    """Upload MP3 to the public voice-notes bucket and return the public URL."""
    try:
        bucket = _gcs_client.bucket(VOICE_NOTES_BUCKET)
        blob   = bucket.blob(filename)
        blob.upload_from_string(audio_bytes, content_type="audio/mpeg")
        url = f"https://storage.googleapis.com/{VOICE_NOTES_BUCKET}/{filename}"
        logger.info("Voice note uploaded | bucket=%s | blob=%s", VOICE_NOTES_BUCKET, filename)
        return url
    except Exception as exc:
        logger.error("GCS upload failed | filename=%s | error=%s", filename, exc)
        return None


def send_voice_notification(to_phone: str, script: str) -> dict:
    """
    Generate a voice note via ElevenLabs TTS, upload to GCS, and place a
    Twilio outbound call to to_phone that plays the MP3.

    Args:
        to_phone: E.164 phone number of the recipient.
        script:   Short spoken text (≤25 words) for the voice note.

    Returns:
        {"success": True, "call_sid": str, "gcs_url": str} on success.
        {"success": False, "error": str} on any failure.
    """
    if not to_phone:
        return {"success": False, "error": "No phone number available for voice notification"}

    # Step 1 — Generate audio
    audio_bytes = _generate_voice_note(script)
    if audio_bytes is None:
        return {"success": False, "error": "ElevenLabs TTS generation failed"}

    # Step 2 — Upload to GCS
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename  = f"voice-note-{uuid.uuid4().hex[:8]}-{timestamp}.mp3"
    gcs_url   = _upload_to_gcs(audio_bytes, filename)
    if gcs_url is None:
        return {"success": False, "error": "GCS upload failed"}

    # Step 3 — Place Twilio call
    try:
        twiml = VoiceResponse()
        twiml.play(gcs_url)

        call = _twilio_client.calls.create(
            to=to_phone,
            from_=TWILIO_PHONE_NUMBER,
            twiml=str(twiml),
        )
        logger.info(
            "Voice call placed | to=%s | sid=%s | status=%s",
            to_phone, call.sid, call.status,
        )
        return {"success": True, "call_sid": call.sid, "gcs_url": gcs_url}

    except Exception as exc:
        logger.error("Twilio voice call failed | to=%s | error=%s", to_phone, exc)
        return {"success": False, "error": str(exc), "gcs_url": gcs_url}
