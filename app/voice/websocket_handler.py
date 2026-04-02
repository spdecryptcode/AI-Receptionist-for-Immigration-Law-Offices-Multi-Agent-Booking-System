"""
Twilio Media Streams WebSocket handler — the core call pipeline.

Flow per call:
  1. Twilio connects via WebSocket to /ws/call
  2. We receive the "connected" event (no audio yet)
  3. We receive "start" event with call metadata → provision session
  4. We play the opening greeting (LLM → TTS → Twilio)
  5. Loop:
     a. Receive "media" events → decode mulaw → send to Deepgram
     b. Deepgram fires on_transcript(text) → send to LLM
     c. LLM streams response tokens → buffer into sentences → TTS
     d. TTS yields mulaw chunks → send back over WebSocket to Twilio
     e. Check for signals: EMERGENCY_TRANSFER, END_CALL, language switch
  6. "stop" event → close Deepgram, extract intake data, persist to DB

Barge-in:
  When Deepgram fires SpeechStarted while TTS is playing, we set a flag.
  The TTS loop checks this flag and stops sending audio mid-stream.

Concurrency:
  Each call runs in its own asyncio.Task registered in main._active_calls.
  asyncio.Semaphore(max_concurrent_calls) limits total concurrent calls.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.config import settings
from app.dependencies import get_redis_client
from app.voice.audio_utils import (
    twilio_payload_to_deepgram,
    elevenlabs_to_twilio_payload,
)
from app.voice.stt_deepgram import DeepgramSTT
from app.voice.tts_elevenlabs import ElevenLabsTTS  # noqa: F401 — re-enable when ElevenLabs credits restored
from app.voice.tts_openai_fallback import OpenAIFallbackTTS as ElevenLabsTTS  # TEMP: OpenAI TTS fallback
from app.agent.llm_agent import ImmigrationAgent, ConversationPhase
from app.voice.conversation_state import CallState, load_call_state, save_call_state
from app.voice.context_manager import ContextManager
from app.agent.intake_flow import build_next_question_hint, extract_field_from_response, next_question
from app.agent.urgency_classifier import create_urgency_task
from app.crm.contact_manager import lookup_caller, sync_call_to_crm
from app.logging_analytics.structured_logger import log_event, TimedOperation
from app.logging_analytics.cost_tracker import CallCostTracker

logger = logging.getLogger(__name__)
router = APIRouter(tags=["websocket"])

# ---------------------------------------------------------------------------
# Concurrency gate — prevent overload
# ---------------------------------------------------------------------------
_call_semaphore: Optional[asyncio.Semaphore] = None


def _get_semaphore() -> asyncio.Semaphore:
    global _call_semaphore
    if _call_semaphore is None:
        _call_semaphore = asyncio.Semaphore(settings.max_concurrent_calls)
    return _call_semaphore


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------

@router.websocket("/ws/call")
async def websocket_call_handler(websocket: WebSocket):
    """
    Main entry point for Twilio Media Streams.
    Each call gets its own instance of this coroutine running as an asyncio Task.
    """
    await websocket.accept()

    sem = _get_semaphore()
    if sem.locked():
        logger.warning("At max concurrent calls capacity — rejecting new WebSocket")
        await websocket.close(code=1013)  # Try Again Later
        return

    async with sem:
        from app.main import _active_calls
        task = asyncio.current_task()
        if task:
            _active_calls.add(task)
        try:
            await _run_call(websocket)
        finally:
            if task:
                _active_calls.discard(task)


# ---------------------------------------------------------------------------
# Core call state machine
# ---------------------------------------------------------------------------

class CallSession:
    """Holds all mutable state for a single call."""

    def __init__(self, websocket: WebSocket):
        self.ws = websocket
        self.call_sid: str = ""
        self.from_number: str = ""
        self.to_number: str = ""
        self.stream_sid: str = ""
        self.language: str = "en"
        self.agent: Optional[ImmigrationAgent] = None
        self.stt: Optional[DeepgramSTT] = None
        self.tts: Optional[ElevenLabsTTS] = None

        # Conversation state (persisted to Redis)
        self.state: Optional[CallState] = None
        self.context: Optional[ContextManager] = None

        # GHL contact ID (from CRM lookup)
        self.ghl_contact_id: Optional[str] = None

        # Pipeline coordination
        self._transcript_queue: asyncio.Queue[str] = asyncio.Queue()
        self._barge_in_flag: asyncio.Event = asyncio.Event()
        self._is_speaking: bool = False
        self._call_active: bool = True

        # Low-confidence consecutive counter (VERIFICATION.md Tests 8 & 9)
        self._low_conf_streak: int = 0

        # Urgency classification background task
        self._urgency_task: Optional[asyncio.Task] = None

        # Timing
        self.started_at: float = time.monotonic()

        # Per-call cost tracking
        self.cost_tracker: CallCostTracker = CallCostTracker("")


async def _run_call(websocket: WebSocket) -> None:
    """Orchestrate a single call from start to finish."""
    session = CallSession(websocket)
    redis = get_redis_client()

    try:
        await _await_start(session)
        if not session.call_sid:
            logger.error("Never received stream start event")
            return

        logger.info(f"Call started: {session.call_sid} from {session.from_number}")
        session.cost_tracker.call_sid = session.call_sid

        caller_name, ghl_contact_id = await lookup_caller(session.from_number, redis)
        session.ghl_contact_id = ghl_contact_id
        returning = bool(caller_name)

        # Initialize conversation state (load from Redis if returning caller)
        session.state = await load_call_state(session.call_sid, redis)
        session.state.language = session.language
        session.context = ContextManager(session.state)

        # Seed phone + known name into intake so dashboard never shows "Unknown"
        if session.from_number and not session.state.intake.get("caller_phone"):
            session.state.intake["caller_phone"] = session.from_number
        if caller_name and not session.state.intake.get("full_name"):
            session.state.intake["full_name"] = caller_name

        session.agent = ImmigrationAgent(
            call_sid=session.call_sid,
            caller_phone=session.from_number,
            language=session.language,
            caller_name=caller_name,
            returning_client=returning,
        )
        session.tts = ElevenLabsTTS(language=session.language, call_sid=session.call_sid)  # noqa: E501

        async def on_transcript(text: str, confidence: float, detected_lang: str) -> None:
            # Discard transcripts that arrive while the agent is speaking — this is
            # Twilio echoing the outbound TTS audio back through the inbound stream.
            # Exception: if the caller intentionally barged in (barge_in_flag is set),
            # allow the transcript through so we can interrupt the agent's response.
            if session._is_speaking and not session._barge_in_flag.is_set():
                logger.debug(f"[{session.call_sid}] Discarding transcript during agent speech (echo): {text!r}")
                return

            if detected_lang.startswith("es") and session.language == "en":
                logger.info(f"[{session.call_sid}] Detected Spanish — switching language")
                session.language = "es"
                session.agent.switch_language("es")
                session.tts = ElevenLabsTTS(language="es", call_sid=session.call_sid)
                if session.state:
                    session.state.language = "es"

            # Low-confidence guard (VERIFICATION.md Tests 8 & 9)
            if confidence < 0.5 and text:
                session._low_conf_streak += 1
                if session._low_conf_streak == 3:
                    # Three consecutive low-confidence turns — offer handoff
                    await session._transcript_queue.put(
                        "__LOW_CONF_HANDOFF__"
                    )
                elif session._low_conf_streak < 3:
                    await _speak(
                        session,
                        "I'm sorry, I didn't quite catch that. Could you please repeat?"
                        if session.language == "en"
                        else "Lo siento, no entendí bien. ¿Puede repetir, por favor?",
                    )
                return  # don't forward low-confidence utterance to LLM

            session._low_conf_streak = 0
            await session._transcript_queue.put(text)

        async def on_speech_start() -> None:
            if session._is_speaking:
                logger.debug(f"[{session.call_sid}] Barge-in detected")
                session._barge_in_flag.set()

        async def on_utterance_end() -> None:
            pass  # queue is already receiving final transcripts

        session.stt = DeepgramSTT(
            on_transcript=on_transcript,
            on_speech_start=on_speech_start,
            on_utterance_end=on_utterance_end,
            call_sid=session.call_sid,
        )

        await redis.hset(f"call:{session.call_sid}", mapping={
            "from": session.from_number,
            "to": session.to_number,
            "status": "active",
            "started_at": str(session.started_at),
        })

        async with session.stt:
            await _play_greeting(session)

            audio_task = asyncio.create_task(_receive_audio_loop(session))
            llm_task = asyncio.create_task(_llm_tts_loop(session, redis))
            timeout_task = asyncio.create_task(_duration_guard(session))

            done, pending = await asyncio.wait(
                [audio_task, llm_task, timeout_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
            for t in done:
                if not t.cancelled() and t.exception():
                    raise t.exception()

    except WebSocketDisconnect:
        logger.info(f"[{session.call_sid}] WebSocket disconnected")
    except Exception as exc:
        logger.error(f"[{session.call_sid}] Call error: {exc}", exc_info=True)
    finally:
        session._call_active = False
        await _finalize_call(session, redis)


# ---------------------------------------------------------------------------
# Sub-tasks
# ---------------------------------------------------------------------------

async def _await_start(session: CallSession) -> None:
    """Wait for Twilio's 'start' event to get stream metadata."""
    try:
        async with asyncio.timeout(10):
            async for raw in session.ws.iter_text():
                msg = json.loads(raw)
                event = msg.get("event")
                if event == "start":
                    start = msg.get("start", {})
                    session.call_sid = start.get("callSid", "")
                    session.stream_sid = msg.get("streamSid", "")
                    custom = start.get("customParameters", {})
                    session.from_number = custom.get("From", "")
                    session.to_number = custom.get("To", "")
                    return
    except TimeoutError:
        logger.error("Timed out waiting for stream start event")


async def _receive_audio_loop(session: CallSession) -> None:
    """Receive Twilio media events and forward audio to Deepgram."""
    async for raw in session.ws.iter_text():
        if not session._call_active:
            break
        try:
            msg = json.loads(raw)
            event = msg.get("event")

            if event == "media":
                payload = msg["media"]["payload"]
                linear16_bytes = twilio_payload_to_deepgram(payload)
                if session.stt:
                    await session.stt.send_audio(linear16_bytes)

            elif event == "stop":
                logger.info(f"[{session.call_sid}] Received 'stop' event")
                session._call_active = False
                break

        except Exception as exc:
            logger.error(f"[{session.call_sid}] Audio receive error: {exc}", exc_info=True)


async def _llm_tts_loop(session: CallSession, redis) -> None:
    """
    Main response loop: dequeue caller transcripts → LLM → TTS → Twilio.
    """
    silence_warned = False
    last_speech_time = time.monotonic()

    while session._call_active:
        try:
            try:
                transcript = await asyncio.wait_for(
                    session._transcript_queue.get(),
                    timeout=1.0,
                )
            except asyncio.TimeoutError:
                elapsed = time.monotonic() - last_speech_time
                if not silence_warned and elapsed > settings.silence_warning_timeout:
                    silence_warned = True
                    await _speak(session, "Are you still there? Take your time, I'm happy to wait.")
                elif elapsed > settings.silence_hard_timeout:
                    logger.info(f"[{session.call_sid}] Hard silence timeout — ending call")
                    await _speak(session, "I haven't heard from you for a while. Please call us back when you're ready. Have a good day.")
                    session._call_active = False
                continue

            last_speech_time = time.monotonic()
            silence_warned = False
            session._barge_in_flag.clear()

            # Low-confidence handoff sentinel (3 consecutive low-conf turns)
            if transcript == "__LOW_CONF_HANDOFF__":
                logger.info(f"[{session.call_sid}] 3 low-confidence turns — offering handoff")
                msg = (
                    "I'm having some trouble hearing you clearly. You can call us back anytime, "
                    f"or I can text you our intake form to complete at your convenience. "
                    f"You can also reach us directly at {settings.office_direct_number}."
                    if session.language == "en"
                    else
                    f"Tengo problemas para escucharle bien. Puede llamarnos en cualquier momento "
                    f"o le enviamos un formulario por mensaje de texto. "
                    f"También puede llamar directamente al {settings.office_direct_number}."
                )
                await _speak(session, msg)
                session._call_active = False
                continue

            # Update conversation state with caller turn
            if session.state and session.context:
                await session.context.add_turn("user", transcript)
                session.state.increment_turns()

                # Log this turn (fire-and-forget)
                from app.logging_analytics.call_logger import log_turn
                asyncio.create_task(
                    log_turn(
                        call_sid=session.call_sid,
                        turn_index=session.state.turn_count,
                        role="caller",
                        text=transcript,
                        phase=session.state.phase.value if session.state.phase else "",
                    ),
                    name=f"log_user:{session.call_sid}:{session.state.turn_count}",
                )

                # Build intake hint for LLM — only during intake phases, not once pitch has begun
                if (not session.agent or session.agent.phase in (
                    ConversationPhase.GREETING,
                    ConversationPhase.IDENTIFICATION,
                    ConversationPhase.URGENCY_TRIAGE,
                    ConversationPhase.INTAKE,
                )):
                    intake_hint = build_next_question_hint(session.state)
                else:
                    intake_hint = ""
            else:
                intake_hint = ""

            full_response = await _stream_llm_to_tts(session, transcript, intake_hint=intake_hint)

            # Reset silence timer after agent finishes speaking so the 15s window
            # starts from *now* (end of TTS), not from when the caller last spoke.
            # Without this, long agent responses (pitch, slot listing) consume most
            # of silence_warning_timeout before the caller even has a chance to reply.
            last_speech_time = time.monotonic()
            silence_warned = False

            if not full_response:
                continue

            # Update state with assistant turn
            if session.state and session.context:
                await session.context.add_turn("assistant", full_response)

                # Log assistant turn (fire-and-forget)
                from app.logging_analytics.call_logger import log_turn as _log_turn
                asyncio.create_task(
                    _log_turn(
                        call_sid=session.call_sid,
                        turn_index=session.state.turn_count,
                        role="assistant",
                        text=full_response,
                        phase=session.state.phase.value if session.state.phase else "",
                    ),
                    name=f"log_ai:{session.call_sid}:{session.state.turn_count}",
                )

                # Fast-path intake extraction from caller's response.
                # Must run during all phases where intake_hint is active, not just INTAKE —
                # otherwise answers given during IDENTIFICATION/URGENCY_TRIAGE are not saved
                # and the same question (e.g. full_name) is asked again on the next turn.
                if session.state.phase in (
                    ConversationPhase.GREETING,
                    ConversationPhase.IDENTIFICATION,
                    ConversationPhase.URGENCY_TRIAGE,
                    ConversationPhase.INTAKE,
                ):
                    q = next_question(session.state)
                    if q:
                        val = extract_field_from_response(q.field, transcript)
                        if val:
                            session.state.record_intake(q.field, val)

                # Trigger urgency classification after URGENCY_TRIAGE phase starts
                if (
                    session.state.phase in (ConversationPhase.URGENCY_TRIAGE, ConversationPhase.INTAKE)
                    and session._urgency_task is None
                    and session.state.urgency_score == 0
                ):
                    session._urgency_task = create_urgency_task(session.state, redis)

                await save_call_state(session.state, redis)

            if not full_response:
                continue

            if session.agent:
                signals = session.agent.check_signals(full_response)
                if signals.get("emergency_transfer"):
                    await _handle_emergency_transfer(session)
                    return
                if signals.get("schedule_now") and not (session.state and session.state.intake.get("_pending_slots_full")):
                    await _handle_schedule_now(session, redis)
                if signals.get("confirm_slot"):
                    await _handle_booking_confirmed(session, signals["confirm_slot"], redis)
                if signals.get("end_call"):
                    session._call_active = False
                    return
                if signals.get("language_switch_es"):
                    session.language = "es"
                    session.tts = ElevenLabsTTS(language="es", call_sid=session.call_sid)
                    if session.state:
                        session.state.language = "es"
                elif signals.get("language_switch_en"):
                    session.language = "en"
                    session.tts = ElevenLabsTTS(language="en", call_sid=session.call_sid)
                    if session.state:
                        session.state.language = "en"

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"[{session.call_sid}] LLM/TTS loop error: {exc}", exc_info=True)


async def _play_greeting(session: CallSession) -> None:
    """Stream the opening greeting from LLM → TTS → Twilio."""
    if not session.agent:
        return

    async def token_iter() -> AsyncIterator[str]:
        async for token in session.agent.greeting_stream():
            yield token

    await _stream_tts_to_twilio(session, session.tts.stream_tokens(token_iter()))


async def _stream_llm_to_tts(session: CallSession, transcript: str, intake_hint: str = "") -> str:
    """Stream LLM tokens → TTS → Twilio. Returns full response text."""
    if not session.agent or not session.tts:
        return ""

    # Build and inject runtime context before each LLM call
    now = datetime.now(settings.tz)
    date_str = now.strftime("%A, %B %-d, %Y, %-I:%M %p %Z")
    ctx_parts = [f"Today is {date_str}."]

    if session.state:
        pending_full = session.state.intake.get("_pending_slots_full", [])
        if pending_full:
            slot_lines = "\n".join(
                f"- {s.get('display', s.get('startTime', ''))} [ISO: {s.get('startTime', '')}]"
                for s in pending_full[:5]
            )
            ctx_parts.append(
                f"Slots already offered to caller — emit CONFIRM_SLOT:ISO when caller confirms one:\n{slot_lines}"
            )

    if intake_hint:
        ctx_parts.append(intake_hint)

    session.agent.runtime_context = "\n\n".join(ctx_parts)

    full_response = ""

    async def token_iterator() -> AsyncIterator[str]:
        nonlocal full_response
        async for token in session.agent.respond_stream(transcript):
            full_response += token
            yield token

    await _stream_tts_to_twilio(session, session.tts.stream_tokens(token_iterator()))
    if full_response:
        session.cost_tracker.add_elevenlabs_chars(len(full_response))
    return full_response


async def _stream_tts_to_twilio(session: CallSession, audio_gen) -> None:
    """Forward mulaw audio chunks from TTS to Twilio. Stops on barge-in."""
    session._is_speaking = True
    session._barge_in_flag.clear()

    try:
        async for mulaw_chunk in audio_gen:
            if session._barge_in_flag.is_set():
                logger.debug(f"[{session.call_sid}] Barge-in — stopping TTS")
                break
            if not session._call_active:
                break
            b64_audio = elevenlabs_to_twilio_payload(mulaw_chunk)
            try:
                await session.ws.send_text(json.dumps({
                    "event": "media",
                    "streamSid": session.stream_sid,
                    "media": {"payload": b64_audio},
                }))
            except Exception as send_exc:
                # WebSocket closed mid-stream — mark call ended and stop sending
                logger.warning(
                    f"[{session.call_sid}] WebSocket send failed during TTS — "
                    f"treating as disconnect: {send_exc}"
                )
                session._call_active = False
                break
    except Exception as exc:
        logger.error(f"[{session.call_sid}] TTS→Twilio error: {exc}", exc_info=True)
        session._call_active = False
    finally:
        session._is_speaking = False


async def _speak(session: CallSession, text: str) -> None:
    """Synthesize and play a static text string."""
    if not session.tts:
        return

    session.cost_tracker.add_elevenlabs_chars(len(text))

    async def _gen() -> AsyncIterator[bytes]:
        async for chunk in session.tts.stream_text(text):
            yield chunk

    await _stream_tts_to_twilio(session, _gen())


async def _duration_guard(session: CallSession) -> None:
    """Enforce soft + hard call duration limits."""
    await asyncio.sleep(settings.call_duration_soft_minutes * 60)
    if session._call_active:
        logger.info(f"[{session.call_sid}] Soft duration limit reached")
        await _speak(
            session,
            "Just a heads up — we want to make sure we have all your information. "
            "We have a few more minutes. Shall we continue?",
        )

    await asyncio.sleep(
        (settings.call_duration_hard_minutes - settings.call_duration_soft_minutes) * 60
    )
    if session._call_active:
        logger.info(f"[{session.call_sid}] Hard duration limit — ending call")
        await _speak(
            session,
            "Thank you so much for your time today. We have your information and "
            "an attorney will follow up. Have a wonderful day.",
        )
        session._call_active = False


async def _handle_emergency_transfer(session: CallSession) -> None:
    """Transfer call to the on-call attorney for detained/emergency cases."""
    logger.warning(f"[{session.call_sid}] EMERGENCY_TRANSFER — routing to attorney")

    if settings.oncall_attorney_phone:
        from twilio.rest import Client as TwilioRestClient
        tw_client = TwilioRestClient(
            settings.twilio_account_sid, settings.twilio_auth_token
        )
        try:
            tw_client.calls(session.call_sid).update(
                twiml=(
                    f'<Response><Dial timeout="30" action="/twilio/status">'
                    f'<Number>{settings.oncall_attorney_phone}</Number>'
                    f'</Dial></Response>'
                )
            )
            logger.info(f"[{session.call_sid}] Emergency transfer to {settings.oncall_attorney_phone}")
        except Exception as exc:
            logger.error(f"[{session.call_sid}] Emergency transfer failed: {exc}")
            await _speak(
                session, "I'm connecting you to our emergency line right now. Please hold."
            )
    else:
        logger.error(f"[{session.call_sid}] ONCALL_ATTORNEY_PHONE not configured!")
        await _speak(
            session,
            "I'm going to have someone call you back immediately. "
            "Please stay available on this number.",
        )

    session._call_active = False


async def _handle_schedule_now(session: CallSession, redis) -> None:
    """Present available slots and book an appointment when caller is ready."""
    # Note: we proceed even if ghl_contact_id is not yet set — the contact will be
    # created eagerly in _handle_booking_confirmed right before the GHL booking call.

    try:
        from app.scheduling.calendar_service import get_available_slots, format_slots_for_speech
        # force_fresh=True bypasses Redis cache so we always show real-time availability
        slots = await get_available_slots(days_ahead=5, redis=redis, force_fresh=True)

        if not slots:
            logger.info(f"[{session.call_sid}] No available slots found")
            return

        # Offer slots to caller
        slot_speech = format_slots_for_speech(slots, language=session.language)
        await _speak(session, slot_speech)

        # Store full slot dicts so _handle_booking_confirmed can look up the ISO
        if session.state:
            session.state.intake["_pending_slots_full"] = slots[:5]
            session.state.intake["_pending_slots"] = [s.get("startTime", "") for s in slots[:5]]
            await save_call_state(session.state, redis)

        # Inject slot context into agent so it knows what ISOs to emit in CONFIRM_SLOT
        if session.agent and slots:
            now = datetime.now(settings.tz)
            date_str = now.strftime("%A, %B %-d, %Y, %-I:%M %p %Z")
            slot_lines = "\n".join(
                f"- {s.get('display', s.get('startTime', ''))} [ISO: {s.get('startTime', '')}]"
                for s in slots[:5]
            )
            session.agent.runtime_context = (
                f"Today is {date_str}.\n\n"
                f"Available slots just offered to caller — emit CONFIRM_SLOT:ISO when caller confirms:\n{slot_lines}"
            )

    except Exception as exc:
        logger.error(f"[{session.call_sid}] Schedule now failed: {exc}", exc_info=True)


async def _handle_booking_confirmed(session: CallSession, confirm_iso: str, redis) -> None:
    """Called when the LLM emits CONFIRM_SLOT:{ISO}. Writes the appointment to GHL + Google Calendar.

    Uses a Redis distributed lock (NX+EX) on the slot key to prevent two concurrent
    callers from booking the same slot simultaneously.  If the lock is already held,
    the slot is being booked by another caller — we fall back to the next pending slot,
    and if none are left we re-fetch live availability and offer a fresh choice.
    """
    if not session.state or not session.ghl_contact_id:
        # For new callers the GHL contact isn't created until _finalize_call.
        # Eagerly create/sync it now so we have a contact_id for the GHL booking.
        if session.state and not session.ghl_contact_id:
            logger.info(f"[{session.call_sid}] CONFIRM_SLOT: no GHL contact yet — eager-creating contact")
            try:
                from app.crm.contact_manager import sync_call_to_crm
                resolved = await sync_call_to_crm(
                    state=session.state,
                    ghl_contact_id=None,
                    lead_score=0,
                    redis=redis,
                )
                if resolved:
                    session.ghl_contact_id = resolved
                    logger.info(f"[{session.call_sid}] Eager GHL contact created: {resolved}")
            except Exception as exc:
                logger.error(f"[{session.call_sid}] Eager CRM sync failed: {exc}")

        if not session.state or not session.ghl_contact_id:
            logger.warning(f"[{session.call_sid}] CONFIRM_SLOT received but missing state/contact_id")
            return

    pending_full = session.state.intake.get("_pending_slots_full", [])
    if not pending_full:
        logger.warning(f"[{session.call_sid}] CONFIRM_SLOT received but no pending slots in state")
        return

    # Match confirmed ISO to one of the offered slots
    matched_slot = None
    matched_index = 0
    for i, slot in enumerate(pending_full):
        slot_iso = slot.get("startTime", "")
        if confirm_iso[:16] and (slot_iso.startswith(confirm_iso[:16]) or confirm_iso.startswith(slot_iso[:16])):
            matched_slot = slot
            matched_index = i
            break

    if not matched_slot:
        matched_slot = pending_full[0]
        matched_index = 0
        logger.warning(
            f"[{session.call_sid}] CONFIRM_SLOT iso={confirm_iso!r} did not match any slot — using first slot"
        )

    from app.scheduling.calendar_service import book_appointment, get_available_slots, format_slots_for_speech
    from app.config import settings as _cfg

    # Ensure caller name is available before booking so the calendar event title is populated.
    # For new callers the name hasn't been extracted yet (that happens at call end), so do a
    # quick partial extraction from the conversation transcript now if full_name is missing.
    if session.agent and not session.state.intake.get("full_name"):
        try:
            partial = await session.agent.extract_intake_data()
            full = (
                partial.get("full_name")
                or " ".join(filter(None, [partial.get("first_name"), partial.get("last_name")]))
            ).strip()
            if full:
                session.state.intake["full_name"] = full
                await save_call_state(session.state, redis)
                logger.info(f"[{session.call_sid}] Extracted caller name for booking: {full!r}")
        except Exception as exc:
            logger.warning(f"[{session.call_sid}] Pre-booking name extraction failed: {exc}")

    # Try slots in order (matched first, then remaining) until we get one we can lock
    candidates = [matched_slot] + [s for j, s in enumerate(pending_full) if j != matched_index]

    booked_slot = None
    for candidate in candidates:
        start_iso = candidate.get("startTime", "")
        lock_key = f"slot_lock:{_cfg.ghl_calendar_id}:{start_iso}"

        # SET lock_key call_sid NX EX 30  — atomic "book it if free"
        acquired = await redis.set(lock_key, session.call_sid, nx=True, ex=30)
        if not acquired:
            logger.info(
                f"[{session.call_sid}] Slot {start_iso} is being booked by another caller — skipping"
            )
            continue

        # We hold the lock — attempt the actual booking
        try:
            appt = await book_appointment(
                contact_id=session.ghl_contact_id,
                slot=candidate,
                caller_name=session.state.intake.get("full_name", ""),
                caller_email=session.state.intake.get("email", ""),
                case_type=session.state.intake.get("case_type", ""),
                language=session.language,
                redis=redis,
            )
        except Exception as exc:
            logger.error(f"[{session.call_sid}] book_appointment raised: {exc}", exc_info=True)
            appt = None
        finally:
            # Always release the lock after the API call
            await redis.delete(lock_key)

        if appt:
            booked_slot = candidate
            session.state.scheduled_at = candidate.get("startTime", confirm_iso)
            session.state.appointment_id = appt.get("id") or appt.get("appointment_id", "")
            session.state.intake.pop("_pending_slots_full", None)
            session.state.intake.pop("_pending_slots", None)
            await save_call_state(session.state, redis)
            logger.info(
                f"[{session.call_sid}] Appointment booked at {session.state.scheduled_at} (id={session.state.appointment_id})"
            )
            break
        else:
            logger.error(
                f"[{session.call_sid}] GHL rejected booking for slot {start_iso} — trying next"
            )

    if not booked_slot:
        # All offered slots failed — fetch fresh availability and ask again
        logger.warning(
            f"[{session.call_sid}] All pending slots unavailable — re-fetching live availability"
        )
        try:
            fresh_slots = await get_available_slots(days_ahead=5, redis=redis, force_fresh=True)
            if fresh_slots:
                session.state.intake["_pending_slots_full"] = fresh_slots[:5]
                session.state.intake["_pending_slots"] = [s.get("startTime", "") for s in fresh_slots[:5]]
                await save_call_state(session.state, redis)
                slot_speech = format_slots_for_speech(fresh_slots, language=session.language)
                retry_msg = (
                    f"I'm sorry, those slots just became unavailable. Here are the next openings: {slot_speech}"
                    if session.language == "en"
                    else
                    f"Lo siento, esos horarios ya no están disponibles. Estos son los próximos: {slot_speech}"
                )
                await _speak(session, retry_msg)
            else:
                no_slots_msg = (
                    "I'm sorry, we don't have any available slots at the moment. "
                    "An attorney will call you back to schedule at a convenient time."
                    if session.language == "en"
                    else
                    "Lo siento, no tenemos horarios disponibles en este momento. "
                    "Un abogado le llamará para programar una cita en un momento conveniente."
                )
                await _speak(session, no_slots_msg)
        except Exception as exc:
            logger.error(f"[{session.call_sid}] Re-fetch slots failed: {exc}", exc_info=True)


async def _finalize_call(session: CallSession, redis) -> None:
    """Post-call cleanup: extract intake data, run lead scorer, sync to CRM, update Redis."""
    logger.info(f"[{session.call_sid}] Finalizing call")

    # Cancel any pending urgency task
    if session._urgency_task and not session._urgency_task.done():
        session._urgency_task.cancel()

    intake_data: dict = {}
    if session.agent:
        try:
            await session.agent.extract_intake_data()
            intake_data = session.agent.intake_data
            logger.info(
                f"[{session.call_sid}] Intake fields: {list(intake_data.keys())}"
            )
        except Exception as exc:
            logger.error(f"[{session.call_sid}] Intake extraction failed: {exc}")

    # Merge LLM-extracted intake into CallState.
    # LLM GPT-4o extraction (JSON mode) is authoritative — it overwrites any
    # fast-path heuristic values stored during the call (e.g. "Yes." stored as
    # full_name from the caller's first utterance).  Private keys starting with
    # "_" (slot scheduling state) are never overwritten.
    if session.state and intake_data:
        for k, v in intake_data.items():
            if v is not None and not k.startswith("_"):
                session.state.intake[k] = v

    # Lead scoring (post-call, non-blocking best-effort)
    lead_score = 0
    if session.state:
        try:
            from app.agent.lead_scorer import LeadScorer
            scorer = LeadScorer(session.call_sid)
            breakdown = await scorer.score(session.state, redis)
            lead_score = breakdown.total
        except Exception as exc:
            logger.error(f"[{session.call_sid}] Lead scoring failed: {exc}")

    # CRM sync (GHL + Supabase queue)
    if session.state:
        try:
            resolved_contact_id = await sync_call_to_crm(
                state=session.state,
                ghl_contact_id=session.ghl_contact_id,
                lead_score=lead_score,
                redis=redis,
            )
            # Capture newly-created contact ID so the SMS check below can use it
            if resolved_contact_id and not session.ghl_contact_id:
                session.ghl_contact_id = resolved_contact_id
        except Exception as exc:
            logger.error(f"[{session.call_sid}] CRM sync failed: {exc}")

    # Confirmation SMS if appointment was booked
    if session.state and session.state.scheduled_at and session.ghl_contact_id:
        try:
            from app.scheduling.reminders import send_confirmation_sms
            caller_name = session.state.intake.get("full_name", "")
            await send_confirmation_sms(
                contact_id=session.ghl_contact_id,
                appointment_datetime_iso=session.state.scheduled_at,
                caller_name=caller_name,
                language=session.state.language,
            )
        except Exception as exc:
            logger.warning(f"[{session.call_sid}] Confirmation SMS failed: {exc}")

    # Launch full post-call analytics pipeline as background task
    if session.context:
        conversation = session.context.get_full_history() if hasattr(session.context, "get_full_history") else []
        asyncio.create_task(
            _run_post_call_pipeline(
                call_sid=session.call_sid,
                conversation=conversation,
                intake=session.state.intake if session.state else {},
                language=session.language,
                phone=session.from_number,
                ghl_contact_id=session.ghl_contact_id or "",
                duration_sec=int(time.monotonic() - session.started_at),
            ),
            name=f"post_call:{session.call_sid[:12]}",
        )

    # Save final call state to Redis
    if session.state:
        await save_call_state(session.state, redis)

    elapsed = time.monotonic() - session.started_at

    # Finalize cost (Deepgram billed on total call duration; OpenAI tokens read from agent)
    session.cost_tracker.add_deepgram_seconds(elapsed)
    if session.agent:
        session.cost_tracker.add_openai_tokens(
            input=session.agent._total_input_tokens,
            output=session.agent._total_output_tokens,
        )
    await session.cost_tracker.persist(redis)

    await redis.hset(f"call:{session.call_sid}", mapping={
        "status": "ended",
        "duration_seconds": int(elapsed),
        "turn_count": session.state.turn_count if session.state else 0,
    })
    await redis.expire(f"call:{session.call_sid}", 86400)  # 24h TTL

    # Analytics event
    await log_event(
        "call_ended",
        call_sid=session.call_sid,
        duration_seconds=int(elapsed),
        lead_score=lead_score,
        language=session.language,
    )

    try:
        await session.ws.close()
    except Exception:
        pass


async def _run_post_call_pipeline(
    call_sid: str,
    conversation: list[dict],
    intake: dict,
    language: str,
    phone: str,
    ghl_contact_id: str,
    duration_sec: int,
) -> None:
    """Thin wrapper so call_logger import stays lazy."""
    from app.logging_analytics.call_logger import run_post_call_pipeline
    await run_post_call_pipeline(
        call_sid=call_sid,
        conversation=conversation,
        intake=intake,
        language=language,
        phone=phone,
        ghl_contact_id=ghl_contact_id,
        duration_sec=duration_sec,
    )
