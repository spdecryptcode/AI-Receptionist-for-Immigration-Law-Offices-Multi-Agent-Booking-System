"""
Unit tests for UrgencyClassifier — VERIFICATION.md Test 17.

Tests cover:
  - _URGENCY_TOOL schema correctness (required keys, value ranges)
  - Early-exit when fewer than 2 turns
  - Full classify() path with a mocked OpenAI client
  - _queue_urgency_alert enqueues the correct JSON to Redis
  - create_urgency_task returns an asyncio.Task
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

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agent.urgency_classifier import (
    _URGENCY_TOOL,
    _queue_urgency_alert,
    create_urgency_task,
    UrgencyClassifier,
)
from app.voice.conversation_state import CallState, UrgencyLabel


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fresh_state(n_turns: int = 0) -> CallState:
    s = CallState(call_sid="CA_urgency_test")
    for i in range(n_turns):
        s.turns.append({
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"Message {i}",
        })
    return s


def _make_tool_call(score: int = 7, action: str = "expedite_consultation",
                    case_type: str = "asylum") -> MagicMock:
    """Return a mock chat completion that looks like a tool-calling response."""
    args = json.dumps({
        "urgency_score": score,
        "urgency_factors": ["pending hearing", "expired status"],
        "recommended_action": action,
        "detected_case_type": case_type,
    })
    tool_call = MagicMock()
    tool_call.function.arguments = args

    msg = MagicMock()
    msg.tool_calls = [tool_call]

    choice = MagicMock()
    choice.message = msg

    resp = MagicMock()
    resp.choices = [choice]
    return resp


# ---------------------------------------------------------------------------
# _URGENCY_TOOL schema
# ---------------------------------------------------------------------------

class TestUrgencyToolSchema:
    def test_type_is_function(self):
        assert _URGENCY_TOOL["type"] == "function"

    def test_function_name(self):
        assert _URGENCY_TOOL["function"]["name"] == "score_urgency"

    def test_required_fields_present(self):
        required = _URGENCY_TOOL["function"]["parameters"]["required"]
        assert "urgency_score" in required
        assert "urgency_factors" in required
        assert "recommended_action" in required
        assert "detected_case_type" in required

    def test_urgency_score_range(self):
        props = _URGENCY_TOOL["function"]["parameters"]["properties"]
        score_def = props["urgency_score"]
        assert score_def["minimum"] == 0
        assert score_def["maximum"] == 10

    def test_recommended_action_enum(self):
        props = _URGENCY_TOOL["function"]["parameters"]["properties"]
        actions = props["recommended_action"]["enum"]
        assert "routine_intake" in actions
        assert "emergency_transfer" in actions
        assert "expedite_consultation" in actions
        assert "immediate_attorney_callback" in actions

    def test_urgency_factors_max_items(self):
        props = _URGENCY_TOOL["function"]["parameters"]["properties"]
        assert props["urgency_factors"]["maxItems"] == 5


# ---------------------------------------------------------------------------
# UrgencyClassifier.classify — early return
# ---------------------------------------------------------------------------

class TestUrgencyClassifierEarlyReturn:
    async def test_skips_when_zero_turns(self):
        """No OpenAI call when state.turns is empty."""
        s = fresh_state(0)
        classifier = UrgencyClassifier("CA_test")
        redis_mock = AsyncMock()
        with patch("app.agent.urgency_classifier.get_openai_client") as mock_client:
            await classifier.classify(s, redis_mock)
        mock_client.assert_not_called()
        # State unchanged
        assert s.urgency_score == 0

    async def test_skips_when_one_turn(self):
        s = fresh_state(1)
        classifier = UrgencyClassifier("CA_test")
        redis_mock = AsyncMock()
        with patch("app.agent.urgency_classifier.get_openai_client") as mock_client:
            await classifier.classify(s, redis_mock)
        mock_client.assert_not_called()
        assert s.urgency_score == 0


# ---------------------------------------------------------------------------
# UrgencyClassifier.classify — full path with mocked OpenAI
# ---------------------------------------------------------------------------

class TestUrgencyClassifierFullPath:
    async def test_sets_urgency_score_and_label(self):
        s = fresh_state(3)
        classifier = UrgencyClassifier("CA_test")
        redis_mock = AsyncMock()

        mock_response = _make_tool_call(score=7, case_type="asylum")
        with patch.object(classifier, "_client") as mock_openai:
            mock_openai.chat.completions.create = AsyncMock(return_value=mock_response)
            with patch("app.agent.urgency_classifier.save_call_state", AsyncMock()):
                await classifier.classify(s, redis_mock)

        assert s.urgency_score == 7
        assert s.urgency_label == UrgencyLabel.HIGH

    async def test_fills_case_type_when_empty(self):
        """case_type is set from detected_case_type when not already in intake."""
        s = fresh_state(3)
        classifier = UrgencyClassifier("CA_test")
        redis_mock = AsyncMock()

        mock_response = _make_tool_call(score=5, case_type="removal_defense")
        with patch.object(classifier, "_client") as mock_openai:
            mock_openai.chat.completions.create = AsyncMock(return_value=mock_response)
            with patch("app.agent.urgency_classifier.save_call_state", AsyncMock()):
                await classifier.classify(s, redis_mock)

        assert s.intake.get("case_type") == "removal_defense"

    async def test_does_not_overwrite_existing_case_type(self):
        s = fresh_state(3)
        s.record_intake("case_type", "family")
        classifier = UrgencyClassifier("CA_test")
        redis_mock = AsyncMock()

        mock_response = _make_tool_call(score=4, case_type="employment")
        with patch.object(classifier, "_client") as mock_openai:
            mock_openai.chat.completions.create = AsyncMock(return_value=mock_response)
            with patch("app.agent.urgency_classifier.save_call_state", AsyncMock()):
                await classifier.classify(s, redis_mock)

        assert s.intake.get("case_type") == "family"  # unchanged

    async def test_queues_alert_for_high_urgency(self):
        s = fresh_state(3)
        classifier = UrgencyClassifier("CA_test")
        redis_mock = AsyncMock()

        mock_response = _make_tool_call(score=9, action="emergency_transfer")
        with patch.object(classifier, "_client") as mock_openai:
            mock_openai.chat.completions.create = AsyncMock(return_value=mock_response)
            with patch("app.agent.urgency_classifier.save_call_state", AsyncMock()):
                await classifier.classify(s, redis_mock)

        redis_mock.rpush.assert_called_once()
        queue_name = redis_mock.rpush.call_args[0][0]
        assert queue_name == "urgency_alerts"

    async def test_does_not_queue_alert_for_low_urgency(self):
        s = fresh_state(3)
        classifier = UrgencyClassifier("CA_test")
        redis_mock = AsyncMock()

        mock_response = _make_tool_call(score=2, action="routine_intake")
        with patch.object(classifier, "_client") as mock_openai:
            mock_openai.chat.completions.create = AsyncMock(return_value=mock_response)
            with patch("app.agent.urgency_classifier.save_call_state", AsyncMock()):
                await classifier.classify(s, redis_mock)

        redis_mock.rpush.assert_not_called()

    async def test_handles_openai_exception_gracefully(self):
        """classify() must not propagate exceptions — failure is non-fatal."""
        s = fresh_state(3)
        classifier = UrgencyClassifier("CA_test")
        redis_mock = AsyncMock()

        with patch.object(classifier, "_client") as mock_openai:
            mock_openai.chat.completions.create = AsyncMock(
                side_effect=RuntimeError("OpenAI unreachable")
            )
            # Should not raise
            await classifier.classify(s, redis_mock)

        # State should be unmodified
        assert s.urgency_score == 0

    async def test_handles_no_tool_call_in_response(self):
        s = fresh_state(3)
        classifier = UrgencyClassifier("CA_test")
        redis_mock = AsyncMock()

        msg = MagicMock()
        msg.tool_calls = None
        choice = MagicMock(); choice.message = msg
        resp = MagicMock(); resp.choices = [choice]

        with patch.object(classifier, "_client") as mock_openai:
            mock_openai.chat.completions.create = AsyncMock(return_value=resp)
            with patch("app.agent.urgency_classifier.save_call_state", AsyncMock()):
                await classifier.classify(s, redis_mock)

        assert s.urgency_score == 0

    async def test_includes_summary_in_dialogue_when_present(self):
        """If state.summary exists it should be prepended; verified by checking
        the messages passed to OpenAI contain the summary text."""
        s = fresh_state(3)
        s.summary = "Earlier: caller mentioned ICE detention."
        classifier = UrgencyClassifier("CA_test")
        redis_mock = AsyncMock()

        mock_response = _make_tool_call(score=9)
        captured_messages = []

        async def capture_call(**kwargs):
            captured_messages.extend(kwargs.get("messages", []))
            return mock_response

        with patch.object(classifier, "_client") as mock_openai:
            mock_openai.chat.completions.create = capture_call
            with patch("app.agent.urgency_classifier.save_call_state", AsyncMock()):
                await classifier.classify(s, redis_mock)

        combined = " ".join(m["content"] for m in captured_messages)
        assert "ICE detention" in combined


# ---------------------------------------------------------------------------
# _queue_urgency_alert
# ---------------------------------------------------------------------------

class TestQueueUrgencyAlert:
    async def test_pushes_to_urgency_alerts_key(self):
        redis_mock = AsyncMock()
        await _queue_urgency_alert(
            call_sid="CA_test",
            score=9,
            label="emergency",
            factors=["detained", "imminent removal"],
            action="emergency_transfer",
            redis=redis_mock,
        )
        redis_mock.rpush.assert_called_once()
        key, payload_str = redis_mock.rpush.call_args[0]
        assert key == "urgency_alerts"
        payload = json.loads(payload_str)
        assert payload["urgency_score"] == 9
        assert payload["urgency_label"] == "emergency"
        assert payload["call_sid"] == "CA_test"
        assert "detained" in payload["factors"]

    async def test_redis_error_does_not_propagate(self):
        redis_mock = AsyncMock()
        redis_mock.rpush.side_effect = ConnectionError("Redis down")
        # Should not raise
        await _queue_urgency_alert(
            call_sid="CA_test",
            score=9,
            label="emergency",
            factors=[],
            action="emergency_transfer",
            redis=redis_mock,
        )


# ---------------------------------------------------------------------------
# create_urgency_task
# ---------------------------------------------------------------------------

class TestCreateUrgencyTask:
    async def test_returns_asyncio_task(self):
        s = fresh_state(3)
        redis_mock = AsyncMock()
        with patch("app.agent.urgency_classifier.save_call_state", AsyncMock()):
            with patch("app.agent.urgency_classifier.get_openai_client") as mk:
                mk.return_value.chat.completions.create = AsyncMock(
                    return_value=_make_tool_call(score=3)
                )
                task = create_urgency_task(s, redis_mock)
                assert isinstance(task, asyncio.Task)
                await task  # let it complete cleanly
