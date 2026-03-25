"""
Unit tests for app/logging_analytics/structured_data.py
            and app/logging_analytics/structured_logger.py.

structured_data:
  to_ghl_custom_fields:
    - maps known intake keys to GHL key names
    - booleans converted to "Yes" / "No"
    - None values skipped
    - unknown keys not in FIELD_MAP skipped
    - all values stringified

  _merge_intake:
    - GPT output used as base
    - _PREFER_BASE keys (full_name, phone_number, email, appointment_booked,
      appointment_datetime) kept from base when present
    - fields present in base but not extracted are filled from base
    - _PREFER_BASE field overridden when base value is falsy (empty/None)

  extract_structured_intake:
    - successful OpenAI call → parsed tool-call JSON merged with existing_intake
    - OpenAI exception → returns existing_intake unchanged
    - empty conversation → still calls OpenAI (no short-circuit)
    - no tool_calls in response → returns existing_intake

structured_logger:
  JSONFormatter:
    - output is valid JSON
    - contains ts, level, logger, msg keys
    - level mapped correctly (INFO→"info", ERROR→"error", etc.)
    - extra fields (call_sid, phase, latency_ms, event, lang) included when set
    - exc_info present → error dict with type/message/traceback
    - no exc_info → no error key

  configure_logging:
    - sets root logger level
    - adds a StreamHandler to root logger
    - clears existing handlers before adding

  log_event:
    - pushes JSON to "analytics_events" Redis list
    - payload contains ts, event, call_sid, phase, latency_ms fields
    - optional kwargs merged into payload
    - Redis exception silently swallowed (no raise)

  TimedOperation:
    - __aenter__ records start time
    - __aexit__ calls log_event with latency_ms >= 0
    - passes event_type and kwargs to log_event
    - does not suppress exceptions from the body
"""
from __future__ import annotations

import json
import logging
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

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.logging_analytics.structured_data import (
    _merge_intake,
    extract_structured_intake,
    to_ghl_custom_fields,
)
from app.logging_analytics.structured_logger import (
    JSONFormatter,
    TimedOperation,
    configure_logging,
    log_event,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CALL_SID = "CA0000001"
_CONVO = [{"role": "user", "content": "I need a visa."}]


def _make_openai_response(extracted: dict):
    tc = MagicMock()
    tc.function.arguments = json.dumps(extracted)
    resp = MagicMock()
    resp.choices[0].message.tool_calls = [tc]
    return resp


# ---------------------------------------------------------------------------
# TestToGhlCustomFields
# ---------------------------------------------------------------------------

class TestToGhlCustomFields:
    def test_known_key_mapped(self):
        intake = {"case_type": "asylum"}
        fields = to_ghl_custom_fields(intake)
        keys = [f["key"] for f in fields]
        assert "case_type" in keys

    def test_boolean_true_converted_to_yes(self):
        intake = {"has_prior_attorney": True}
        fields = to_ghl_custom_fields(intake)
        match = next(f for f in fields if f["key"] == "has_prior_attorney")
        assert match["field_value"] == "Yes"

    def test_boolean_false_converted_to_no(self):
        intake = {"has_criminal_history": False}
        fields = to_ghl_custom_fields(intake)
        match = next(f for f in fields if f["key"] == "has_criminal_history")
        assert match["field_value"] == "No"

    def test_none_value_skipped(self):
        intake = {"case_type": None, "country_of_origin": "Mexico"}
        fields = to_ghl_custom_fields(intake)
        keys = [f["key"] for f in fields]
        assert "case_type" not in keys
        assert "country_of_origin" in keys

    def test_unknown_key_not_included(self):
        intake = {"unknown_field_xyz": "value"}
        fields = to_ghl_custom_fields(intake)
        assert fields == []

    def test_numeric_value_stringified(self):
        intake = {"time_in_us_years": 5}
        fields = to_ghl_custom_fields(intake)
        match = next(f for f in fields if f["key"] == "time_in_us")
        assert match["field_value"] == "5"

    def test_all_values_are_strings(self):
        intake = {
            "case_type": "asylum",
            "has_upcoming_hearing": True,
            "time_in_us_years": 3,
        }
        fields = to_ghl_custom_fields(intake)
        for f in fields:
            assert isinstance(f["field_value"], str)

    def test_empty_intake_returns_empty_list(self):
        assert to_ghl_custom_fields({}) == []

    def test_immigration_emergency_mapped(self):
        intake = {"immigration_emergency": True}
        fields = to_ghl_custom_fields(intake)
        match = next((f for f in fields if f["key"] == "immigration_emergency"), None)
        assert match is not None
        assert match["field_value"] == "Yes"

    def test_consultation_type_mapped(self):
        intake = {"consultation_type": "video"}
        fields = to_ghl_custom_fields(intake)
        match = next((f for f in fields if f["key"] == "preferred_consultation_type"), None)
        assert match is not None
        assert match["field_value"] == "video"


# ---------------------------------------------------------------------------
# TestMergeIntake
# ---------------------------------------------------------------------------

class TestMergeIntake:
    def test_gpt_output_used_as_base(self):
        base = {}
        extracted = {"case_type": "asylum", "country_of_origin": "Honduras"}
        merged = _merge_intake(base, extracted)
        assert merged["case_type"] == "asylum"

    def test_prefer_base_full_name_preserved(self):
        base = {"full_name": "Maria Lopez"}
        extracted = {"full_name": "M. Lopez", "case_type": "family-based"}
        merged = _merge_intake(base, extracted)
        assert merged["full_name"] == "Maria Lopez"

    def test_prefer_base_phone_preserved(self):
        base = {"phone_number": "+15551234567"}
        extracted = {"phone_number": "555-1234"}
        merged = _merge_intake(base, extracted)
        assert merged["phone_number"] == "+15551234567"

    def test_prefer_base_email_preserved(self):
        base = {"email": "real@example.com"}
        extracted = {"email": "guessed@example.com"}
        merged = _merge_intake(base, extracted)
        assert merged["email"] == "real@example.com"

    def test_prefer_base_appointment_booked_preserved(self):
        base = {"appointment_booked": True}
        extracted = {"appointment_booked": False}
        merged = _merge_intake(base, extracted)
        assert merged["appointment_booked"] is True

    def test_falsy_base_prefer_field_overridden_by_gpt(self):
        """Empty base value for _PREFER_BASE field lets GPT value through."""
        base = {"full_name": ""}
        extracted = {"full_name": "Jane Doe"}
        merged = _merge_intake(base, extracted)
        assert merged["full_name"] == "Jane Doe"

    def test_base_fills_gaps_not_in_extracted(self):
        base = {"urgency_reason": "ICE hearing tomorrow"}
        extracted = {"case_type": "removal defense"}
        merged = _merge_intake(base, extracted)
        assert merged["urgency_reason"] == "ICE hearing tomorrow"
        assert merged["case_type"] == "removal defense"

    def test_both_empty_returns_empty(self):
        merged = _merge_intake({}, {})
        assert merged == {}

    def test_extracted_new_field_included(self):
        """GPT-extracted field NOT in base should appear in merged."""
        base = {}
        extracted = {"has_prior_deportation": True}
        merged = _merge_intake(base, extracted)
        assert merged["has_prior_deportation"] is True


# ---------------------------------------------------------------------------
# TestExtractStructuredIntake
# ---------------------------------------------------------------------------

class TestExtractStructuredIntake:
    async def test_successful_extraction_merged_with_existing(self):
        existing = {"phone_number": "+15559876543"}
        extracted = {"case_type": "asylum", "phone_number": "wrong"}
        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(
            return_value=_make_openai_response(extracted)
        )
        with patch("openai.AsyncOpenAI", return_value=mock_client):
            result = await extract_structured_intake(_CALL_SID, _CONVO, existing)
        assert result["case_type"] == "asylum"
        # phone preserved from existing (prefer_base)
        assert result["phone_number"] == "+15559876543"

    async def test_openai_exception_returns_existing(self):
        existing = {"case_type": "family-based"}
        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(side_effect=Exception("timeout"))
        with patch("openai.AsyncOpenAI", return_value=mock_client):
            result = await extract_structured_intake(_CALL_SID, _CONVO, existing)
        assert result == existing

    async def test_no_tool_calls_returns_existing(self):
        existing = {"case_type": "TPS"}
        mock_resp = MagicMock()
        mock_resp.choices[0].message.tool_calls = None
        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_resp)
        with patch("openai.AsyncOpenAI", return_value=mock_client):
            result = await extract_structured_intake(_CALL_SID, _CONVO, existing)
        assert result == existing

    async def test_uses_conversation_turns(self):
        """Verify the conversation text is included in the API request."""
        captured_messages: list = []

        async def capture_create(**kwargs):
            captured_messages.extend(kwargs["messages"])
            return _make_openai_response({})

        mock_client = AsyncMock()
        mock_client.chat.completions.create = capture_create
        with patch("openai.AsyncOpenAI", return_value=mock_client):
            await extract_structured_intake(
                _CALL_SID,
                [{"role": "user", "content": "My name is Rosa."}],
                {},
            )
        user_msg = captured_messages[-1]["content"]
        assert "Rosa" in user_msg

    async def test_empty_existing_intake_accepted(self):
        extracted = {"case_type": "DACA"}
        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(
            return_value=_make_openai_response(extracted)
        )
        with patch("openai.AsyncOpenAI", return_value=mock_client):
            result = await extract_structured_intake(_CALL_SID, _CONVO, {})
        assert result["case_type"] == "DACA"


# ---------------------------------------------------------------------------
# TestJSONFormatter
# ---------------------------------------------------------------------------

class TestJSONFormatter:
    def _make_record(
        self,
        msg: str = "test message",
        level: int = logging.INFO,
        **extra,
    ) -> logging.LogRecord:
        record = logging.LogRecord(
            name="test.logger",
            level=level,
            pathname="",
            lineno=0,
            msg=msg,
            args=(),
            exc_info=None,
        )
        for k, v in extra.items():
            setattr(record, k, v)
        return record

    def test_output_is_valid_json(self):
        formatter = JSONFormatter()
        record = self._make_record("hello")
        output = formatter.format(record)
        data = json.loads(output)
        assert isinstance(data, dict)

    def test_contains_required_keys(self):
        formatter = JSONFormatter()
        data = json.loads(formatter.format(self._make_record("msg")))
        for key in ("ts", "level", "logger", "msg"):
            assert key in data

    def test_level_info_mapped(self):
        formatter = JSONFormatter()
        data = json.loads(formatter.format(self._make_record("x", level=logging.INFO)))
        assert data["level"] == "info"

    def test_level_error_mapped(self):
        formatter = JSONFormatter()
        data = json.loads(formatter.format(self._make_record("x", level=logging.ERROR)))
        assert data["level"] == "error"

    def test_level_warning_mapped(self):
        formatter = JSONFormatter()
        data = json.loads(formatter.format(self._make_record("x", level=logging.WARNING)))
        assert data["level"] == "warning"

    def test_level_debug_mapped(self):
        formatter = JSONFormatter()
        data = json.loads(formatter.format(self._make_record("x", level=logging.DEBUG)))
        assert data["level"] == "debug"

    def test_extra_call_sid_included(self):
        formatter = JSONFormatter()
        record = self._make_record("x", call_sid="CA999")
        data = json.loads(formatter.format(record))
        assert data["call_sid"] == "CA999"

    def test_extra_latency_ms_included(self):
        formatter = JSONFormatter()
        record = self._make_record("x", latency_ms=42.5)
        data = json.loads(formatter.format(record))
        assert data["latency_ms"] == 42.5

    def test_extra_fields_missing_not_in_output(self):
        formatter = JSONFormatter()
        data = json.loads(formatter.format(self._make_record("x")))
        assert "call_sid" not in data
        assert "latency_ms" not in data

    def test_exc_info_adds_error_key(self):
        formatter = JSONFormatter()
        try:
            raise ValueError("something went wrong")
        except ValueError:
            import sys
            exc_info = sys.exc_info()
        record = logging.LogRecord(
            name="t",
            level=logging.ERROR,
            pathname="",
            lineno=0,
            msg="err",
            args=(),
            exc_info=exc_info,
        )
        data = json.loads(formatter.format(record))
        assert "error" in data
        assert data["error"]["type"] == "ValueError"
        assert "something went wrong" in data["error"]["message"]

    def test_no_exc_info_no_error_key(self):
        formatter = JSONFormatter()
        data = json.loads(formatter.format(self._make_record("x")))
        assert "error" not in data

    def test_msg_content_correct(self):
        formatter = JSONFormatter()
        data = json.loads(formatter.format(self._make_record("hello world")))
        assert data["msg"] == "hello world"


# ---------------------------------------------------------------------------
# TestConfigureLogging
# ---------------------------------------------------------------------------

class TestConfigureLogging:
    def test_sets_root_level_info(self):
        configure_logging(level="INFO", json_output=False)
        assert logging.getLogger().level == logging.INFO

    def test_sets_root_level_debug(self):
        configure_logging(level="DEBUG", json_output=False)
        assert logging.getLogger().level == logging.DEBUG

    def test_adds_stream_handler(self):
        configure_logging(level="INFO", json_output=False)
        root = logging.getLogger()
        types = [type(h).__name__ for h in root.handlers]
        assert "StreamHandler" in types

    def test_json_output_uses_json_formatter(self):
        configure_logging(level="INFO", json_output=True)
        root = logging.getLogger()
        formatters = [type(h.formatter).__name__ for h in root.handlers]
        assert "JSONFormatter" in formatters

    def test_existing_handlers_cleared(self):
        """Second call must not double-add handlers."""
        configure_logging(level="INFO", json_output=False)
        count_first = len(logging.getLogger().handlers)
        configure_logging(level="INFO", json_output=False)
        count_second = len(logging.getLogger().handlers)
        assert count_second == count_first == 1


# ---------------------------------------------------------------------------
# TestLogEvent
# ---------------------------------------------------------------------------

class TestLogEvent:
    async def test_pushes_to_analytics_events_queue(self):
        mock_redis = AsyncMock()
        with patch("app.logging_analytics.structured_logger._get_redis", return_value=mock_redis):
            await log_event("test_event")
        mock_redis.rpush.assert_awaited_once()
        queue_name = mock_redis.rpush.call_args[0][0]
        assert queue_name == "analytics_events"

    async def test_payload_contains_event_field(self):
        mock_redis = AsyncMock()
        with patch("app.logging_analytics.structured_logger._get_redis", return_value=mock_redis):
            await log_event("phase_transition")
        payload = json.loads(mock_redis.rpush.call_args[0][1])
        assert payload["event"] == "phase_transition"

    async def test_payload_contains_call_sid(self):
        mock_redis = AsyncMock()
        with patch("app.logging_analytics.structured_logger._get_redis", return_value=mock_redis):
            await log_event("evt", call_sid="CA777")
        payload = json.loads(mock_redis.rpush.call_args[0][1])
        assert payload["call_sid"] == "CA777"

    async def test_payload_contains_latency_ms(self):
        mock_redis = AsyncMock()
        with patch("app.logging_analytics.structured_logger._get_redis", return_value=mock_redis):
            await log_event("tts_latency", latency_ms=123.45)
        payload = json.loads(mock_redis.rpush.call_args[0][1])
        assert payload["latency_ms"] == 123.45

    async def test_payload_contains_phase(self):
        mock_redis = AsyncMock()
        with patch("app.logging_analytics.structured_logger._get_redis", return_value=mock_redis):
            await log_event("turn", phase="intake")
        payload = json.loads(mock_redis.rpush.call_args[0][1])
        assert payload["phase"] == "intake"

    async def test_extra_kwargs_merged(self):
        mock_redis = AsyncMock()
        with patch("app.logging_analytics.structured_logger._get_redis", return_value=mock_redis):
            await log_event("transcript", text="hello", confidence=0.97)
        payload = json.loads(mock_redis.rpush.call_args[0][1])
        assert payload["text"] == "hello"
        assert payload["confidence"] == 0.97

    async def test_redis_exception_silently_swallowed(self):
        mock_redis = AsyncMock()
        mock_redis.rpush = AsyncMock(side_effect=Exception("redis down"))
        with patch("app.logging_analytics.structured_logger._get_redis", return_value=mock_redis):
            await log_event("evt")  # must not raise

    async def test_optional_fields_absent_when_not_provided(self):
        mock_redis = AsyncMock()
        with patch("app.logging_analytics.structured_logger._get_redis", return_value=mock_redis):
            await log_event("bare_event")
        payload = json.loads(mock_redis.rpush.call_args[0][1])
        assert "call_sid" not in payload
        assert "phase" not in payload
        assert "latency_ms" not in payload


# ---------------------------------------------------------------------------
# TestTimedOperation
# ---------------------------------------------------------------------------

class TestTimedOperation:
    async def test_logs_event_with_latency(self):
        events: list[dict] = []

        async def fake_log_event(event_type, **kwargs):
            events.append({"event": event_type, **kwargs})

        with patch("app.logging_analytics.structured_logger.log_event", fake_log_event):
            async with TimedOperation("deepgram_connect"):
                pass

        assert len(events) == 1
        assert events[0]["event"] == "deepgram_connect"
        assert events[0]["latency_ms"] >= 0

    async def test_passes_kwargs_to_log_event(self):
        events: list[dict] = []

        async def fake_log_event(event_type, **kwargs):
            events.append(kwargs)

        with patch("app.logging_analytics.structured_logger.log_event", fake_log_event):
            async with TimedOperation("my_op", call_sid="CA123"):
                pass

        assert events[0]["call_sid"] == "CA123"

    async def test_does_not_suppress_exceptions(self):
        async def fake_log_event(event_type, **kwargs):
            pass

        with patch("app.logging_analytics.structured_logger.log_event", fake_log_event):
            with pytest.raises(ValueError, match="body error"):
                async with TimedOperation("op"):
                    raise ValueError("body error")

    async def test_latency_is_positive(self):
        import asyncio
        latencies: list[float] = []

        async def fake_log_event(event_type, **kwargs):
            latencies.append(kwargs.get("latency_ms", -1))

        with patch("app.logging_analytics.structured_logger.log_event", fake_log_event):
            async with TimedOperation("slow_op"):
                await asyncio.sleep(0.01)  # small real delay

        assert latencies[0] > 0
