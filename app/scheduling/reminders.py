"""
Post-call appointment reminders and follow-up SMS.

Reminder sequence (sent via GHL automations where possible, Twilio SMS as fallback):
  1. Confirmation SMS — immediately after booking (this module)
  2. 24h reminder — GHL automation triggered tag "reminder-24h"
  3. 2h reminder  — GHL automation triggered tag "reminder-2h"
  4. No-show follow-up — GHL automation triggered tag "no-show"

TCPA compliance:
  - SMS only sent toCallerIDs that provided explicit consent (`sms_consent=True`)
  - Every outbound SMS includes "Reply STOP to opt out"
  - All consent status is stored in GHL contact custom fields

This module handles:
  - Immediate confirmation SMS (step 1 and fallback for 2+3)
  - Tagging the GHL contact to trigger GHL automations for steps 2+3
  - No-show follow-up queuing (step 4)
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import settings
from app.crm.ghl_client import get_ghl_client

logger = logging.getLogger(__name__)

# GHL tag that triggers the reminder automation workflow
_TAG_REMINDER_24H = "reminder-24h"
_TAG_REMINDER_2H = "reminder-2h"
_TAG_CONFIRM_SENT = "confirmation-sent"


# ─── TCPA guard ───────────────────────────────────────────────────────────────

def _stop_footer(language: str) -> str:
    if language == "es":
        return "\nResponda STOP para cancelar mensajes."
    return "\nReply STOP to opt out."


async def _check_sms_consent(contact_id: str) -> bool:
    """
    Check whether the GHL contact has given SMS consent.
    Reads `sms_consent` custom field from the contact.
    Returns True if consent is explicitly 'yes'; defaults to False (TCPA safe).
    """
    ghl = get_ghl_client()
    contact = await ghl.get_contact(contact_id)
    if not contact:
        return False
    custom = contact.get("customField") or contact.get("custom_fields") or {}
    consent = str(custom.get("sms_consent", "")).lower()
    return consent in ("yes", "true", "1", "opt-in")


# ─── Confirmation SMS ─────────────────────────────────────────────────────────

async def send_confirmation_sms(
    contact_id: str,
    appointment_datetime_iso: str,  # ISO 8601 UTC
    caller_name: str = "",
    language: str = "en",
    tz: ZoneInfo | None = None,
) -> bool:
    """
    Send an immediate appointment confirmation SMS via GHL.
    Only sends if sms_consent is True.
    Returns True if sent successfully.
    """
    if not await _check_sms_consent(contact_id):
        logger.info(f"SMS confirmation skipped — no consent on contact {contact_id}")
        return False

    ghl = get_ghl_client()
    tz = tz or settings.tz

    # Format local time
    try:
        dt_utc = datetime.fromisoformat(appointment_datetime_iso.replace("Z", "+00:00"))
        dt_local = dt_utc.astimezone(tz)
        time_str = dt_local.strftime("%A, %B %-d at %-I:%M %p %Z")
    except Exception:
        time_str = appointment_datetime_iso

    name_part = f", {caller_name}" if caller_name else ""

    if language == "es":
        body = (
            f"Hola{name_part}! Su consulta de inmigración ha sido confirmada para "
            f"{time_str}. "
            f"Si necesita reprogramar, llámenos. "
            f"{settings.law_firm_name}"
            f"{_stop_footer('es')}"
        )
    else:
        body = (
            f"Hi{name_part}! Your immigration consultation is confirmed for "
            f"{time_str}. "
            f"If you need to reschedule, please call us. "
            f"{settings.law_firm_name}"
            f"{_stop_footer('en')}"
        )

    ghl = get_ghl_client()
    sent = await ghl.send_sms(contact_id=contact_id, message=body)

    if sent:
        # Tag the contact to trigger GHL reminder automation
        await ghl.add_tags(contact_id, [_TAG_CONFIRM_SENT, _TAG_REMINDER_24H, _TAG_REMINDER_2H])
        logger.info(f"Confirmation SMS sent to contact {contact_id}")
    else:
        logger.warning(f"GHL send_sms failed for {contact_id} — queuing Twilio fallback")
        await _queue_twilio_sms_fallback(contact_id, body)

    return sent


# ─── No-show follow-up ────────────────────────────────────────────────────────

async def schedule_no_show_follow_up(
    contact_id: str,
    appointment_id: str,
    redis,
) -> None:
    """
    Queue a no-show follow-up task.
    The background DB worker picks this up and sends the follow-up SMS
    if the appointment status is still 'noShow' after 4 hours.
    """
    payload = {
        "type": "no_show_followup",
        "contact_id": contact_id,
        "appointment_id": appointment_id,
    }
    try:
        await redis.rpush("follow_up_queue", json.dumps(payload))
        # Also tag contact for GHL no-show automation
        ghl = get_ghl_client()
        await ghl.add_tags(contact_id, ["no-show-candidate"])
        logger.info(f"No-show follow-up queued for contact {contact_id}")
    except Exception as exc:
        logger.warning(f"Failed to queue no-show follow-up: {exc}")


# ─── Voicemail follow-up ──────────────────────────────────────────────────────

async def send_voicemail_follow_up_sms(
    contact_id: str,
    caller_name: str = "",
    language: str = "en",
) -> bool:
    """
    Send a callback confirmation SMS after a caller leaves a voicemail.
    Only sends if sms_consent is True.
    """
    if not await _check_sms_consent(contact_id):
        return False

    ghl = get_ghl_client()
    name_part = f", {caller_name}" if caller_name else ""
    firm = settings.law_firm_name

    if language == "es":
        body = (
            f"Hola{name_part}! Recibimos su mensaje en {firm}. "
            f"Un representante se comunicará con usted dentro de las próximas 2 horas hábiles."
            f"{_stop_footer('es')}"
        )
    else:
        body = (
            f"Hi{name_part}! We received your voicemail at {firm}. "
            f"A representative will reach out within the next 2 business hours."
            f"{_stop_footer('en')}"
        )

    sent = await ghl.send_sms(contact_id=contact_id, message=body)
    if not sent:
        await _queue_twilio_sms_fallback(contact_id, body)
    return sent


# ─── Twilio SMS fallback ──────────────────────────────────────────────────────

async def _queue_twilio_sms_fallback(contact_id: str, message: str) -> None:
    """
    Queue an SMS to be sent via Twilio REST API when GHL messaging fails.
    This requires a background worker to consume `twilio_sms_queue`.
    """
    # We can't send directly here without the phone number (would need another GHL lookup).
    # The worker in the DB writer resolves contact_id → phone and calls Twilio.
    try:
        from app.dependencies import get_redis_client
        redis = get_redis_client()
        payload = {"contact_id": contact_id, "message": message}
        await redis.rpush("twilio_sms_queue", json.dumps(payload))
    except Exception as exc:
        logger.error(f"Failed to queue Twilio SMS fallback: {exc}")
