"""
TwiML response builders for every non-WebSocket scenario.

Used when:
  - WebSocket connection fails (circuit breaker open)
  - Office is closed (after-hours)
  - Caller presses a DTMF digit on the IVR fallback menu
  - Voicemail recording flow
  - System is at capacity

All builders return complete TwiML XML strings ready for a Twilio webhook response.
"""
from __future__ import annotations

from app.config import settings


# ─── Constants ────────────────────────────────────────────────────────────────

_AI_STREAM_URL = f"wss://{settings.base_host}/ws/media-stream"

_VOICEMAIL_URL = f"https://{settings.base_host}/twilio/voicemail"
_GATHER_ACTION_URL = f"https://{settings.base_host}/twilio/ivr-menu"
_TRANSFER_FALLBACK_URL = f"https://{settings.base_host}/twilio/transfer-fallback"

_VOICE_EN = "Polly.Joanna-Neural"
_VOICE_ES = "Polly.Lupe-Neural"

_OFFICE_PHONE = getattr(settings, "office_direct_number", settings.twilio_phone_number)


# ─── Primary AI stream TwiML ──────────────────────────────────────────────────

def twiml_ai_stream(call_sid: str = "") -> str:
    """
    Connect caller to the AI via Twilio Media Streams WebSocket.
    This is the main happy-path response.
    """
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Response>'
        f'<Connect>'
        f'<Stream url="{_AI_STREAM_URL}">'
        f'<Parameter name="call_sid" value="{call_sid}"/>'
        f'</Stream>'
        f'</Connect>'
        '</Response>'
    )


# ─── IVR fallback menu ────────────────────────────────────────────────────────

def twiml_ivr_menu(language: str = "en", retry: bool = False) -> str:
    """
    DTMF menu — shown when WebSocket / AI is unavailable.

      1 — New consultation (→ voicemail / callback offer)
      2 — Existing case status (→ attorney voicemail)
      0 — Speak to the front desk (→ cold transfer)

    Gracefully handles no-input/invalid via defaulting to voicemail after 2 retries.
    """
    voice = _VOICE_ES if language == "es" else _VOICE_EN
    if language == "es":
        intro = (
            "Gracias por llamar. Nuestro asistente virtual no está disponible en este momento. "
            if not retry else
            "Lo siento, no escuché su selección. "
        )
        menu = (
            "Para una nueva consulta, oprima uno. "
            "Para verificar el estado de su caso, oprima dos. "
            "Para hablar con recepción, oprima cero."
        )
    else:
        intro = (
            "Thank you for calling. Our AI assistant is temporarily unavailable. "
            if not retry else
            "Sorry, I didn't catch that. "
        )
        menu = (
            "For a new consultation, press one. "
            "To check on an existing case, press two. "
            "To speak with the front desk, press zero."
        )

    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Response>'
        f'<Gather numDigits="1" action="{_GATHER_ACTION_URL}" method="POST" timeout="8">'
        f'<Say voice="{voice}">{intro}{menu}</Say>'
        '</Gather>'
        # No-input fallback: redirect back with retry=1
        f'<Redirect method="POST">{_GATHER_ACTION_URL}?retry=1&amp;lang={language}</Redirect>'
        '</Response>'
    )


def twiml_ivr_digit(digit: str, language: str = "en") -> str:
    """
    Route a DTMF digit from the IVR fallback menu.

      "1" → voicemail/callback offer for new consultation
      "2" → attorney voicemail
      "0" → cold transfer to front desk
      other → replay IVR menu with retry=True
    """
    if digit == "1":
        return twiml_new_consultation_offer(language)
    elif digit == "2":
        return twiml_existing_case_voicemail(language)
    elif digit == "0":
        return twiml_front_desk_transfer(language)
    else:
        return twiml_ivr_menu(language, retry=True)


# ─── New consultation offer ───────────────────────────────────────────────────

def twiml_new_consultation_offer(language: str = "en") -> str:
    """
    Offer new caller a choice: leave voicemail or schedule a callback.
    For callbacks we gather their number and add to queue.
    """
    voice = _VOICE_ES if language == "es" else _VOICE_EN
    callback_url = f"https://{settings.base_host}/twilio/callback-request"
    if language == "es":
        msg = (
            "Para una nueva consulta, puede dejarnos un mensaje ahora "
            "o nosotros le llamaremos de regreso. "
            "Oprima uno para dejar un mensaje. Oprima dos para solicitar una llamada de regreso."
        )
    else:
        msg = (
            "For a new consultation, you can leave us a message now "
            "or we can call you back. "
            "Press one to leave a message. Press two to request a callback."
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Response>'
        f'<Gather numDigits="1" action="{callback_url}" method="POST" timeout="8">'
        f'<Say voice="{voice}">{msg}</Say>'
        '</Gather>'
        # Default: drop into voicemail if no input
        f'{_voicemail_verb(language)}'
        '</Response>'
    )


# ─── Voicemail flows ──────────────────────────────────────────────────────────

def twiml_voicemail(language: str = "en") -> str:
    """Standard voicemail prompt + record verb."""
    voice = _VOICE_ES if language == "es" else _VOICE_EN
    if language == "es":
        msg = (
            "Por favor deje su nombre, número de teléfono y una breve descripción "
            "de su asunto legal después del tono. Le responderemos lo antes posible."
        )
    else:
        msg = (
            "Please leave your name, phone number, and a brief description "
            "of your legal matter after the tone. We will get back to you as soon as possible."
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Response>'
        f'<Say voice="{voice}">{msg}</Say>'
        f'{_voicemail_verb(language)}'
        '</Response>'
    )


def twiml_existing_case_voicemail(language: str = "en") -> str:
    voice = _VOICE_ES if language == "es" else _VOICE_EN
    if language == "es":
        msg = (
            "Para verificar el estado de su caso, deje su nombre y número de caso "
            "después del tono. Un miembro de nuestro equipo le responderá en un día hábil."
        )
    else:
        msg = (
            "To check on your case status, please leave your name and case number "
            "after the tone. A team member will return your call within one business day."
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Response>'
        f'<Say voice="{voice}">{msg}</Say>'
        f'{_voicemail_verb(language)}'
        '</Response>'
    )


def _voicemail_verb(language: str = "en") -> str:
    """Return just the <Record> verb XML (no <Response> wrapper)."""
    return (
        f'<Record maxLength="180" '
        f'action="{_VOICEMAIL_URL}" '
        f'method="POST" '
        f'transcribe="false" '
        f'playBeep="true"/>'
    )


# ─── Transfers ────────────────────────────────────────────────────────────────

def twiml_front_desk_transfer(language: str = "en") -> str:
    """Cold transfer to the direct office line."""
    voice = _VOICE_ES if language == "es" else _VOICE_EN
    if language == "es":
        msg = "Transfiriendo su llamada ahora. Por favor espere."
    else:
        msg = "Transferring your call now. Please hold."
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Response>'
        f'<Say voice="{voice}">{msg}</Say>'
        f'<Dial timeout="30" action="{_TRANSFER_FALLBACK_URL}" method="POST">'
        f'<Number>{_OFFICE_PHONE}</Number>'
        f'</Dial>'
        '</Response>'
    )


def twiml_after_hours(language: str = "en") -> str:
    """After-hours message offering voicemail."""
    voice = _VOICE_ES if language == "es" else _VOICE_EN
    if language == "es":
        msg = (
            "Gracias por llamar. Nuestro horario de atención es de lunes a viernes, "
            "de nueve de la mañana a cinco de la tarde. "
            "Deje su mensaje y le llamaremos el próximo día hábil."
        )
    else:
        msg = (
            "Thank you for calling. Our office hours are Monday through Friday, "
            "nine a.m. to five p.m. "
            "Please leave a message and we will return your call on the next business day."
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Response>'
        f'<Say voice="{voice}">{msg}</Say>'
        f'{_voicemail_verb(language)}'
        '</Response>'
    )


def twiml_at_capacity(language: str = "en") -> str:
    """Queue-full message offering callback or voicemail."""
    voice = _VOICE_ES if language == "es" else _VOICE_EN
    callback_url = f"https://{settings.base_host}/twilio/callback-request"
    if language == "es":
        msg = (
            "Todos nuestros agentes están ocupados. "
            "Oprima uno para recibir una llamada de regreso. "
            "Oprima dos para dejar un mensaje de voz."
        )
    else:
        msg = (
            "All of our agents are currently busy. "
            "Press one to receive a callback. "
            "Press two to leave a voicemail."
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Response>'
        f'<Gather numDigits="1" action="{callback_url}" method="POST" timeout="8">'
        f'<Say voice="{voice}">{msg}</Say>'
        '</Gather>'
        f'{_voicemail_verb(language)}'
        '</Response>'
    )


# ─── Error / hang-up ─────────────────────────────────────────────────────────

def twiml_error_goodbye(language: str = "en") -> str:
    """Graceful hang-up after unrecoverable error."""
    voice = _VOICE_ES if language == "es" else _VOICE_EN
    if language == "es":
        msg = "Lo sentimos, ha ocurrido un error. Por favor llame de nuevo más tarde. Adiós."
    else:
        msg = "We're sorry, an error occurred. Please call again later. Goodbye."
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Response>'
        f'<Say voice="{voice}">{msg}</Say>'
        '<Hangup/>'
        '</Response>'
    )
