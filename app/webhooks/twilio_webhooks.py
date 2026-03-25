"""
Twilio inbound-call webhook and call-status callbacks.

Endpoints:
  POST /twilio/voice              — Inbound call entry → TwiML WebSocket stream
  POST /twilio/status             — Async status callbacks (completed, no-answer, etc.)
  POST /twilio/recording          — Recording status callbacks
  POST /twilio/voicemail          — Voicemail recording complete → async processing
  POST /twilio/ivr-menu           — DTMF digit from IVR fallback menu
  POST /twilio/callback-request   — Caller pressed "callback" in IVR
  POST /twilio/callback-connect   — TwiML served when outbound callback is answered
  POST /twilio/transfer-fallback  — Attorney didn't answer a transfer

All POST bodies are form-encoded (Twilio's native format).
All endpoints validate Twilio's X-Twilio-Signature before processing.
"""
from __future__ import annotations

import asyncio
import json
import logging
import hmac
import hashlib
import base64
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import Response as FastAPIResponse
from twilio.twiml.voice_response import VoiceResponse, Connect, Stream

from app.config import settings
from app.dependencies import get_redis_client
from app.telephony.call_router import route_inbound_call, is_office_open

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/twilio", tags=["twilio"])


# ---------------------------------------------------------------------------
# Twilio signature validation
# ---------------------------------------------------------------------------

def _validate_twilio_signature(
    request_url: str,
    form_params: dict[str, str],
    x_twilio_signature: str,
) -> bool:
    """
    Validate Twilio's HMAC-SHA1 request signature.
    https://www.twilio.com/docs/usage/webhooks/webhooks-security

    Reconstructed: HMAC-SHA1(auth_token, url + sorted_params_concat)
    Returns True if signatures match.
    """
    sorted_params = "".join(
        f"{k}{v}" for k, v in sorted(form_params.items())
    )
    string_to_sign = request_url + sorted_params
    mac = hmac.new(
        settings.twilio_auth_token.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha1,
    )
    expected = base64.b64encode(mac.digest()).decode("utf-8")
    return hmac.compare_digest(expected, x_twilio_signature)


async def _require_twilio_signature(request: Request) -> dict[str, str]:
    """Validate Twilio signature; raise 403 if invalid. Returns form data."""
    sig = request.headers.get("X-Twilio-Signature", "")
    form_data = await request.form()
    params = dict(form_data)
    if not _validate_twilio_signature(str(request.url), params, sig):
        logger.warning("Twilio signature validation failed — rejecting request")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid Twilio signature",
        )
    return params


def _twiml_response(twiml_str: str) -> FastAPIResponse:
    return FastAPIResponse(content=twiml_str, media_type="text/xml")


# ---------------------------------------------------------------------------
# POST /twilio/voice  — inbound call entry point
# ---------------------------------------------------------------------------

@router.post("/voice")
async def inbound_voice(request: Request):
    """
    Respond to an inbound Twilio call with TwiML <Connect><Stream>.
    Upgrades the call to a Media Stream WebSocket for real-time audio.
    """
    params = await _require_twilio_signature(request)

    from app.main import _accepting_connections  # avoid circular at module load

    call_sid = params.get("CallSid", "unknown")
    from_number = params.get("From", "unknown")
    to_number = params.get("To", settings.twilio_phone_number)
    logger.info(f"Inbound call {call_sid} from {from_number}")

    twiml_str = route_inbound_call(
        call_sid=call_sid,
        from_number=from_number,
        to_number=to_number,
        accepting_connections=_accepting_connections,
        language="en",  # Detected later by STT; default English for initial TwiML
    )

    return _twiml_response(twiml_str)


# ---------------------------------------------------------------------------
# POST /twilio/status  — async call status updates
# ---------------------------------------------------------------------------

@router.post("/status")
async def call_status_callback(request: Request):
    """Handle completed/busy/no-answer/failed status from Twilio."""
    params = await _require_twilio_signature(request)

    call_sid = params.get("CallSid", "")
    call_status = params.get("CallStatus", "")
    duration = params.get("CallDuration", "0")
    from_number = params.get("From", "")
    logger.info(f"Call status: {call_sid} → {call_status} ({duration}s)")

    redis = get_redis_client()
    session_key = f"call:{call_sid}"
    async with redis.pipeline(transaction=True) as pipe:
        pipe.hset(session_key, "call_status", call_status)
        pipe.hset(session_key, "duration_seconds", duration)
        await pipe.execute()

    if call_status in ("no-answer", "busy"):
        logger.info(f"Queuing callback for {from_number} ({call_status})")
        await redis.rpush("callback_queue", f"{from_number}:{call_sid}:{call_status}")

    # Safety-net: ensure a conversations row exists for every completed call
    # where the WebSocket pipeline never ran (e.g. caller hung up immediately).
    # Skip if _finalize_call already ran and set status="ended" in Redis —
    # in that case the real full-data row is already written.
    if call_status == "completed" and call_sid:
        try:
            existing_status = await redis.hget(f"call:{call_sid}", "status")
            if existing_status not in (b"ended", "ended"):
                payload = json.dumps({
                    "call_sid": call_sid,
                    "language": "en",
                    "lead_score": -1,
                    "urgency_score": 0,
                    "urgency_label": "low",
                    "intake": {"caller_phone": from_number},
                    "scheduled_at": None,
                    "appointment_id": None,
                    "transferred_at": None,
                    "duration_seconds": int(duration),
                })
                await redis.rpush("db_sync_queue", payload)
        except Exception as _e:
            logger.warning(f"[{call_sid}] Failed to queue safety-net conversations row: {_e}")

    return {"ok": True}


# ---------------------------------------------------------------------------
# POST /twilio/recording  — recording available
# ---------------------------------------------------------------------------

@router.post("/recording")
async def recording_status_callback(request: Request):
    """Store completed recording URL in Redis against the call SID."""
    params = await _require_twilio_signature(request)

    call_sid = params.get("CallSid", "")
    recording_url = params.get("RecordingUrl", "")
    recording_status = params.get("RecordingStatus", "")
    logger.info(f"Recording {recording_status} for call {call_sid}")

    if recording_url and recording_status == "completed":
        redis = get_redis_client()
        await redis.hset(f"call:{call_sid}", "recording_url", recording_url)

    return {"ok": True}


# ---------------------------------------------------------------------------
# POST /twilio/voicemail  — voicemail recording complete
# ---------------------------------------------------------------------------

@router.post("/voicemail")
async def voicemail_callback(request: Request):
    """Queue voicemail for async transcription and thank the caller."""
    params = await _require_twilio_signature(request)

    call_sid = params.get("CallSid", "")
    from_number = params.get("From", "")
    recording_url = params.get("RecordingUrl", "")
    duration = params.get("RecordingDuration", "0")
    logger.info(f"Voicemail from {from_number} ({duration}s): {call_sid}")

    if recording_url and int(duration) > 0:
        import asyncio
        from app.telephony.voicemail import process_voicemail
        recording_sid = params.get("RecordingSid", "")
        asyncio.create_task(
            process_voicemail(
                recording_sid=recording_sid,
                recording_url=recording_url,
                caller_number=from_number,
                call_sid=call_sid,
                duration_sec=int(duration),
            ),
            name=f"voicemail:{call_sid[:12]}",
        )

    from app.telephony.twiml_responses import twiml_error_goodbye
    twiml = VoiceResponse()
    twiml.say(
        "Your message has been received. Someone will call you back shortly. Thank you.",
        voice="Polly.Joanna-Neural",
    )
    twiml.hangup()
    return _twiml_response(str(twiml))


# ---------------------------------------------------------------------------
# POST /twilio/ivr-menu  — DTMF digit from fallback IVR
# ---------------------------------------------------------------------------

@router.post("/ivr-menu")
async def ivr_menu(request: Request):
    """Route a DTMF digit from the IVR fallback menu."""
    params = await _require_twilio_signature(request)
    digit = params.get("Digits", "")
    lang = request.query_params.get("lang", params.get("lang", "en"))
    retry = request.query_params.get("retry", "0") == "1"

    from app.telephony.twiml_responses import twiml_ivr_menu, twiml_ivr_digit
    if not digit:
        return _twiml_response(twiml_ivr_menu(language=lang, retry=retry))
    return _twiml_response(twiml_ivr_digit(digit=digit, language=lang))


# ---------------------------------------------------------------------------
# POST /twilio/callback-request  — Caller wants a callback
# ---------------------------------------------------------------------------

@router.post("/callback-request")
async def callback_request(request: Request):
    """
    Caller pressed the callback option in the IVR or new-consultation menu.
    Enqueue them and play a confirmation.
    """
    params = await _require_twilio_signature(request)
    digit = params.get("Digits", "")
    from_number = params.get("From", "")
    call_sid = params.get("CallSid", "")
    lang = request.query_params.get("lang", "en")

    from app.telephony.twiml_responses import twiml_voicemail

    if digit == "2" or not digit:
        # Enqueue callback
        from app.telephony.outbound_callback import enqueue_callback
        redis = get_redis_client()
        await enqueue_callback(
            redis_client=redis,
            caller_number=from_number,
            language=lang,
            reason="callback request from IVR",
        )
        twiml = VoiceResponse()
        msg = (
            "Hemos registrado su número. Le llamaremos en nuestro próximo horario disponible. Hasta luego."
            if lang == "es" else
            "We have noted your number. We will call you back during our next available time. Goodbye."
        )
        twiml.say(msg, voice="Polly.Joanna-Neural")
        twiml.hangup()
        return _twiml_response(str(twiml))
    else:
        # Digit "1" or anything else → voicemail
        return _twiml_response(twiml_voicemail(lang))


# ---------------------------------------------------------------------------
# POST /twilio/callback-connect  — TwiML for outbound callback answer
# ---------------------------------------------------------------------------

@router.post("/callback-connect")
async def callback_connect(request: Request):
    """
    Served when the callee answers our outbound callback call.
    Connects them to the AI WebSocket with context pre-loaded.
    """
    params = await _require_twilio_signature(request)
    lang = request.query_params.get("lang", "en")
    name = request.query_params.get("name", "")
    call_sid = params.get("CallSid", "")

    from app.telephony.twiml_responses import twiml_ai_stream
    return _twiml_response(twiml_ai_stream(call_sid=call_sid))


# ---------------------------------------------------------------------------
# POST /twilio/transfer-fallback  — Transfer no-answer
# ---------------------------------------------------------------------------

@router.post("/transfer-fallback")
async def transfer_fallback(request: Request):
    """
    Called when the attorney doesn't answer a warm/cold transfer.
    Offers voicemail.
    """
    params = await _require_twilio_signature(request)
    lang = request.query_params.get("lang", "en")
    from app.telephony.call_transfer import twiml_transfer_no_answer
    return _twiml_response(twiml_transfer_no_answer(language=lang))
