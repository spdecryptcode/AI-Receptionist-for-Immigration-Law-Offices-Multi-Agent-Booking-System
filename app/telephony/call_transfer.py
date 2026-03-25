"""
Call transfer — cold transfer, warm transfer (conference bridge), and fallback.

Transfer modes:
  - Cold transfer: `call.update(twiml=<Dial>)` — passes caller directly to attorney.
    No AI briefing. Used for emergencies where speed > context.

  - Warm transfer: conference bridge where AI:
    1. Plays a whisper to the attorney leg with a 30-second case summary
    2. Then connects both legs into a conference bridge
    3. AI drops off once attorney acknowledges
    Used for standard consultations where context handoff matters.

  - Fallback: no answer after 30s → return caller to AI → offer voicemail / callback.

All Twilio REST calls run in a thread executor (Twilio library is sync).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)


# ─── Cold transfer ────────────────────────────────────────────────────────────

async def cold_transfer(call_sid: str, to_number: str, fallback_action_url: str = "") -> bool:
    """
    Immediately bridge caller to `to_number` via Twilio REST API.

    `fallback_action_url` — Twilio calls this URL if the destination doesn't answer.
    If not provided, defaults to `/twilio/voicemail`.

    Returns True if the API call succeeded.
    """
    action = fallback_action_url or f"https://{settings.base_host}/twilio/transfer-fallback"
    twiml = (
        f'<Response>'
        f'<Dial timeout="30" action="{action}" method="POST">'
        f'<Number>{to_number}</Number>'
        f'</Dial>'
        f'</Response>'
    )
    return await _update_call_twiml(call_sid, twiml, label=f"cold_transfer→{to_number}")


# ─── Warm transfer ────────────────────────────────────────────────────────────

async def warm_transfer(
    call_sid: str,
    to_number: str,
    whisper_text: str,
    conference_name: str,
    language: str = "en",
) -> bool:
    """
    Warm transfer via conference bridge.

    Step 1 — Move caller to a holding conference (they hear hold music):
        caller_leg → <Conference name={conference_name} startConferenceOnEnter=false>

    Step 2 — Dial attorney, play whisper, then add attorney to conference:
        attorney_leg → <Conference name={conference_name} startConferenceOnEnter=true>

    The AI has already dropped by the time both legs are live.

    Returns True if caller TwiML update succeeded (attorney dial is best-effort).
    """
    # Step 1: move caller to waiting conference
    hold_music = "http://com.twilio.sounds.music.s3.amazonaws.com/MARKOVICHAMP.mp3"
    conference_twiml = (
        f'<Response>'
        f'<Say voice="Polly.Joanna" language="en-US">'
        f'{"Please hold while I connect you with an attorney." if language == "en" else "Por favor espere mientras le conecto con un abogado."}'
        f'</Say>'
        f'<Dial>'
        f'<Conference startConferenceOnEnter="false" waitUrl="{hold_music}" '
        f'waitMethod="GET" beep="false">{conference_name}</Conference>'
        f'</Dial>'
        f'</Response>'
    )
    success = await _update_call_twiml(call_sid, conference_twiml, label="warm_transfer_hold")
    if not success:
        return False

    # Step 2: dial attorney via outbound call (async, non-blocking)
    asyncio.create_task(
        _dial_attorney_to_conference(
            to_number=to_number,
            conference_name=conference_name,
            whisper_text=whisper_text,
        ),
        name=f"attorney_dial:{conference_name}",
    )
    return True


async def _dial_attorney_to_conference(
    to_number: str,
    conference_name: str,
    whisper_text: str,
) -> None:
    """
    Outbound call to the attorney with a whisper, then join conference once they pick up.
    Fires as a background task.
    """
    attorney_twiml = (
        f'<Response>'
        f'<Say voice="Polly.Joanna">{whisper_text}</Say>'
        f'<Pause length="1"/>'
        f'<Dial>'
        f'<Conference startConferenceOnEnter="true" endConferenceOnExit="true" '
        f'beep="false">{conference_name}</Conference>'
        f'</Dial>'
        f'</Response>'
    )
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _place_outbound_call_sync, to_number, attorney_twiml)
        logger.info(f"Attorney leg placed for conference {conference_name}")
    except Exception as exc:
        logger.error(f"Attorney dial to conference {conference_name} failed: {exc}")


def _place_outbound_call_sync(to_number: str, twiml: str) -> None:
    from twilio.rest import Client as TwilioClient
    client = TwilioClient(settings.twilio_account_sid, settings.twilio_auth_token)
    client.calls.create(
        to=to_number,
        from_=settings.twilio_phone_number,
        twiml=twiml,
    )


# ─── Transfer fallback webhook helpers ────────────────────────────────────────

def twiml_transfer_no_answer(language: str = "en") -> str:
    """
    TwiML returned when the attorney doesn't answer the transfer.
    Sends caller back to a friendly message + voicemail offer.
    """
    voicemail_url = f"https://{settings.base_host}/twilio/voicemail"
    if language == "es":
        msg = (
            "El abogado no está disponible en este momento. "
            "Por favor deje su mensaje después del tono y nos comunicaremos con usted "
            "lo antes posible."
        )
    else:
        msg = (
            "Our attorney is unavailable right now. "
            "Please leave a message after the tone and we will get back to you "
            "as soon as possible."
        )
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<Response>'
        f'<Say voice="Polly.Joanna" language="en-US">{msg}</Say>'
        f'<Record maxLength="120" action="{voicemail_url}" transcribe="false"/>'
        f'<Say>We did not receive a recording. Goodbye.</Say>'
        f'</Response>'
    )


# ─── Internal helper ──────────────────────────────────────────────────────────

async def _update_call_twiml(call_sid: str, twiml: str, label: str = "") -> bool:
    """Update an active call's TwiML via the Twilio REST API (async, runs in executor)."""
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, _update_call_sync, call_sid, twiml
        )
        logger.info(f"[{call_sid}] Call updated: {label}")
        return True
    except Exception as exc:
        logger.error(f"[{call_sid}] Call update failed ({label}): {exc}")
        return False


def _update_call_sync(call_sid: str, twiml: str) -> None:
    from twilio.rest import Client as TwilioClient
    client = TwilioClient(settings.twilio_account_sid, settings.twilio_auth_token)
    client.calls(call_sid).update(twiml=twiml)


# ─── Convenience: build whisper text ─────────────────────────────────────────

def build_attorney_whisper(intake: dict, urgency_score: int, language: str = "en") -> str:
    """
    Build a concise 30-second whisper summary for the attorney.
    """
    name = intake.get("full_name", "the caller")
    case = intake.get("case_type", "immigration matter")
    status = intake.get("current_immigration_status", "unknown status")
    urgency_word = "URGENT — " if urgency_score >= 6 else ""

    return (
        f"{urgency_word}Incoming transfer from {name}. "
        f"Case type: {case}. Immigration status: {status}. "
        f"Urgency score: {urgency_score} out of 10. "
        f"Press any key or say OK to connect the caller."
    )
