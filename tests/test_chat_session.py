"""
Tests for app/chat/session.py

Covers:
  - create_session() returns correct schema and persists to Redis
  - create_session() normalises unsupported language to "en"
  - get_session() returns None on Redis miss
  - get_session() deserialises valid JSON from Redis
  - get_session() returns None if stored value is corrupt JSON
  - save_session() calls setex with correct key and TTL
  - append_turn() appends user and assistant turns
  - append_turn() keeps only the last 12 turns (trim)
  - append_turn() returns None when session does not exist
  - check_rate_limit() returns True when under limit
  - check_rate_limit() returns False when over limit (>30)
  - delete_session() calls redis.delete with correct key
"""
from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_redis(stored_value=None):
    """Return an AsyncMock redis client with configurable get() return value."""
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=stored_value)
    redis.setex = AsyncMock(return_value=True)
    redis.delete = AsyncMock(return_value=1)
    redis.incr = AsyncMock(return_value=1)   # default: first call
    redis.expire = AsyncMock(return_value=True)
    return redis


def _make_session(session_id: str = "test-session-id", language: str = "en") -> dict:
    return {
        "session_id": session_id,
        "language": language,
        "created_at": time.time(),
        "turns": [],
        "intake": {},
        "phase": "GREETING",
        "case_type": None,
    }


# ---------------------------------------------------------------------------
# create_session
# ---------------------------------------------------------------------------

class TestCreateSession:
    @pytest.mark.asyncio
    async def test_returns_dict_with_required_keys(self):
        mock_redis = _make_mock_redis()
        with patch("app.chat.session.get_redis_client", return_value=mock_redis):
            from app.chat.session import create_session
            result = await create_session("en")
        assert "session_id" in result
        assert "language" in result
        assert "turns" in result
        assert "phase" in result

    @pytest.mark.asyncio
    async def test_default_language_is_en(self):
        mock_redis = _make_mock_redis()
        with patch("app.chat.session.get_redis_client", return_value=mock_redis):
            from app.chat.session import create_session
            result = await create_session()
        assert result["language"] == "en"

    @pytest.mark.asyncio
    async def test_es_language_preserved(self):
        mock_redis = _make_mock_redis()
        with patch("app.chat.session.get_redis_client", return_value=mock_redis):
            from app.chat.session import create_session
            result = await create_session("es")
        assert result["language"] == "es"

    @pytest.mark.asyncio
    async def test_unsupported_language_normalises_to_en(self):
        mock_redis = _make_mock_redis()
        with patch("app.chat.session.get_redis_client", return_value=mock_redis):
            from app.chat.session import create_session
            result = await create_session("fr")
        assert result["language"] == "en"

    @pytest.mark.asyncio
    async def test_initial_phase_is_greeting(self):
        mock_redis = _make_mock_redis()
        with patch("app.chat.session.get_redis_client", return_value=mock_redis):
            from app.chat.session import create_session
            result = await create_session("en")
        assert result["phase"] == "GREETING"

    @pytest.mark.asyncio
    async def test_initial_turns_empty(self):
        mock_redis = _make_mock_redis()
        with patch("app.chat.session.get_redis_client", return_value=mock_redis):
            from app.chat.session import create_session
            result = await create_session("en")
        assert result["turns"] == []

    @pytest.mark.asyncio
    async def test_persists_to_redis_with_ttl(self):
        mock_redis = _make_mock_redis()
        with patch("app.chat.session.get_redis_client", return_value=mock_redis):
            from app.chat.session import create_session
            result = await create_session("en")
        mock_redis.setex.assert_called_once()
        args = mock_redis.setex.call_args[0]
        assert f"chat_session:{result['session_id']}" == args[0]
        assert args[1] == 60 * 60 * 24  # 24h TTL

    @pytest.mark.asyncio
    async def test_session_id_is_string(self):
        mock_redis = _make_mock_redis()
        with patch("app.chat.session.get_redis_client", return_value=mock_redis):
            from app.chat.session import create_session
            result = await create_session("en")
        assert isinstance(result["session_id"], str)
        assert len(result["session_id"]) >= 10


# ---------------------------------------------------------------------------
# get_session
# ---------------------------------------------------------------------------

class TestGetSession:
    @pytest.mark.asyncio
    async def test_returns_none_on_cache_miss(self):
        mock_redis = _make_mock_redis(stored_value=None)
        with patch("app.chat.session.get_redis_client", return_value=mock_redis):
            from app.chat.session import get_session
            result = await get_session("nonexistent-id")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_parsed_data_on_hit(self):
        session = _make_session()
        mock_redis = _make_mock_redis(stored_value=json.dumps(session))
        with patch("app.chat.session.get_redis_client", return_value=mock_redis):
            from app.chat.session import get_session
            result = await get_session("test-session-id")
        assert result["session_id"] == "test-session-id"
        assert result["language"] == "en"

    @pytest.mark.asyncio
    async def test_returns_none_on_corrupt_json(self):
        mock_redis = _make_mock_redis(stored_value=b"not-valid-json{{{{")
        with patch("app.chat.session.get_redis_client", return_value=mock_redis):
            from app.chat.session import get_session
            result = await get_session("any-id")
        assert result is None


# ---------------------------------------------------------------------------
# save_session
# ---------------------------------------------------------------------------

class TestSaveSession:
    @pytest.mark.asyncio
    async def test_calls_setex_with_correct_key(self):
        mock_redis = _make_mock_redis()
        session = _make_session("my-session-42")
        with patch("app.chat.session.get_redis_client", return_value=mock_redis):
            from app.chat.session import save_session
            await save_session(session)
        mock_redis.setex.assert_called_once()
        key = mock_redis.setex.call_args[0][0]
        assert key == "chat_session:my-session-42"

    @pytest.mark.asyncio
    async def test_ttl_is_24_hours(self):
        mock_redis = _make_mock_redis()
        session = _make_session("sess-ttl-test")
        with patch("app.chat.session.get_redis_client", return_value=mock_redis):
            from app.chat.session import save_session
            await save_session(session)
        ttl = mock_redis.setex.call_args[0][1]
        assert ttl == 60 * 60 * 24

    @pytest.mark.asyncio
    async def test_stores_valid_json(self):
        mock_redis = _make_mock_redis()
        session = _make_session("sess-json-test")
        with patch("app.chat.session.get_redis_client", return_value=mock_redis):
            from app.chat.session import save_session
            await save_session(session)
        stored = mock_redis.setex.call_args[0][2]
        parsed = json.loads(stored)
        assert parsed["session_id"] == "sess-json-test"


# ---------------------------------------------------------------------------
# append_turn
# ---------------------------------------------------------------------------

class TestAppendTurn:
    @pytest.mark.asyncio
    async def test_appends_user_turn(self):
        session = _make_session("turn-test")
        mock_redis = _make_mock_redis(stored_value=json.dumps(session))
        with patch("app.chat.session.get_redis_client", return_value=mock_redis):
            from app.chat.session import append_turn
            result = await append_turn("turn-test", "user", "Hello, I need immigration help.")
        assert result is not None
        assert result["turns"][-1]["role"] == "user"
        assert result["turns"][-1]["content"] == "Hello, I need immigration help."

    @pytest.mark.asyncio
    async def test_appends_assistant_turn(self):
        session = _make_session("turn-asst")
        mock_redis = _make_mock_redis(stored_value=json.dumps(session))
        with patch("app.chat.session.get_redis_client", return_value=mock_redis):
            from app.chat.session import append_turn
            result = await append_turn("turn-asst", "assistant", "How can I help you?")
        assert result["turns"][-1]["role"] == "assistant"

    @pytest.mark.asyncio
    async def test_trims_to_12_turns(self):
        session = _make_session("trim-test")
        # Pre-populate with 12 turns
        session["turns"] = [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"msg {i}"}
            for i in range(12)
        ]
        mock_redis = _make_mock_redis(stored_value=json.dumps(session))
        with patch("app.chat.session.get_redis_client", return_value=mock_redis):
            from app.chat.session import append_turn
            result = await append_turn("trim-test", "user", "13th message")
        assert len(result["turns"]) == 12
        assert result["turns"][-1]["content"] == "13th message"

    @pytest.mark.asyncio
    async def test_returns_none_when_session_missing(self):
        mock_redis = _make_mock_redis(stored_value=None)
        with patch("app.chat.session.get_redis_client", return_value=mock_redis):
            from app.chat.session import append_turn
            result = await append_turn("ghost-session", "user", "hello")
        assert result is None


# ---------------------------------------------------------------------------
# check_rate_limit
# ---------------------------------------------------------------------------

class TestCheckRateLimit:
    @pytest.mark.asyncio
    async def test_returns_true_when_first_call(self):
        mock_redis = _make_mock_redis()
        mock_redis.incr = AsyncMock(return_value=1)
        with patch("app.chat.session.get_redis_client", return_value=mock_redis):
            from app.chat.session import check_rate_limit
            result = await check_rate_limit("127.0.0.1")
        assert result is True

    @pytest.mark.asyncio
    async def test_returns_true_when_at_limit(self):
        mock_redis = _make_mock_redis()
        mock_redis.incr = AsyncMock(return_value=30)
        with patch("app.chat.session.get_redis_client", return_value=mock_redis):
            from app.chat.session import check_rate_limit
            result = await check_rate_limit("10.0.0.1")
        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_when_over_limit(self):
        mock_redis = _make_mock_redis()
        mock_redis.incr = AsyncMock(return_value=31)
        with patch("app.chat.session.get_redis_client", return_value=mock_redis):
            from app.chat.session import check_rate_limit
            result = await check_rate_limit("10.0.0.2")
        assert result is False

    @pytest.mark.asyncio
    async def test_sets_expiry_on_first_call(self):
        mock_redis = _make_mock_redis()
        mock_redis.incr = AsyncMock(return_value=1)
        with patch("app.chat.session.get_redis_client", return_value=mock_redis):
            from app.chat.session import check_rate_limit
            await check_rate_limit("192.168.1.1")
        mock_redis.expire.assert_called_once()
        # TTL should be 60 seconds
        assert mock_redis.expire.call_args[0][1] == 60

    @pytest.mark.asyncio
    async def test_does_not_reset_expiry_on_subsequent_calls(self):
        mock_redis = _make_mock_redis()
        mock_redis.incr = AsyncMock(return_value=5)  # Not first call
        with patch("app.chat.session.get_redis_client", return_value=mock_redis):
            from app.chat.session import check_rate_limit
            await check_rate_limit("192.168.1.2")
        mock_redis.expire.assert_not_called()


# ---------------------------------------------------------------------------
# delete_session
# ---------------------------------------------------------------------------

class TestDeleteSession:
    @pytest.mark.asyncio
    async def test_calls_redis_delete_with_correct_key(self):
        mock_redis = _make_mock_redis()
        with patch("app.chat.session.get_redis_client", return_value=mock_redis):
            from app.chat.session import delete_session
            await delete_session("del-session-99")
        mock_redis.delete.assert_called_once_with("chat_session:del-session-99")
