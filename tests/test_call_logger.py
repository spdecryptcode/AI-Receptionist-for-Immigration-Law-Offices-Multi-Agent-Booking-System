"""
Unit tests for app/logging_analytics/call_logger.py.

Covers:
  _now_ms:
    - returns a positive integer
    - returns a value close to current epoch * 1000

  log_turn:
    - pushes JSON payload to "db_sync_queue" via Redis
    - payload contains call_sid, turn_index, role, text, phase, intent, ts
    - text truncated to 4000 chars
    - Redis failure falls back to _buffer_turn (msg_buffer prefix)
    - payload type field is "conversation_message"

  _buffer_turn:
    - pushes to key "msg_buffer:{call_sid}"
    - sets TTL of _MSG_BUFFER_TTL (600) on the key

  flush_turn_buffer:
    - pops items from "msg_buffer:{call_sid}" and re-pushes to "db_sync_queue"
    - returns count of items flushed
    - empty buffer returns 0
    - Redis exception → returns 0 (no raise)

  run_post_call_pipeline:
    - calls flush_turn_buffer
    - calls _generate_summary, _extract_structured_data, _analyse_sentiment concurrently
    - calls _write_call_summary_row with results
    - exception in one gather step normalised to None/{} and does not block others
    - summary exception normalised to None
    - structured exception normalised to {}
    - sentiment exception normalised to {}
"""
from __future__ import annotations

import json
import os
import time

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

from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from app.logging_analytics.call_logger import (
    _MSG_BUFFER_PREFIX,
    _MSG_BUFFER_TTL,
    _now_ms,
    flush_turn_buffer,
    log_turn,
    run_post_call_pipeline,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SID = "CA-logger-test"
_CONVO = [
    {"role": "user", "content": "Hello"},
    {"role": "assistant", "content": "Hi there"},
]


def _make_aioredis_conn(rpush_result=None, lpop_side_effect=None):
    """Return an AsyncMock that acts as an async context manager for aioredis."""
    mock_conn = AsyncMock()
    mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.__aexit__ = AsyncMock(return_value=False)
    if lpop_side_effect is not None:
        mock_conn.lpop = AsyncMock(side_effect=lpop_side_effect)
    else:
        mock_conn.lpop = AsyncMock(return_value=None)
    mock_conn.rpush = AsyncMock(return_value=rpush_result or 1)
    mock_conn.expire = AsyncMock()
    return mock_conn


# ---------------------------------------------------------------------------
# TestNowMs
# ---------------------------------------------------------------------------

class TestNowMs:
    def test_returns_positive_int(self):
        result = _now_ms()
        assert isinstance(result, int)
        assert result > 0

    def test_close_to_current_epoch_ms(self):
        expected = int(time.time() * 1000)
        result = _now_ms()
        assert abs(result - expected) < 5000  # within 5 seconds

    def test_constants(self):
        assert _MSG_BUFFER_PREFIX == "msg_buffer:"
        assert _MSG_BUFFER_TTL == 600


# ---------------------------------------------------------------------------
# TestLogTurn
# ---------------------------------------------------------------------------

class TestLogTurn:
    async def test_pushes_to_db_sync_queue(self):
        mock_conn = _make_aioredis_conn()
        with patch("app.logging_analytics.call_logger.aioredis.from_url", return_value=mock_conn):
            await log_turn(_SID, 0, "user", "Hello")
        mock_conn.rpush.assert_awaited_once()
        queue = mock_conn.rpush.call_args[0][0]
        assert queue == "db_sync_queue"

    async def test_payload_is_valid_json(self):
        mock_conn = _make_aioredis_conn()
        with patch("app.logging_analytics.call_logger.aioredis.from_url", return_value=mock_conn):
            await log_turn(_SID, 1, "assistant", "How can I help?")
        raw = mock_conn.rpush.call_args[0][1]
        payload = json.loads(raw)
        assert payload["call_sid"] == _SID
        assert payload["turn_index"] == 1
        assert payload["role"] == "assistant"
        assert payload["text"] == "How can I help?"

    async def test_payload_type_is_conversation_message(self):
        mock_conn = _make_aioredis_conn()
        with patch("app.logging_analytics.call_logger.aioredis.from_url", return_value=mock_conn):
            await log_turn(_SID, 0, "user", "hi")
        payload = json.loads(mock_conn.rpush.call_args[0][1])
        assert payload["type"] == "conversation_message"

    async def test_payload_contains_phase_and_intent(self):
        mock_conn = _make_aioredis_conn()
        with patch("app.logging_analytics.call_logger.aioredis.from_url", return_value=mock_conn):
            await log_turn(_SID, 0, "user", "text", phase="intake", intent="name")
        payload = json.loads(mock_conn.rpush.call_args[0][1])
        assert payload["phase"] == "intake"
        assert payload["intent"] == "name"

    async def test_text_truncated_to_4000(self):
        long_text = "x" * 5000
        mock_conn = _make_aioredis_conn()
        with patch("app.logging_analytics.call_logger.aioredis.from_url", return_value=mock_conn):
            await log_turn(_SID, 0, "user", long_text)
        payload = json.loads(mock_conn.rpush.call_args[0][1])
        assert len(payload["text"]) == 4000

    async def test_redis_failure_falls_back_to_buffer(self):
        """When main queue push fails, message goes to msg_buffer:{call_sid}."""
        fail_conn = AsyncMock()
        fail_conn.__aenter__ = AsyncMock(return_value=fail_conn)
        fail_conn.__aexit__ = AsyncMock(return_value=False)
        fail_conn.rpush = AsyncMock(side_effect=Exception("connection refused"))

        buffer_conn = _make_aioredis_conn()

        call_count = 0

        def make_conn(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return fail_conn if call_count == 1 else buffer_conn

        with patch("app.logging_analytics.call_logger.aioredis.from_url", side_effect=make_conn):
            await log_turn(_SID, 0, "user", "hello")

        # Buffer conn should have received the rpush
        buffer_conn.rpush.assert_awaited_once()
        key = buffer_conn.rpush.call_args[0][0]
        assert key == f"{_MSG_BUFFER_PREFIX}{_SID}"

    async def test_payload_contains_latency_ms(self):
        mock_conn = _make_aioredis_conn()
        with patch("app.logging_analytics.call_logger.aioredis.from_url", return_value=mock_conn):
            await log_turn(_SID, 0, "assistant", "hi", latency_ms=120)
        payload = json.loads(mock_conn.rpush.call_args[0][1])
        assert payload["latency_ms"] == 120

    async def test_payload_contains_ts(self):
        mock_conn = _make_aioredis_conn()
        with patch("app.logging_analytics.call_logger.aioredis.from_url", return_value=mock_conn):
            await log_turn(_SID, 0, "user", "hello")
        payload = json.loads(mock_conn.rpush.call_args[0][1])
        assert isinstance(payload["ts"], int)
        assert payload["ts"] > 0


# ---------------------------------------------------------------------------
# TestFlushTurnBuffer
# ---------------------------------------------------------------------------

class TestFlushTurnBuffer:
    async def test_returns_zero_for_empty_buffer(self):
        mock_conn = _make_aioredis_conn(lpop_side_effect=[None])
        with patch("app.logging_analytics.call_logger.aioredis.from_url", return_value=mock_conn):
            count = await flush_turn_buffer(_SID)
        assert count == 0

    async def test_flushes_items_to_db_sync_queue(self):
        items = ['{"type":"conversation_message","call_sid":"sid"}', None]
        mock_conn = _make_aioredis_conn(lpop_side_effect=items)
        with patch("app.logging_analytics.call_logger.aioredis.from_url", return_value=mock_conn):
            count = await flush_turn_buffer(_SID)
        assert count == 1
        # rpush should have been called with the item
        rpush_calls = mock_conn.rpush.await_args_list
        assert any(c[0][0] == "db_sync_queue" for c in rpush_calls)

    async def test_returns_count_of_flushed_items(self):
        items = ["item1", "item2", "item3", None]
        mock_conn = _make_aioredis_conn(lpop_side_effect=items)
        with patch("app.logging_analytics.call_logger.aioredis.from_url", return_value=mock_conn):
            count = await flush_turn_buffer(_SID)
        assert count == 3

    async def test_redis_exception_returns_zero(self):
        fail_conn = AsyncMock()
        fail_conn.__aenter__ = AsyncMock(return_value=fail_conn)
        fail_conn.__aexit__ = AsyncMock(return_value=False)
        fail_conn.lpop = AsyncMock(side_effect=Exception("redis down"))
        with patch("app.logging_analytics.call_logger.aioredis.from_url", return_value=fail_conn):
            count = await flush_turn_buffer(_SID)
        assert count == 0


# ---------------------------------------------------------------------------
# TestRunPostCallPipeline
# ---------------------------------------------------------------------------

class TestRunPostCallPipeline:
    async def _run_patched(
        self,
        summary="Call summary.",
        structured=None,
        sentiment=None,
        summary_exc=None,
        structured_exc=None,
        sentiment_exc=None,
    ):
        if structured is None:
            structured = {"case_type": "asylum"}
        if sentiment is None:
            sentiment = {"label": "positive", "overall_score": 0.8}

        flush_mock = AsyncMock()
        summary_mock = AsyncMock(return_value=summary) if not summary_exc else AsyncMock(side_effect=summary_exc)
        structured_mock = AsyncMock(return_value=structured) if not structured_exc else AsyncMock(side_effect=structured_exc)
        sentiment_mock = AsyncMock(return_value=sentiment) if not sentiment_exc else AsyncMock(side_effect=sentiment_exc)
        write_mock = AsyncMock()

        with patch("app.logging_analytics.call_logger.flush_turn_buffer", flush_mock), \
             patch("app.logging_analytics.call_logger._generate_summary", summary_mock), \
             patch("app.logging_analytics.call_logger._extract_structured_data", structured_mock), \
             patch("app.logging_analytics.call_logger._analyse_sentiment", sentiment_mock), \
             patch("app.logging_analytics.call_logger._write_call_summary_row", write_mock):
            await run_post_call_pipeline(_SID, _CONVO, {})
            return write_mock

    async def test_calls_flush_turn_buffer(self):
        flush_mock = AsyncMock()
        with patch("app.logging_analytics.call_logger.flush_turn_buffer", flush_mock), \
             patch("app.logging_analytics.call_logger._generate_summary", AsyncMock(return_value="")), \
             patch("app.logging_analytics.call_logger._extract_structured_data", AsyncMock(return_value={})), \
             patch("app.logging_analytics.call_logger._analyse_sentiment", AsyncMock(return_value={})), \
             patch("app.logging_analytics.call_logger._write_call_summary_row", AsyncMock()):
            await run_post_call_pipeline(_SID, _CONVO, {})
        flush_mock.assert_awaited_once_with(_SID)

    async def test_calls_write_call_summary_row(self):
        write_mock = await self._run_patched()
        write_mock.assert_awaited_once()

    async def test_write_receives_summary(self):
        write_mock = await self._run_patched(summary="My summary.")
        kwargs = write_mock.await_args[1]
        assert kwargs["summary"] == "My summary."

    async def test_write_receives_structured(self):
        write_mock = await self._run_patched(structured={"case_type": "TPS"})
        kwargs = write_mock.await_args[1]
        assert kwargs["structured"]["case_type"] == "TPS"

    async def test_write_receives_sentiment(self):
        write_mock = await self._run_patched(
            sentiment={"label": "negative", "overall_score": -0.5}
        )
        kwargs = write_mock.await_args[1]
        assert kwargs["sentiment"]["label"] == "negative"

    async def test_summary_exception_normalised_to_none(self):
        write_mock = await self._run_patched(summary_exc=Exception("openai down"))
        kwargs = write_mock.await_args[1]
        assert kwargs["summary"] is None

    async def test_structured_exception_normalised_to_empty_dict(self):
        write_mock = await self._run_patched(structured_exc=Exception("parse error"))
        kwargs = write_mock.await_args[1]
        assert kwargs["structured"] == {}

    async def test_sentiment_exception_normalised_to_empty_dict(self):
        write_mock = await self._run_patched(sentiment_exc=Exception("timeout"))
        kwargs = write_mock.await_args[1]
        assert kwargs["sentiment"] == {}

    async def test_one_failure_does_not_block_others(self):
        """If summary fails, structured + sentiment still run."""
        summary_mock = AsyncMock(side_effect=Exception("fail"))
        structured_mock = AsyncMock(return_value={"case_type": "asylum"})
        sentiment_mock = AsyncMock(return_value={"label": "neutral"})
        write_mock = AsyncMock()

        with patch("app.logging_analytics.call_logger.flush_turn_buffer", AsyncMock()), \
             patch("app.logging_analytics.call_logger._generate_summary", summary_mock), \
             patch("app.logging_analytics.call_logger._extract_structured_data", structured_mock), \
             patch("app.logging_analytics.call_logger._analyse_sentiment", sentiment_mock), \
             patch("app.logging_analytics.call_logger._write_call_summary_row", write_mock):
            await run_post_call_pipeline(_SID, _CONVO, {})

        structured_mock.assert_awaited_once()
        sentiment_mock.assert_awaited_once()
        write_kwargs = write_mock.await_args[1]
        assert write_kwargs["structured"]["case_type"] == "asylum"
