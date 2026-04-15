"""
Service E — Execution Agent
Notification delivery module.

Three independent notification channels — each can succeed or fail without
blocking the others. Results are collected and returned to main.py for
inclusion in the BigQuery audit log.

Channels:
  1. Email  — Gmail SMTP via smtplib (stdlib — no extra dependency)
  2. SMS    — Twilio Programmable SMS (trial: pre-verified numbers only)
  3. Voice  — ElevenLabs TTS → GCS (public MP3) → Twilio outbound call

Note on client initialisation:
  Twilio and GCS clients are module-level (reused across warm instances).
  The Gmail SMTP connection is opened per-send — SMTP connections are stateful
  and time out during Cloud Run idle periods, so a persistent connection is not safe.

Environment variables required:
  GOOGLE_CLOUD_PROJECT
  VOICE_NOTES_BUCKET       — GCS bucket for temporary MP3 storage
  GMAIL_USER               — full Gmail address used as sender (e.g. you@gmail.com)
  GMAIL_APP_PASSWORD       — 16-char App Password from Google Account settings
                             Requires 2FA on the account.
                             Generate at: https://myaccount.google.com/apppasswords
  TWILIO_ACCOUNT_SID
  TWILIO_AUTH_TOKEN
  TWILIO_PHONE_NUMBER      — your provisioned Twilio number (E.164 format)
  ELEVENLABS_API_KEY
  ELEVENLABS_VOICE_ID      — voice ID from ElevenLabs library
                             (default: 21m00Tcm4TlvDq8ikWAM = Rachel)
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
PROJECT_ID             = os.environ["GOOGLE_CLOUD_PROJECT"]
VOICE_NOTES_BUCKET     = os.environ.get("VOICE_NOTES_BUCKET", f"{PROJECT_ID}-voice-notes")

GMAIL_USER             = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD     = os.environ["GMAIL_APP_PASSWORD"]

TWILIO_ACCOUNT_SID     = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN      = os.environ["TWILIO_AUTH_TOKEN"]
TWILIO_PHONE_NUMBER    = os.environ["TWILIO_PHONE_NUMBER"]

ELEVENLABS_API_KEY     = os.environ["ELEVENLABS_API_KEY"]
ELEVENLABS_VOICE_ID    = os.environ.get("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")
ELEVENLABS_TTS_URL     = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"

# ---------------------------------------------------------------------------
# Module-level clients — reused across warm Cloud Run instances
# Note: Gmail SMTP connection is NOT module-level — see send_email() below.
# ---------------------------------------------------------------------------
_gcs_client    = storage.Client(project=PROJECT_ID)
_twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)


# ---------------------------------------------------------------------------
# Channel 1 — Email via Gmail SMTP
# ---------------------------------------------------------------------------

def send_email(
    to_email:  str,
    subject:   str,
    body:      str,
) -> dict:
    """
    Send a plain-text email via Gmail SMTP using an App Password.

    Opens a fresh SMTP connection per call — SMTP connections are stateful
    and time out during Cloud Run idle periods, making a module-level
    persistent connection unreliable in a serverless environment.

    Prerequisites:
      - 2-Step Verification must be enabled on the Google account.
      - Generate a 16-char App Password at:
        https://myaccount.google.com/apppasswords
        (App name: AgenticTerps or any label you prefer)
      - Set GMAIL_USER to the full Gmail address (e.g. you@gmail.com)
      - Set GMAIL_APP_PASSWORD to the generated 16-char password

    Returns:
        {"success": True, "to": str} on success.
        {"success": False, "error": str} on failure.
    """
    if not to_email:
        return {"success": False, "error": "No contact_email in payload"}

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

        logger.info("Email sent | from=%s | to=%s", GMAIL_USER, to_email)
        return {"success": True, "to": to_email}

    except smtplib.SMTPAuthenticationError as exc:
        logger.error(
            "Gmail SMTP authentication failed | user=%s | error=%s\n"
            "Ensure GMAIL_APP_PASSWORD is a valid App Password "
            "(not your regular Gmail password) and 2FA is enabled.",
            GMAIL_USER, exc,
        )
        return {"success": False, "error": f"SMTP auth failed: {exc}"}

    except Exception as exc:
        logger.error("Gmail SMTP email failed | to=%s | error=%s", to_email, exc)
        return {"success": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Channel 2 — SMS via Twilio
# ---------------------------------------------------------------------------

def send_sms(to_phone: str, body: str) -> dict:
    """
    Send an SMS via Twilio Programmable SMS.

    On Twilio trial accounts, messages can only be sent to pre-verified numbers.
    The contact_phone values in the Firestore shipment documents must be verified
    in the Twilio Console before this will succeed.

    Returns:
        {"success": True, "message_sid": str} on success.
        {"success": False, "error": str} on failure.
    """
    if not to_phone:
        return {"success": False, "error": "No contact_phone in payload"}

    try:
        message = _twilio_client.messages.create(
            to=to_phone,
            from_=TWILIO_PHONE_NUMBER,
            body=body,
        )
        logger.info(
            "SMS sent | to=%s | sid=%s | status=%s",
            to_phone, message.sid, message.status,
        )
        return {"success": True, "message_sid": message.sid}

    except Exception as exc:
        logger.error("Twilio SMS failed | to=%s | error=%s", to_phone, exc)
        return {"success": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Channel 3a — Voice note generation via ElevenLabs TTS
# ---------------------------------------------------------------------------

def generate_voice_note(script: str) -> Optional[bytes]:
    """
    Call the ElevenLabs TTS API to convert the voice_script to MP3 audio bytes.

    Uses eleven_multilingual_v2 model — available on the free tier.

    Returns:
        Raw MP3 bytes on success, None on failure.
    """
    try:
        headers = {
            "xi-api-key":   ELEVENLABS_API_KEY,
            "Content-Type": "application/json",
            "Accept":       "audio/mpeg",
        }
        body = {
            "text":       script,
            "model_id":   "eleven_multilingual_v2",
            "voice_settings": {
                "stability":        0.5,
                "similarity_boost": 0.75,
            },
        }
        response = requests.post(
            ELEVENLABS_TTS_URL,
            json=body,
            headers=headers,
            timeout=30,
        )
        response.raise_for_status()
        logger.info(
            "ElevenLabs TTS generated | voice_id=%s | audio_bytes=%d",
            ELEVENLABS_VOICE_ID, len(response.content),
        )
        return response.content

    except Exception as exc:
        logger.error("ElevenLabs TTS failed | error=%s", exc)
        return None


# ---------------------------------------------------------------------------
# Channel 3b — Upload MP3 to GCS (public URL)
# ---------------------------------------------------------------------------

def upload_voice_note_to_gcs(
    audio_bytes: bytes,
    drug_id:     str,
    approval_id: str,
) -> Optional[str]:
    """
    Upload the MP3 audio bytes to the voice-notes GCS bucket.

    The bucket is configured with public-read IAM so the resulting URL is
    immediately accessible by Twilio without signed URL complexity.

    Returns:
        Public GCS URL string on success, None on failure.
    """
    timestamp  = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    blob_name  = f"voice-note-{drug_id}-{approval_id}-{timestamp}.mp3"

    try:
        bucket = _gcs_client.bucket(VOICE_NOTES_BUCKET)
        blob   = bucket.blob(blob_name)
        blob.upload_from_string(audio_bytes, content_type="audio/mpeg")

        public_url = f"https://storage.googleapis.com/{VOICE_NOTES_BUCKET}/{blob_name}"
        logger.info(
            "Voice note uploaded to GCS | bucket=%s | blob=%s | url=%s",
            VOICE_NOTES_BUCKET, blob_name, public_url,
        )
        return public_url

    except Exception as exc:
        logger.error("GCS voice note upload failed | blob=%s | error=%s", blob_name, exc)
        return None


# ---------------------------------------------------------------------------
# Channel 3c — Outbound voice call via Twilio
# ---------------------------------------------------------------------------

def make_voice_call(to_phone: str, audio_url: str) -> dict:
    """
    Place an outbound Twilio voice call that plays the voice note MP3.

    Uses inline TwiML with <Play> verb — no separate TwiML endpoint needed.
    On Twilio trial accounts, calls can only be made to pre-verified numbers.

    Returns:
        {"success": True, "call_sid": str} on success.
        {"success": False, "error": str} on failure.
    """
    if not to_phone:
        return {"success": False, "error": "No contact_phone in payload"}

    try:
        twiml_response = VoiceResponse()
        twiml_response.play(audio_url)

        call = _twilio_client.calls.create(
            to=to_phone,
            from_=TWILIO_PHONE_NUMBER,
            twiml=str(twiml_response),
        )
        logger.info(
            "Voice call placed | to=%s | sid=%s | status=%s",
            to_phone, call.sid, call.status,
        )
        return {"success": True, "call_sid": call.sid}

    except Exception as exc:
        logger.error("Twilio voice call failed | to=%s | error=%s", to_phone, exc)
        return {"success": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Orchestrator — run all three channels and return collected results
# ---------------------------------------------------------------------------

def execute_all_channels(
    payload:       dict,
    content:       dict,
) -> dict:
    """
    Execute all three notification channels independently.
    A failure in one channel does not prevent the others from running.

    Args:
        payload: The decoded execute-actions message dict.
        content: The generated notification content dict from content_gen.py.

    Returns:
        Dict with results for each channel:
        {
            "email":       {"success": bool, ...},
            "sms":         {"success": bool, ...},
            "voice_call":  {"success": bool, ...},
        }
    """
    contact_email = payload.get("contact_email", "")
    contact_phone = payload.get("contact_phone", "")
    drug_id       = payload.get("drug_id", "unknown")
    approval_id   = payload.get("approval_id", str(uuid.uuid4()))

    results: dict = {}

    # ── Channel 1: Email ────────────────────────────────────────────────
    logger.info("Executing channel: email | to=%s", contact_email)
    results["email"] = send_email(
        to_email=contact_email,
        subject=content["email_subject"],
        body=content["email_body"],
    )

    # ── Channel 2: SMS ──────────────────────────────────────────────────
    logger.info("Executing channel: SMS | to=%s", contact_phone)
    results["sms"] = send_sms(
        to_phone=contact_phone,
        body=content["sms_text"],
    )

    # ── Channel 3: Voice note ───────────────────────────────────────────
    logger.info("Executing channel: voice | to=%s", contact_phone)

    audio_bytes = generate_voice_note(content["voice_script"])

    if audio_bytes is None:
        results["voice_call"] = {
            "success": False,
            "error":   "ElevenLabs TTS generation failed — voice call skipped",
        }
    else:
        gcs_url = upload_voice_note_to_gcs(audio_bytes, drug_id, approval_id)
        if gcs_url is None:
            results["voice_call"] = {
                "success": False,
                "error":   "GCS upload failed — voice call skipped",
            }
        else:
            results["voice_call"] = make_voice_call(
                to_phone=contact_phone,
                audio_url=gcs_url,
            )
            results["voice_call"]["gcs_url"] = gcs_url

    # ── Summary ─────────────────────────────────────────────────────────
    successes = sum(1 for r in results.values() if r.get("success"))
    logger.info(
        "Notification channels complete | %d/3 succeeded | "
        "email=%s | sms=%s | voice=%s",
        successes,
        results["email"].get("success"),
        results["sms"].get("success"),
        results["voice_call"].get("success"),
    )

    return results
