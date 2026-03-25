"""
Call routing — office hours check and overflow / after-hours handling.

This module is called from twilio_webhooks.py when an inbound call arrives.
It decides:
  1. Is the office open? (based on OFFICE_HOURS_START/END + OFFICE_TIMEZONE)
  2. Is the system at max capacity? (checked via _accepting_connections flag in main.py)
  3. Route accordingly:
     - Open + capacity available → AI agent (return WebSocket TwiML)
     - Open + at capacity → voicemail queue with callback promise
     - After hours → after-hours message + voicemail
     - Weekend → weekend message + voicemail
     - Holiday (US federal) → optional, returns same as after-hours

Returns TwiML XML strings for each routing outcome.

TwiML responses generated here are passed back in the HTTP response to Twilio.
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import settings

logger = logging.getLogger(__name__)

# Exported constants — used by outbound_callback.py for re-queue delay calculation
OFFICE_OPEN_HOUR: int = 9
OFFICE_CLOSE_HOUR: int = 22
OFFICE_TZ: str = "America/New_York"  # kept in sync with settings.office_timezone

# We use a simple holiday check rather than a full holiday library
# to avoid adding a dependency. Extend _US_FEDERAL_HOLIDAYS as needed.
_US_FEDERAL_HOLIDAYS_MMDD = {
    "01-01",  # New Year's Day
    "07-04",  # Independence Day
    "11-11",  # Veterans Day
    "12-25",  # Christmas Day
}


# ─── Office hours check ───────────────────────────────────────────────────────

def is_office_open(now: datetime | None = None) -> bool:
    """
    Return True if the current time falls within configured office hours.
    Currently set to 24/7 — always open.
    """
    return True

    tz: ZoneInfo = settings.tz  # noqa: unreachable — kept for reference
    now = now or datetime.now(tz)

    # Weekend
    if now.weekday() >= 5:  # Sat=5, Sun=6
        return False

    # Federal holiday
    mmdd = now.strftime("%m-%d")
    if mmdd in _US_FEDERAL_HOLIDAYS_MMDD:
        return False

    # Office hours window
    try:
        start_h, start_m = map(int, settings.office_hours_start.split(":"))
        end_h, end_m = map(int, settings.office_hours_end.split(":"))
    except ValueError:
        logger.warning("Invalid office_hours_start/end config — defaulting to always open")
        return True

    open_time = now.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
    close_time = now.replace(hour=end_h, minute=end_m, second=0, microsecond=0)

    return open_time <= now < close_time


# ─── TwiML builders ──────────────────────────────────────────────────────────

def twiml_ai_agent(base_url: str, call_sid: str, from_number: str, to_number: str) -> str:
    """Return TwiML to connect the call to the AI agent WebSocket."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f'<Connect>'
        f'<Stream url="wss://{base_url}/ws/call">'
        f'<Parameter name="CallSid" value="{call_sid}"/>'
        f'<Parameter name="From" value="{from_number}"/>'
        f'<Parameter name="To" value="{to_number}"/>'
        f"</Stream>"
        f"</Connect>"
        "</Response>"
    )



def twiml_after_hours(language: str = "en") -> str:
    """Return TwiML for after-hours greeting + voicemail prompt."""
    if language == "es":
        message = (
            "Gracias por llamar. Nuestro horario de atención es de lunes a viernes "
            "de nueve de la mañana a diez de la noche. Por favor deje su nombre, número "
            "y una descripción breve de su situación migratoria después del tono y nos "
            "comunicaremos con usted el siguiente día hábil."
        )
    else:
        message = (
            "Thank you for calling. Our office hours are Monday through Friday, "
            "nine AM to ten PM. Please leave your name, phone number, and a brief "
            "description of your immigration matter after the tone and we will return "
            "your call the next business day."
        )
    return _voicemail_twiml(message)


def twiml_at_capacity(language: str = "en") -> str:
    """Return TwiML when all agent slots are in use."""
    if language == "es":
        message = (
            "Gracias por llamar. En este momento todos nuestros representantes están "
            "atendiendo otras llamadas. Por favor deje su mensaje después del tono y "
            "le devolveremos la llamada lo antes posible, generalmente dentro de dos horas."
        )
    else:
        message = (
            "Thank you for calling. Our representatives are currently assisting other "
            "callers. Please leave a message after the tone and we will return your call "
            "as soon as possible, typically within two hours."
        )
    return _voicemail_twiml(message)


def twiml_recording_consent() -> str:
    """Return TwiML to announce call recording (required by many state laws)."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        '<Say voice="Polly.Joanna" language="en-US">'
        "This call may be recorded for quality and training purposes."
        "</Say>"
        "</Response>"
    )


def _voicemail_twiml(message: str) -> str:
    """Return TwiML to play a message and record voicemail."""
    record_action = f"https://{settings.base_host}/twilio/voicemail"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f'<Say voice="Polly.Joanna" language="en-US">{message}</Say>'
        '<Record action="' + record_action + '" '
        'maxLength="120" '
        'transcribe="true" '
        'transcribeCallback="' + record_action + '/transcription"'
        "/>"
        "<Say>We did not receive a recording. Goodbye.</Say>"
        "</Response>"
    )


# ─── High-level routing decision ─────────────────────────────────────────────

def route_inbound_call(
    call_sid: str,
    from_number: str,
    to_number: str,
    accepting_connections: bool = True,
    language: str = "en",
    now: datetime | None = None,
) -> str:
    """
    Determine the correct TwiML response for an inbound call.
    Returns a TwiML XML string ready to send to Twilio.
    """
    if not is_office_open(now):
        logger.info(f"[{call_sid}] After-hours call from {from_number}")
        return twiml_after_hours(language)

    if not accepting_connections:
        logger.warning(f"[{call_sid}] At capacity — deflecting to voicemail")
        return twiml_at_capacity(language)

    logger.info(f"[{call_sid}] Routing to AI agent")
    return twiml_ai_agent(settings.base_host, call_sid, from_number, to_number)
