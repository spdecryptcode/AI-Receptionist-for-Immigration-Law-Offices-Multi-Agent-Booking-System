"""
Unit tests for app/logging_analytics/sentiment_scorer.py.

Covers:
  _neutral_defaults:
    - returns dict with all required keys
    - neutral label, 0.0 score, frustration_detected=False

  score_conversation:
    - empty conversation → _neutral_defaults() immediately (no OpenAI call)
    - successful OpenAI response → returns parsed tool-call JSON
    - OpenAI API exception → returns _neutral_defaults()
    - tool_calls is None/empty → returns _neutral_defaults()
    - uses last 20 turns of conversation (not all)
    - logs call_sid in result

  _SENTIMENT_TOOL schema:
    - required fields present in schema
    - label enum contains expected values
"""
from __future__ import annotations

import json
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

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.logging_analytics.sentiment_scorer import (
    _SENTIMENT_TOOL,
    _neutral_defaults,
    score_conversation,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CALL_SID = "CA1234567890abcdef"
_CONVO = [
    {"role": "user", "content": "I need help with my visa application."},
    {"role": "assistant", "content": "I can assist you with that today."},
]

_POSITIVE_RESULT = {
    "overall_score": 0.8,
    "label": "positive",
    "frustration_detected": False,
    "frustration_triggers": [],
    "caller_confidence": "high",
    "intake_gaps": [],
    "coaching_note": "Caller was confident and engaged.",
}


def _make_openai_response(result: dict):
    """Build a mock OpenAI response with a single tool call."""
    mock_tool_call = MagicMock()
    mock_tool_call.function.arguments = json.dumps(result)
    mock_resp = MagicMock()
    mock_resp.choices[0].message.tool_calls = [mock_tool_call]
    return mock_resp


def _make_openai_client(result: dict | None = None, raises: Exception | None = None):
    mock_client = AsyncMock()
    if raises:
        mock_client.chat.completions.create = AsyncMock(side_effect=raises)
    elif result is None:
        # tool_calls is None
        mock_resp = MagicMock()
        mock_resp.choices[0].message.tool_calls = None
        mock_client.chat.completions.create = AsyncMock(return_value=mock_resp)
    else:
        mock_client.chat.completions.create = AsyncMock(
            return_value=_make_openai_response(result)
        )
    return mock_client


# ---------------------------------------------------------------------------
# TestNeutralDefaults
# ---------------------------------------------------------------------------

class TestNeutralDefaults:
    def test_label_is_neutral(self):
        assert _neutral_defaults()["label"] == "neutral"

    def test_overall_score_is_zero(self):
        assert _neutral_defaults()["overall_score"] == 0.0

    def test_frustration_detected_false(self):
        assert _neutral_defaults()["frustration_detected"] is False

    def test_frustration_triggers_empty(self):
        assert _neutral_defaults()["frustration_triggers"] == []

    def test_intake_gaps_empty(self):
        assert _neutral_defaults()["intake_gaps"] == []

    def test_caller_confidence_medium(self):
        assert _neutral_defaults()["caller_confidence"] == "medium"

    def test_coaching_note_present(self):
        assert "coaching_note" in _neutral_defaults()

    def test_returns_new_dict_each_call(self):
        a = _neutral_defaults()
        b = _neutral_defaults()
        a["label"] = "changed"
        assert b["label"] == "neutral"


# ---------------------------------------------------------------------------
# TestScoreConversation
# ---------------------------------------------------------------------------

class TestScoreConversation:
    async def test_empty_conversation_returns_neutral_immediately(self):
        """No OpenAI call should happen for an empty conversation."""
        with patch("openai.AsyncOpenAI") as mock_cls:
            result = await score_conversation(_CALL_SID, [])
        mock_cls.assert_not_called()
        assert result == _neutral_defaults()

    async def test_successful_scoring_returns_parsed_result(self):
        mock_client = _make_openai_client(_POSITIVE_RESULT)
        with patch("openai.AsyncOpenAI", return_value=mock_client):
            result = await score_conversation(_CALL_SID, _CONVO)
        assert result["label"] == "positive"
        assert result["overall_score"] == 0.8
        assert result["frustration_detected"] is False

    async def test_openai_exception_returns_neutral(self):
        mock_client = _make_openai_client(raises=Exception("API rate limit"))
        with patch("openai.AsyncOpenAI", return_value=mock_client):
            result = await score_conversation(_CALL_SID, _CONVO)
        assert result == _neutral_defaults()

    async def test_no_tool_calls_returns_neutral(self):
        mock_client = _make_openai_client(result=None)
        with patch("openai.AsyncOpenAI", return_value=mock_client):
            result = await score_conversation(_CALL_SID, _CONVO)
        assert result == _neutral_defaults()

    async def test_all_required_fields_in_result(self):
        mock_client = _make_openai_client(_POSITIVE_RESULT)
        with patch("openai.AsyncOpenAI", return_value=mock_client):
            result = await score_conversation(_CALL_SID, _CONVO)
        for key in ("overall_score", "label", "frustration_detected",
                    "frustration_triggers", "caller_confidence", "intake_gaps",
                    "coaching_note"):
            assert key in result, f"Missing key: {key}"

    async def test_negative_sentiment_preserved(self):
        negative = {
            **_neutral_defaults(),
            "overall_score": -0.9,
            "label": "negative",
            "frustration_detected": True,
            "frustration_triggers": ["cost", "wait times"],
        }
        mock_client = _make_openai_client(negative)
        with patch("openai.AsyncOpenAI", return_value=mock_client):
            result = await score_conversation(_CALL_SID, _CONVO)
        assert result["label"] == "negative"
        assert result["frustration_detected"] is True
        assert "cost" in result["frustration_triggers"]

    async def test_uses_only_last_20_turns(self):
        """25 turns supplied — only last 20 should be in the API message."""
        turns_content: list[str] = []

        async def capture_create(**kwargs):
            # The user message content is the joined transcript
            turns_content.append(kwargs["messages"][1]["content"])
            return _make_openai_response(_neutral_defaults())

        mock_client = AsyncMock()
        mock_client.chat.completions.create = capture_create

        conversation = [{"role": "user", "content": f"turn {i}"} for i in range(25)]
        with patch("openai.AsyncOpenAI", return_value=mock_client):
            await score_conversation(_CALL_SID, conversation)

        assert len(turns_content) == 1
        text = turns_content[0]
        # turns 0-4 should NOT appear; turns 5-24 (last 20) should
        assert "turn 0" not in text
        assert "turn 4" not in text
        assert "turn 5" in text
        assert "turn 24" in text

    async def test_single_turn_conversation_works(self):
        mock_client = _make_openai_client(_POSITIVE_RESULT)
        with patch("openai.AsyncOpenAI", return_value=mock_client):
            result = await score_conversation(_CALL_SID, [{"role": "user", "content": "help"}])
        assert result["label"] == "positive"

    async def test_connection_error_returns_neutral(self):
        mock_client = _make_openai_client(raises=ConnectionError("network failure"))
        with patch("openai.AsyncOpenAI", return_value=mock_client):
            result = await score_conversation(_CALL_SID, _CONVO)
        assert result == _neutral_defaults()


# ---------------------------------------------------------------------------
# TestSentimentToolSchema
# ---------------------------------------------------------------------------

class TestSentimentToolSchema:
    def test_tool_name(self):
        assert _SENTIMENT_TOOL[0]["function"]["name"] == "record_sentiment"

    def test_required_fields(self):
        required = _SENTIMENT_TOOL[0]["function"]["parameters"]["required"]
        for field in ("overall_score", "label", "frustration_detected",
                      "frustration_triggers", "caller_confidence",
                      "intake_gaps", "coaching_note"):
            assert field in required

    def test_label_enum_values(self):
        props = _SENTIMENT_TOOL[0]["function"]["parameters"]["properties"]
        label_enum = props["label"]["enum"]
        assert "positive" in label_enum
        assert "negative" in label_enum
        assert "neutral" in label_enum
        assert "mixed" in label_enum

    def test_caller_confidence_enum_values(self):
        props = _SENTIMENT_TOOL[0]["function"]["parameters"]["properties"]
        conf_enum = props["caller_confidence"]["enum"]
        assert "high" in conf_enum
        assert "medium" in conf_enum
        assert "low" in conf_enum

    def test_schema_is_type_function(self):
        assert _SENTIMENT_TOOL[0]["type"] == "function"
