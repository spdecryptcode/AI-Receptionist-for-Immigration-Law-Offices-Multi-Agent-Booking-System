"""
Unit tests for telephony modules:
  - app/telephony/call_transfer.py  (cold/warm transfer, TwiML helpers)
  - app/telephony/voicemail.py      (download, transcribe, summarise, emergency)
  - app/telephony/outbound_callback.py (enqueue, pop, promote)
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
os.environ.setdefault("BASE_URL", "https://test.example.com")
os.environ.setdefault("GHL_API_KEY", "ghl-test")
os.environ.setdefault("GHL_LOCATION_ID", "loc-test")
os.environ.setdefault("ELEVENLABS_VOICE_ID_EN", "voice-en-test")
os.environ.setdefault("ELEVENLABS_VOICE_ID_ES", "voice-es-test")
os.environ.setdefault("GHL_CALENDAR_ID", "cal-test")
os.environ.setdefault("GOOGLE_CALENDAR_ID", "gcal-test")

import json
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.telephony.call_transfer import (
    _update_call_twiml,
    build_attorney_whisper,
    cold_transfer,
    twiml_transfer_no_answer,
    warm_transfer,
)
from app.telephony.voicemail import (
    _download_recording,
    _is_emergency,
    _next_business_day_iso,
    _summarise,
    _transcribe,
)
from app.telephony.outbound_callback import (
    _MAX_RETRIES,
    _QUEUE_KEY,
    _pop_item,
    enqueue_callback,
    promote_delayed_callbacks,
)


# ===========================================================================
# call_transfer — build_attorney_whisper
# ===========================================================================

class TestBuildAttorneyWhisper:
    _intake = {
        "full_name": "Maria Garcia",
        "case_type": "asylum",
        "current_immigration_status": "pending",
    }

    def test_urgent_prefix_when_score_gte_6(self):
        result = build_attorney_whisper(self._intake, urgency_score=7)
        assert result.startswith("URGENT — ")

    def test_urgent_prefix_at_boundary_score_6(self):
        result = build_attorney_whisper(self._intake, urgency_score=6)
        assert result.startswith("URGENT — ")

    def test_no_urgent_prefix_when_score_lt_6(self):
        result = build_attorney_whisper(self._intake, urgency_score=5)
        assert not result.startswith("URGENT")

    def test_contains_name_case_status_score(self):
        result = build_attorney_whisper(self._intake, urgency_score=3)
        assert "Maria Garcia" in result
        assert "asylum" in result
        assert "pending" in result
        assert "3" in result

    def test_ends_with_connect_instruction(self):
        result = build_attorney_whisper(self._intake, urgency_score=4)
        assert "connect the caller" in result.lower()

    def test_fallback_name_when_missing(self):
        result = build_attorney_whisper({}, urgency_score=3)
        assert "the caller" in result


# ===========================================================================
# call_transfer — twiml_transfer_no_answer
# ===========================================================================

class TestTwimlTransferNoAnswer:
    def test_english_response_contains_unavailable(self):
        xml = twiml_transfer_no_answer(language="en")
        assert "unavailable" in xml.lower()

    def test_english_includes_record_verb(self):
        xml = twiml_transfer_no_answer(language="en")
        assert "<Record" in xml

    def test_spanish_response_contains_abogado(self):
        xml = twiml_transfer_no_answer(language="es")
        assert "abogado" in xml

    def test_response_is_valid_xml_opener(self):
        xml = twiml_transfer_no_answer()
        assert xml.strip().startswith("<?xml") or xml.strip().startswith("<Response")

    def test_spanish_includes_record_verb(self):
        xml = twiml_transfer_no_answer(language="es")
        assert "<Record" in xml


# ===========================================================================
# call_transfer — _update_call_twiml
# ===========================================================================

class TestUpdateCallTwiml:
    async def test_returns_true_on_success(self):
        with patch("app.telephony.call_transfer._update_call_sync") as mock_sync:
            result = await _update_call_twiml("CA123", "<Response/>")
        assert result is True
        mock_sync.assert_called_once_with("CA123", "<Response/>")

    async def test_returns_false_on_exception(self):
        with patch(
            "app.telephony.call_transfer._update_call_sync",
            side_effect=Exception("twilio error"),
        ):
            result = await _update_call_twiml("CA123", "<Response/>")
        assert result is False


# ===========================================================================
# call_transfer — cold_transfer
# ===========================================================================

class TestColdTransfer:
    async def test_returns_true_on_success(self):
        with patch(
            "app.telephony.call_transfer._update_call_twiml",
            new_callable=AsyncMock,
            return_value=True,
        ):
            result = await cold_transfer("CA123", "+15551234567")
        assert result is True

    async def test_twiml_contains_target_number(self):
        captured = {}

        async def capture(call_sid, twiml, label=""):
            captured["twiml"] = twiml
            return True

        with patch("app.telephony.call_transfer._update_call_twiml", side_effect=capture):
            await cold_transfer("CA123", "+15559999999")

        assert "+15559999999" in captured["twiml"]

    async def test_default_fallback_url_contains_transfer_fallback(self):
        captured = {}

        async def capture(call_sid, twiml, label=""):
            captured["twiml"] = twiml
            return True

        with patch("app.telephony.call_transfer._update_call_twiml", side_effect=capture):
            await cold_transfer("CA123", "+15551234567")

        assert "transfer-fallback" in captured["twiml"]

    async def test_returns_false_when_update_fails(self):
        with patch(
            "app.telephony.call_transfer._update_call_twiml",
            new_callable=AsyncMock,
            return_value=False,
        ):
            result = await cold_transfer("CA123", "+15551234567")
        assert result is False

    async def test_custom_fallback_url_used(self):
        captured = {}

        async def capture(call_sid, twiml, label=""):
            captured["twiml"] = twiml
            return True

        with patch("app.telephony.call_transfer._update_call_twiml", side_effect=capture):
            await cold_transfer("CA123", "+15551234567", fallback_action_url="https://custom.example.com/fallback")

        assert "custom.example.com" in captured["twiml"]


# ===========================================================================
# call_transfer — warm_transfer
# ===========================================================================

class TestWarmTransfer:
    async def test_returns_true_on_success(self):
        with (
            patch(
                "app.telephony.call_transfer._update_call_twiml",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch("asyncio.create_task"),
        ):
            result = await warm_transfer(
                "CA123", "+15551234567", "whisper text", "conf-room-1"
            )
        assert result is True

    async def test_returns_false_when_update_fails(self):
        with patch(
            "app.telephony.call_transfer._update_call_twiml",
            new_callable=AsyncMock,
            return_value=False,
        ):
            result = await warm_transfer(
                "CA123", "+15551234567", "whisper", "conf-1"
            )
        assert result is False

    async def test_background_task_created_on_success(self):
        with (
            patch(
                "app.telephony.call_transfer._update_call_twiml",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch("asyncio.create_task") as mock_create_task,
        ):
            await warm_transfer("CA123", "+15551234567", "whisper", "conf-1")
        mock_create_task.assert_called_once()

    async def test_twiml_contains_conference_name(self):
        captured = {}

        async def capture(call_sid, twiml, label=""):
            captured["twiml"] = twiml
            return True

        with (
            patch("app.telephony.call_transfer._update_call_twiml", side_effect=capture),
            patch("asyncio.create_task"),
        ):
            await warm_transfer("CA123", "+15551234567", "whisper", "my-conf-room")

        assert "my-conf-room" in captured["twiml"]

    async def test_spanish_hold_message(self):
        captured = {}

        async def capture(call_sid, twiml, label=""):
            captured["twiml"] = twiml
            return True

        with (
            patch("app.telephony.call_transfer._update_call_twiml", side_effect=capture),
            patch("asyncio.create_task"),
        ):
            await warm_transfer("CA123", "+15551234567", "whisper", "conf-1", language="es")

        assert "abogado" in captured["twiml"].lower() or "espere" in captured["twiml"].lower()


# ===========================================================================
# voicemail — _is_emergency
# ===========================================================================

class TestIsEmergency:
    def test_detained_triggers_emergency(self):
        assert _is_emergency("I was detained by officers this morning") is True

    def test_ice_triggers_emergency(self):
        assert _is_emergency("ICE came to our house") is True

    def test_deportation_triggers_emergency(self):
        assert _is_emergency("facing deportation tomorrow") is True

    def test_emergencia_spanish_triggers(self):
        assert _is_emergency("es una emergencia") is True

    def test_detenido_spanish_triggers(self):
        assert _is_emergency("estoy detenido") is True

    def test_normal_message_does_not_trigger(self):
        assert _is_emergency("I would like to schedule a consultation") is False

    def test_empty_string_does_not_trigger(self):
        assert _is_emergency("") is False

    def test_case_insensitive_matching(self):
        assert _is_emergency("DETAINED AND URGENT SITUATION") is True

    def test_urgent_triggers_emergency(self):
        assert _is_emergency("this is urgent please help") is True


# ===========================================================================
# voicemail — _next_business_day_iso
# ===========================================================================

class TestNextBusinessDayIso:
    def test_returns_string(self):
        result = _next_business_day_iso()
        assert isinstance(result, str)

    def test_never_returns_saturday(self):
        result = _next_business_day_iso()
        d = date.fromisoformat(result)
        assert d.weekday() != 5  # Saturday

    def test_never_returns_sunday(self):
        result = _next_business_day_iso()
        d = date.fromisoformat(result)
        assert d.weekday() != 6  # Sunday

    def test_is_in_the_future(self):
        result = _next_business_day_iso()
        d = date.fromisoformat(result)
        assert d > date.today()


# ===========================================================================
# voicemail — _summarise
# ===========================================================================

class TestSummarise:
    async def test_unavailable_transcript_returns_no_transcription(self):
        result = await _summarise("[Transcription unavailable]", "en")
        assert result == "No transcription available."

    async def test_empty_transcript_returns_no_transcription(self):
        result = await _summarise("", "en")
        assert result == "No transcription available."

    async def test_gpt_exception_returns_truncated_transcript(self):
        long_transcript = "A" * 500
        with patch("openai.AsyncOpenAI", side_effect=Exception("api error")):
            result = await _summarise(long_transcript, "en")
        assert result == long_transcript[:300]

    async def test_successful_gpt_summary(self):
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock(message=MagicMock(content="  Summary text.  "))]
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_resp)

        with patch("openai.AsyncOpenAI", return_value=mock_client):
            result = await _summarise("I was calling about my visa renewal", "en")

        assert result == "Summary text."

    async def test_spanish_language_flag(self):
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock(message=MagicMock(content="Resumen."))]
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_resp)

        with patch("openai.AsyncOpenAI", return_value=mock_client) as MockCls:
            await _summarise("Llamo por mi visa", "es")

        # Verify AsyncOpenAI was instantiated (API key passed)
        MockCls.assert_called_once()


# ===========================================================================
# voicemail — _download_recording
# ===========================================================================

class TestDownloadRecording:
    def _make_httpx_mock(self, content: bytes):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.content = content

        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)

        mock_cls = MagicMock(return_value=mock_client)
        return mock_cls

    async def test_returns_bytes_on_success(self):
        mock_cls = self._make_httpx_mock(b"audio data")
        with patch("httpx.AsyncClient", mock_cls):
            result = await _download_recording("https://api.twilio.com/recordings/RE123")
        assert result == b"audio data"

    async def test_appends_mp3_if_missing(self):
        mock_cls = self._make_httpx_mock(b"data")
        url_called = {}

        async def capture_get(url, **kwargs):
            url_called["url"] = url
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.content = b"data"
            return resp

        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(side_effect=capture_get)
        mock_cls = MagicMock(return_value=mock_client)

        with patch("httpx.AsyncClient", mock_cls):
            await _download_recording("https://api.twilio.com/recordings/RE123")

        assert url_called["url"].endswith(".mp3")

    async def test_does_not_double_mp3_suffix(self):
        url_called = {}

        async def capture_get(url, **kwargs):
            url_called["url"] = url
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.content = b"data"
            return resp

        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(side_effect=capture_get)
        mock_cls = MagicMock(return_value=mock_client)

        with patch("httpx.AsyncClient", mock_cls):
            await _download_recording("https://api.twilio.com/recordings/RE123.mp3")

        assert url_called["url"].count(".mp3") == 1

    async def test_returns_none_on_http_error(self):
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(side_effect=Exception("connection refused"))
        mock_cls = MagicMock(return_value=mock_client)

        with patch("httpx.AsyncClient", mock_cls):
            result = await _download_recording("https://api.twilio.com/recordings/RE123")

        assert result is None


# ===========================================================================
# voicemail — _transcribe
# ===========================================================================

class TestTranscribe:
    def _mock_dg_response(self, transcript: str):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = MagicMock(
            return_value={
                "results": {
                    "channels": [
                        {"alternatives": [{"transcript": transcript}]}
                    ]
                }
            }
        )
        return mock_resp

    async def test_extracts_transcript_from_deepgram_response(self):
        mock_resp = self._mock_dg_response("hello world")
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_cls = MagicMock(return_value=mock_client)

        with patch("httpx.AsyncClient", mock_cls):
            result = await _transcribe(b"audio data", "en")

        assert result == "hello world"

    async def test_returns_empty_string_on_exception(self):
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(side_effect=Exception("deepgram error"))
        mock_cls = MagicMock(return_value=mock_client)

        with patch("httpx.AsyncClient", mock_cls):
            result = await _transcribe(b"audio", "en")

        assert result == ""

    async def test_empty_channels_returns_empty_string(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = MagicMock(return_value={"results": {"channels": []}})
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_cls = MagicMock(return_value=mock_client)

        with patch("httpx.AsyncClient", mock_cls):
            result = await _transcribe(b"audio", "en")

        assert result == ""


# ===========================================================================
# outbound_callback — enqueue_callback
# ===========================================================================

class TestEnqueueCallback:
    async def test_calls_lpush_with_queue_key(self):
        mock_redis = AsyncMock()
        await enqueue_callback(mock_redis, "+15551234567")
        mock_redis.lpush.assert_called_once()
        args = mock_redis.lpush.call_args[0]
        assert args[0] == _QUEUE_KEY

    async def test_payload_has_correct_caller_number(self):
        mock_redis = AsyncMock()
        await enqueue_callback(mock_redis, "+15551234567", language="en")
        payload_str = mock_redis.lpush.call_args[0][1]
        payload = json.loads(payload_str)
        assert payload["caller_number"] == "+15551234567"

    async def test_payload_retries_default_to_zero(self):
        mock_redis = AsyncMock()
        await enqueue_callback(mock_redis, "+15551234567")
        payload_str = mock_redis.lpush.call_args[0][1]
        payload = json.loads(payload_str)
        assert payload["retries"] == 0

    async def test_payload_includes_language(self):
        mock_redis = AsyncMock()
        await enqueue_callback(mock_redis, "+15551234567", language="es")
        payload_str = mock_redis.lpush.call_args[0][1]
        payload = json.loads(payload_str)
        assert payload["language"] == "es"

    async def test_payload_includes_requested_at(self):
        mock_redis = AsyncMock()
        await enqueue_callback(mock_redis, "+15551234567")
        payload_str = mock_redis.lpush.call_args[0][1]
        payload = json.loads(payload_str)
        assert "requested_at" in payload


# ===========================================================================
# outbound_callback — _pop_item
# ===========================================================================

class TestPopItem:
    async def test_returns_none_on_timeout(self):
        mock_redis = AsyncMock()
        mock_redis.brpop = AsyncMock(return_value=None)
        result = await _pop_item(mock_redis)
        assert result is None

    async def test_returns_parsed_dict_on_valid_json(self):
        payload = json.dumps({"caller_number": "+15551234567", "retries": 0})
        mock_redis = AsyncMock()
        mock_redis.brpop = AsyncMock(return_value=(_QUEUE_KEY, payload))
        result = await _pop_item(mock_redis)
        assert result is not None
        assert result["caller_number"] == "+15551234567"

    async def test_returns_none_on_invalid_json(self):
        mock_redis = AsyncMock()
        mock_redis.brpop = AsyncMock(return_value=(_QUEUE_KEY, "not-valid-json"))
        result = await _pop_item(mock_redis)
        assert result is None


# ===========================================================================
# outbound_callback — promote_delayed_callbacks
# ===========================================================================

class TestPromoteDelayedCallbacks:
    async def test_returns_zero_when_no_ready_items(self):
        mock_redis = AsyncMock()
        mock_redis.zrangebyscore = AsyncMock(return_value=[])
        result = await promote_delayed_callbacks(mock_redis)
        assert result == 0

    async def test_returns_count_of_promoted_items(self):
        item1 = json.dumps({"caller_number": "+15551111111", "retries": 0})
        item2 = json.dumps({"caller_number": "+15552222222", "retries": 1})

        mock_pipe = MagicMock()
        mock_pipe.execute = AsyncMock(return_value=[1, 1, 1, 1])
        mock_pipe.lpush = MagicMock()
        mock_pipe.zrem = MagicMock()

        mock_redis = AsyncMock()
        mock_redis.zrangebyscore = AsyncMock(return_value=[item1, item2])
        mock_redis.pipeline = MagicMock(return_value=mock_pipe)

        result = await promote_delayed_callbacks(mock_redis)
        assert result == 2

    async def test_pipeline_called_for_each_item(self):
        item = json.dumps({"caller_number": "+15551111111", "retries": 0})

        mock_pipe = MagicMock()
        mock_pipe.execute = AsyncMock(return_value=[1, 1])
        mock_pipe.lpush = MagicMock()
        mock_pipe.zrem = MagicMock()

        mock_redis = AsyncMock()
        mock_redis.zrangebyscore = AsyncMock(return_value=[item])
        mock_redis.pipeline = MagicMock(return_value=mock_pipe)

        await promote_delayed_callbacks(mock_redis)

        mock_pipe.lpush.assert_called_once_with(_QUEUE_KEY, item)
        mock_pipe.zrem.assert_called_once_with("callback_delayed", item)
        mock_pipe.execute.assert_called_once()
