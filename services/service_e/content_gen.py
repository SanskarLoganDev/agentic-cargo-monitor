"""
Service E — Execution Agent
Content generation module.

Makes a single Claude call to produce all three notification channel payloads
from the approved risk event. This is NOT an agentic loop — it is one structured
generation call that produces deterministic output consumed by notifications.py.

Uses claude-haiku-4-5-20251001 (same model as Service C) — fast and cost-efficient
for content drafting tasks that don't require deep reasoning.

Output schema:
  {
    "email_subject": str,
    "email_body":    str,  — full professional plain-text email body
    "sms_text":      str,  — max 160 characters, sent directly to contact_phone
    "voice_script":  str   — max 25 words, spoken naturally, delivered via ElevenLabs TTS
  }
"""

import json
import logging
import os

import anthropic

logger = logging.getLogger(__name__)

CLAUDE_MODEL      = "claude-haiku-4-5-20251001"
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

# Module-level client — reused across warm Cloud Run instances
_claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM_PROMPT = """You are a pharmaceutical cold-chain emergency notification specialist.
You draft concise, urgent, professional communications when a shipment risk has been
confirmed and a human operator has approved the mitigation plan.

Rules:
1. Return ONLY valid JSON — no preamble, no markdown code fences.
2. email_body must be plain text (no HTML, no markdown). Include: greeting, situation
   summary, specific breach details, approved mitigation plan, and clear next steps.
   Keep it under 400 words. Professional but urgent in tone.
3. sms_text must be 160 characters or fewer. Lead with risk level and drug name.
   No emojis.
4. voice_script must be 25 words or fewer. Written for spoken delivery — natural
   sentences, no abbreviations, no symbols. Start with "Critical alert" or
   "Urgent alert" depending on risk level.
5. Use the drug name, not the drug ID, in all outputs.

Required JSON structure:
{
  "email_subject": "string",
  "email_body":    "string",
  "sms_text":      "string (max 160 chars)",
  "voice_script":  "string (max 25 words)"
}"""


def generate_notification_content(payload: dict) -> dict:
    """
    Generate email, SMS, and voice notification content from the approved risk payload.

    Args:
        payload: The fully decoded execute-actions message dict. Must contain at minimum
                 drug_name, risk_level, overall_assessment, breaches, recommended_actions,
                 mitigation_plan, contact_email, contact_phone.

    Returns:
        Dict with keys: email_subject, email_body, sms_text, voice_script.

    Raises:
        ValueError: If Claude returns non-JSON or the JSON is missing required keys.
    """
    drug_name     = payload.get("drug_name") or payload.get("drug_id", "Unknown Drug")
    risk_level    = payload.get("risk_level", "CRITICAL")
    assessment    = payload.get("overall_assessment", "No assessment provided.")
    breaches      = payload.get("breaches", [])
    actions       = payload.get("recommended_actions", [])
    plan          = payload.get("mitigation_plan", "")
    approved_by   = payload.get("approved_by", "Operator")
    approved_at   = payload.get("approved_at", "")
    spoilage      = payload.get("spoilage_likelihood", "UNKNOWN")
    viable_pct    = payload.get("estimated_viable_units_percent", 0)
    reg_flags     = payload.get("regulatory_flags", [])

    breach_summary = "\n".join(
        f"  - {b.get('parameter', '').upper()}: {b.get('message', '')} "
        f"(deviation: {b.get('deviation', 'N/A')})"
        for b in breaches
    ) or "  - No specific breach details provided."

    actions_summary = "\n".join(
        f"  {i+1}. {a}" for i, a in enumerate(actions)
    ) or "  1. Contact the responsible party immediately."

    reg_summary = ", ".join(reg_flags) if reg_flags else "None flagged."

    user_message = f"""Generate emergency notification content for the following approved risk event.

RISK EVENT SUMMARY:
  Drug Name          : {drug_name}
  Risk Level         : {risk_level}
  Spoilage Likelihood: {spoilage}
  Viable Units       : {viable_pct}%
  Approved By        : {approved_by}
  Approved At        : {approved_at}

ASSESSMENT:
{assessment}

CONFIRMED BREACHES:
{breach_summary}

RECOMMENDED ACTIONS:
{actions_summary}

APPROVED MITIGATION PLAN:
{plan if plan else "Standard cold-chain breach protocol — contact carrier and receiving facility."}

REGULATORY FLAGS:
{reg_summary}

Generate the email subject, email body, SMS text (max 160 chars), and voice script
(max 25 words) for notifying the responsible party. Return ONLY the JSON object."""

    logger.info(
        "Calling Claude for notification content | drug=%s | risk=%s",
        drug_name, risk_level,
    )

    response = _claude.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1200,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )

    raw = response.content[0].text.strip()

    # Strip markdown fences if Claude wraps the JSON
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else raw
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    logger.info(
        "Claude content generation complete | input_tokens=%d output_tokens=%d",
        response.usage.input_tokens,
        response.usage.output_tokens,
    )

    content = json.loads(raw)

    # Validate required keys are present
    required_keys = {"email_subject", "email_body", "sms_text", "voice_script"}
    missing = required_keys - set(content.keys())
    if missing:
        raise ValueError(
            f"Claude response missing required keys: {missing}. Raw: {raw[:200]}"
        )

    # Hard-enforce SMS length — truncate if Claude exceeded the limit
    if len(content["sms_text"]) > 160:
        logger.warning(
            "SMS text exceeded 160 chars (%d) — truncating",
            len(content["sms_text"]),
        )
        content["sms_text"] = content["sms_text"][:157] + "..."

    logger.info(
        "Content generated | subject='%s' | sms_len=%d | voice_words=%d",
        content["email_subject"],
        len(content["sms_text"]),
        len(content["voice_script"].split()),
    )

    return content
