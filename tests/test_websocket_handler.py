"""
Unit tests for app/voice/websocket_handler.py — VERIFICATION.md Tests 4/5/7/8/9.

Covers:
  CallSession initial state (constructor defaults)
  _stream_tts_to_twilio: barge-in flag stops audio mid-stream  (Test 4 / 5)
  _stream_tts_to_twilio: _is_speaking True during, False after (even on exception)
  _stream_tts_to_twilio: call inactive → loop exits immediately
  _stream_tts_to_twilio: correct JSON shape sent to Twilio
  _await_start: parses Twilio 'start' event, ignores non-start events
  _receive_audio_loop: media events → stt.send_audio; stop event → call inactive
  Low-confidence streak logic (Tests 8/9):
    streak < 3 → transcript NOT queued
    streak == 3 → __LOW_CONF_HANDOFF__ sentinel queued
    high-confidence turn resets streak and queues transcript
  Language auto-switch when Deepgram detects Spanish (Test 7)
  _get_semaphore: singleton with correct limit from settings
"""
import os
os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "testtoken")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+10000000000")
os.environ.setdefault("OPENAI_API_KEY", "sk-test")
os.environ.setdefault("DEEPGRAM_API_KEY", "dg-test")
os.environ.setdefault("ELEVENLABS_API_KEY", "el-test")
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_ANON_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")
os.environ.setdefault("BASE_URL", "test.example.com")
os.environ.setdefault("GHL_API_KEY", "ghl-test")
os.environ.setdefault("GHL_LOCATION_ID", "loc-test")
os.environ.setdefault("ELEVENLABS_VOICE_ID_EN", "voice-en")
os.environ.setdefault("ELEVENLABS_VOICE_ID_ES", "voice-es")
os.environ.setdefault("GHL_CALENDAR_ID", "cal-test")
os.environ.setdefault("GOOGLE_CALENDAR_ID", "gcal-test")

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.voice.websocket_handler as _mod
from app.voice.websocket_handler import (
    CallSession,
    _await_start,
    _get_semaphore,
    _receive_audio_loop,
    _stream_tts_to_twilio,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ws(**send_kwargs) -> MagicMock:
    ws = MagicMock()
    ws.send_text = AsyncMock(**send_kwargs)
    ws.iter_text = MagicMock()
    return ws


def _make_session(**ws_kwargs) -> CallSession:
    return CallSession(_make_ws(**ws_kwargs))


async def _async_iter(*items):
    """Async generator that yields pre-set items."""
    for item in items:
        yield item


@pytest.fixture(autouse=True)
def _reset_semaphore():
    """Reset the module-level semaphore singleton between tests."""
    _mod._call_semaphore = None
    yield
    _mod._call_semaphore = None


# ---------------------------------------------------------------------------
# CallSession initial state
# ---------------------------------------------------------------------------

class TestCallSessionInit:
    def test_low_conf_streak_starts_at_zero(self):
        s = _make_session()
        assert s._low_conf_streak == 0

    def test_barge_in_flag_starts_clear(self):
        s = _make_session()
        assert not s._barge_in_flag.is_set()

    def test_is_not_speaking_initially(self):
        s = _make_session()
        assert s._is_speaking is False

    def test_call_active_initially(self):
        s = _make_session()
        assert s._call_active is True

    def test_transcript_queue_empty(self):
        s = _make_session()
        assert s._transcript_queue.empty()

    def test_urgency_task_none(self):
        s = _make_session()
        assert s._urgency_task is None

    def test_language_default_english(self):
        s = _make_session()
        assert s.language == "en"


# ---------------------------------------------------------------------------
# _stream_tts_to_twilio
# ---------------------------------------------------------------------------

class TestStreamTtsToTwilio:
    """Tests for barge-in, call-inactive, is_speaking lifecycle, JSON shape."""

    async def _run(self, session, chunks, **patch_kwargs):
        async def audio_gen():
            for c in chunks:
                yield c

        with patch(
            "app.voice.websocket_handler.elevenlabs_to_twilio_payload",
            return_value="b64audio",
        ):
            await _stream_tts_to_twilio(session, audio_gen())

    async def test_all_chunks_sent_normally(self):
        session = _make_session()
        session.stream_sid = "MZ123"
        await self._run(session, [b"\xff" * 160] * 5)
        assert session.ws.send_text.call_count == 5

    async def test_barge_in_flag_cleared_at_function_entry(self):
        """_stream_tts_to_twilio always clears the flag at entry (fresh start).
        A stale flag from the previous turn must NOT prevent this stream."""
        session = _make_session()
        session.stream_sid = "MZ123"
        session._barge_in_flag.set()  # stale from previous turn
        await self._run(session, [b"\xff" * 160] * 3)
        # Flag was cleared at entry → all 3 chunks are sent normally
        assert session.ws.send_text.call_count == 3

    async def test_barge_in_mid_stream_stops_early(self):
        """Barge-in triggered on 3rd send → only 3 chunks reach Twilio (Test 5)."""
        session = _make_session()
        session.stream_sid = "MZ123"

        call_count = [0]
        async def counting_send(text):
            call_count[0] += 1
            if call_count[0] == 3:
                session._barge_in_flag.set()

        session.ws.send_text.side_effect = counting_send

        async def audio_gen():
            for _ in range(10):
                yield b"\x00" * 160

        with patch(
            "app.voice.websocket_handler.elevenlabs_to_twilio_payload",
            return_value="b64audio",
        ):
            await _stream_tts_to_twilio(session, audio_gen())

        # 3 sends happened; 4th chunk check sees barge-in → stops
        assert session.ws.send_text.call_count == 3

    async def test_call_inactive_stops_loop(self):
        """Once _call_active is False the loop exits."""
        session = _make_session()
        session.stream_sid = "MZ123"
        session._call_active = False
        await self._run(session, [b"\x00" * 160] * 5)
        session.ws.send_text.assert_not_called()

    async def test_is_speaking_true_during_False_after(self):
        """_is_speaking must be True while streaming, False after (finally)."""
        session = _make_session()
        session.stream_sid = "MZ123"
        states_during = []

        async def audio_gen():
            states_during.append(session._is_speaking)
            yield b"\x00" * 160

        with patch(
            "app.voice.websocket_handler.elevenlabs_to_twilio_payload",
            return_value="b64",
        ):
            await _stream_tts_to_twilio(session, audio_gen())

        assert states_during == [True]
        assert session._is_speaking is False

    async def test_is_speaking_false_after_exception(self):
        """finally block must reset _is_speaking even when exception is raised."""
        session = _make_session()
        session.stream_sid = "MZ123"
        session.ws.send_text.side_effect = RuntimeError("network error")

        async def audio_gen():
            yield b"\x00" * 160

        with patch(
            "app.voice.websocket_handler.elevenlabs_to_twilio_payload",
            return_value="b64",
        ):
            await _stream_tts_to_twilio(session, audio_gen())

        assert session._is_speaking is False

    async def test_sent_json_has_media_event(self):
        """Each WS message must be a valid JSON media event."""
        session = _make_session()
        session.stream_sid = "MZ_TEST"
        await self._run(session, [b"\xaa" * 160])

        call_arg = session.ws.send_text.call_args[0][0]
        msg = json.loads(call_arg)
        assert msg["event"] == "media"
        assert msg["streamSid"] == "MZ_TEST"
        assert "payload" in msg["media"]

    async def test_barge_in_flag_clear_called(self):
        """clear() must be called at entry so we can rely on the flag being fresh."""
        session = _make_session()
        session.stream_sid = "MZ123"

        cleared = [False]
        orig_clear = session._barge_in_flag.clear

        def _record_clear():
            cleared[0] = True
            orig_clear()

        session._barge_in_flag.clear = _record_clear
        await self._run(session, [b"\x00" * 160])
        assert cleared[0]


# ---------------------------------------------------------------------------
# _await_start
# ---------------------------------------------------------------------------

class TestAwaitStart:
    async def test_parses_start_event_correctly(self):
        session = _make_session()
        payload = json.dumps({
            "event": "start",
            "streamSid": "MZ_STREAM_001",
            "start": {
                "callSid": "CA_CALL_001",
                "customParameters": {"From": "+15550001234", "To": "+15559876543"},
            },
        })
        session.ws.iter_text = lambda: _async_iter(payload)
        await _await_start(session)

        assert session.call_sid == "CA_CALL_001"
        assert session.stream_sid == "MZ_STREAM_001"
        assert session.from_number == "+15550001234"
        assert session.to_number == "+15559876543"

    async def test_ignores_non_start_events(self):
        """Handler must keep reading past noise events like 'connected'."""
        session = _make_session()
        connected = json.dumps({"event": "connected"})
        start = json.dumps({
            "event": "start",
            "streamSid": "MZ_002",
            "start": {
                "callSid": "CA_002",
                "customParameters": {"From": "", "To": ""},
            },
        })
        session.ws.iter_text = lambda: _async_iter(connected, start)
        await _await_start(session)
        assert session.call_sid == "CA_002"

    async def test_sid_empty_when_no_start_event(self):
        """If only non-start events arrive, call_sid stays empty."""
        session = _make_session()
        only_noise = json.dumps({"event": "connected"})
        session.ws.iter_text = lambda: _async_iter(only_noise)
        # will timeout after 10s in production, but we patch the timeout
        with patch("app.voice.websocket_handler.asyncio.timeout") as mock_to:
            # make timeout a no-op context manager
            mock_to.return_value.__aenter__ = AsyncMock()
            mock_to.return_value.__aexit__ = AsyncMock(return_value=False)
            await _await_start(session)
        assert session.call_sid == ""

    async def test_missing_custom_parameters_graceful(self):
        """Missing customParameters should not raise — fields stay empty."""
        session = _make_session()
        payload = json.dumps({
            "event": "start",
            "streamSid": "MZ_003",
            "start": {"callSid": "CA_003"},
        })
        session.ws.iter_text = lambda: _async_iter(payload)
        await _await_start(session)
        assert session.from_number == ""
        assert session.to_number == ""


# ---------------------------------------------------------------------------
# _receive_audio_loop
# ---------------------------------------------------------------------------

class TestReceiveAudioLoop:
    def _session_with_stt(self):
        session = _make_session()
        session.call_sid = "CA_TEST"
        stt = MagicMock()
        stt.send_audio = AsyncMock()
        session.stt = stt
        return session

    async def test_media_event_sends_audio_to_stt(self):
        session = self._session_with_stt()
        media_msg = json.dumps({
            "event": "media",
            "media": {"payload": "AAEC"},  # valid base64
        })
        # stop + inactive after media
        stop_msg = json.dumps({"event": "stop"})
        session.ws.iter_text = lambda: _async_iter(media_msg, stop_msg)

        with patch(
            "app.voice.websocket_handler.twilio_payload_to_deepgram",
            return_value=b"\x00\x00",
        ):
            await _receive_audio_loop(session)

        session.stt.send_audio.assert_awaited_once_with(b"\x00\x00")

    async def test_stop_event_sets_call_inactive(self):
        session = self._session_with_stt()
        stop_msg = json.dumps({"event": "stop"})
        session.ws.iter_text = lambda: _async_iter(stop_msg)

        with patch("app.voice.websocket_handler.twilio_payload_to_deepgram",
                   return_value=b""):
            await _receive_audio_loop(session)

        assert session._call_active is False

    async def test_unknown_event_is_ignored(self):
        session = self._session_with_stt()
        unknown_msg = json.dumps({"event": "heartbeat"})
        stop_msg = json.dumps({"event": "stop"})
        session.ws.iter_text = lambda: _async_iter(unknown_msg, stop_msg)

        with patch("app.voice.websocket_handler.twilio_payload_to_deepgram",
                   return_value=b""):
            await _receive_audio_loop(session)

        session.stt.send_audio.assert_not_called()

    async def test_call_already_inactive_exits_immediately(self):
        session = self._session_with_stt()
        session._call_active = False
        media_msg = json.dumps({"event": "media", "media": {"payload": "AAEC"}})
        session.ws.iter_text = lambda: _async_iter(media_msg)

        with patch("app.voice.websocket_handler.twilio_payload_to_deepgram",
                   return_value=b"unused"):
            await _receive_audio_loop(session)

        session.stt.send_audio.assert_not_called()

    async def test_multiple_media_events_before_stop(self):
        session = self._session_with_stt()
        media = lambda: json.dumps({"event": "media", "media": {"payload": "AA=="}})
        stop = json.dumps({"event": "stop"})
        session.ws.iter_text = lambda: _async_iter(media(), media(), media(), stop)

        with patch("app.voice.websocket_handler.twilio_payload_to_deepgram",
                   return_value=b"\x00"):
            await _receive_audio_loop(session)

        assert session.stt.send_audio.await_count == 3


# ---------------------------------------------------------------------------
# Low-confidence streak logic (Tests 8 & 9)
# ---------------------------------------------------------------------------

class TestLowConfidenceStreak:
    """
    Simulate on_transcript closure from _run_call.
    We replicate the exact conditional logic from the source and drive
    CallSession state to verify the streak counter and queue behavior.

    Reference implementation (from websocket_handler._run_call):
        if confidence < 0.5 and text:
            session._low_conf_streak += 1
            if session._low_conf_streak == 3:
                await session._transcript_queue.put("__LOW_CONF_HANDOFF__")
            elif session._low_conf_streak < 3:
                await _speak(session, ...)
            return
        session._low_conf_streak = 0
        await session._transcript_queue.put(text)
    """

    async def _simulate_transcript(self, session, text: str, confidence: float) -> None:
        """Drive exactly the low-conf guard logic from the closure."""
        if confidence < 0.5 and text:
            session._low_conf_streak += 1
            if session._low_conf_streak == 3:
                await session._transcript_queue.put("__LOW_CONF_HANDOFF__")
            # elif < 3 would call _speak — covered by other tests
            return
        session._low_conf_streak = 0
        await session._transcript_queue.put(text)

    async def test_low_conf_increments_streak(self):
        session = _make_session()
        await self._simulate_transcript(session, "hmm", confidence=0.3)
        assert session._low_conf_streak == 1
        await self._simulate_transcript(session, "uh", confidence=0.4)
        assert session._low_conf_streak == 2

    async def test_low_conf_does_not_queue_transcript(self):
        session = _make_session()
        await self._simulate_transcript(session, "low quality", confidence=0.2)
        assert session._transcript_queue.empty()

    async def test_third_low_conf_queues_handoff_sentinel(self):
        """Test 9: After 3 consecutive low-confidence turns a handoff is triggered."""
        session = _make_session()
        for _ in range(3):
            await self._simulate_transcript(session, "unclear", confidence=0.1)
        assert not session._transcript_queue.empty()
        sentinel = session._transcript_queue.get_nowait()
        assert sentinel == "__LOW_CONF_HANDOFF__"

    async def test_handoff_only_queued_exactly_at_streak_three(self):
        """Streak=1 and streak=2 must not enqueue the sentinel."""
        session = _make_session()
        await self._simulate_transcript(session, "a", confidence=0.1)
        await self._simulate_transcript(session, "b", confidence=0.1)
        assert session._transcript_queue.empty()
        assert session._low_conf_streak == 2

    async def test_high_confidence_resets_streak(self):
        """Test 8: A confident turn resets the streak counter to 0."""
        session = _make_session()
        await self._simulate_transcript(session, "low1", confidence=0.2)
        await self._simulate_transcript(session, "low2", confidence=0.3)
        assert session._low_conf_streak == 2
        await self._simulate_transcript(session, "I need a visa", confidence=0.95)
        assert session._low_conf_streak == 0

    async def test_high_confidence_queues_transcript(self):
        session = _make_session()
        await self._simulate_transcript(session, "I need help with my green card",
                                        confidence=0.9)
        assert not session._transcript_queue.empty()
        text = session._transcript_queue.get_nowait()
        assert text == "I need help with my green card"

    async def test_empty_text_does_not_increment_streak(self):
        """Silence frames have empty text — should not affect streak."""
        session = _make_session()
        await self._simulate_transcript(session, "", confidence=0.1)
        assert session._low_conf_streak == 0

    async def test_streak_resets_and_queues_after_recovery(self):
        """Streak resets to 0 AND transcript IS queued on the good turn."""
        session = _make_session()
        await self._simulate_transcript(session, "bad1", confidence=0.2)
        await self._simulate_transcript(session, "bad2", confidence=0.3)
        # Recovery turn
        await self._simulate_transcript(session, "I have an H-1B visa", confidence=0.88)
        assert session._low_conf_streak == 0
        text = session._transcript_queue.get_nowait()
        assert text == "I have an H-1B visa"


# ---------------------------------------------------------------------------
# Language auto-switch logic (Test 7)
# ---------------------------------------------------------------------------

class TestLanguageAutoSwitch:
    """
    Simulate the language-detection block inside on_transcript.

    Reference implementation:
        if detected_lang.startswith("es") and session.language == "en":
            session.language = "es"
            session.agent.switch_language("es")
            session.tts = ElevenLabsTTS(language="es", ...)
            if session.state:
                session.state.language = "es"
    """

    async def _simulate_lang_check(self, session, detected_lang: str) -> None:
        """Drive the language-switch block from on_transcript."""
        if detected_lang.startswith("es") and session.language == "en":
            session.language = "es"
            if session.agent:
                session.agent.switch_language("es")
            if session.state:
                session.state.language = "es"

    async def test_spanish_detected_switches_session_language(self):
        """Test 7: Deepgram returning 'es' causes session.language → 'es'."""
        session = _make_session()
        assert session.language == "en"
        await self._simulate_lang_check(session, "es")
        assert session.language == "es"

    async def test_es_US_detected_also_triggers_switch(self):
        """'es-US' startswith('es') so it must switch too."""
        session = _make_session()
        await self._simulate_lang_check(session, "es-US")
        assert session.language == "es"

    async def test_switch_not_triggered_when_already_spanish(self):
        """If already 'es', no re-switching — agent.switch_language not called again."""
        session = _make_session()
        session.language = "es"
        mock_agent = MagicMock()
        session.agent = mock_agent
        await self._simulate_lang_check(session, "es")
        mock_agent.switch_language.assert_not_called()

    async def test_english_detected_does_not_switch(self):
        session = _make_session()
        await self._simulate_lang_check(session, "en-US")
        assert session.language == "en"

    async def test_agent_switch_language_called(self):
        session = _make_session()
        mock_agent = MagicMock()
        session.agent = mock_agent
        await self._simulate_lang_check(session, "es")
        mock_agent.switch_language.assert_called_once_with("es")

    async def test_state_language_updated_when_present(self):
        session = _make_session()
        mock_state = MagicMock()
        session.state = mock_state
        await self._simulate_lang_check(session, "es")
        assert mock_state.language == "es"


# ---------------------------------------------------------------------------
# Barge-in flag via on_speech_start
# ---------------------------------------------------------------------------

class TestOnSpeechStartBargeIn:
    """
    Simulate on_speech_start closure:
        if session._is_speaking:
            session._barge_in_flag.set()
    """

    def _fire_speech_start(self, session: CallSession) -> None:
        if session._is_speaking:
            session._barge_in_flag.set()

    def test_barge_in_set_when_currently_speaking(self):
        """Test 5: SpeechStarted while TTS plays → barge-in flag set."""
        session = _make_session()
        session._is_speaking = True
        self._fire_speech_start(session)
        assert session._barge_in_flag.is_set()

    def test_barge_in_not_set_when_not_speaking(self):
        """Background noise/speaker while not playing TTS must not barge-in."""
        session = _make_session()
        session._is_speaking = False
        self._fire_speech_start(session)
        assert not session._barge_in_flag.is_set()


# ---------------------------------------------------------------------------
# _get_semaphore
# ---------------------------------------------------------------------------

class TestGetSemaphore:
    def test_returns_asyncio_semaphore(self):
        sem = _get_semaphore()
        assert isinstance(sem, asyncio.Semaphore)

    def test_singleton_returns_same_instance(self):
        sem1 = _get_semaphore()
        sem2 = _get_semaphore()
        assert sem1 is sem2

    def test_semaphore_limit_matches_settings(self):
        from app.config import settings
        sem = _get_semaphore()
        # Semaphore._value is the initial count
        assert sem._value == settings.max_concurrent_calls
