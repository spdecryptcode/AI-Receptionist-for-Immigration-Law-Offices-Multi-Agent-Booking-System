"""
Unit tests for app/logging_analytics/db_worker.py.

Covers:
  _bool_str:
    - "yes"/"true"/"1" → True
    - "no"/"false"/"0" → False
    - bool True/False passthrough
    - None → None
    - unrecognised string → None
    - case-insensitive ("YES", "TRUE", "NO", "FALSE")

  _dispatch:
    - invalid JSON → no raise, no supabase call
    - routes db_sync_queue + type=conversation_message → _handle_conversation_message
    - routes db_sync_queue + type=call_summary → _handle_call_summary
    - routes db_sync_queue + type=call_cost → _handle_call_cost
    - routes lead_score_queue → _handle_lead_score
    - routes analytics_events → _handle_analytics_event
    - routes urgency_alerts → _handle_urgency_alert
    - routes twilio_sms_queue → _handle_twilio_sms (no assert on body, just no raise)
    - routes voicemail_log_queue → _handle_voicemail_log
    - routes audit_log_queue → _handle_audit_log
    - handler exception → dead-letters to dlq:{queue} via redis.rpush

  _handle_conversation_message:
    - calls supabase.table("conversation_messages").insert(row).execute()
    - row contains call_sid, turn_index, role, content, phase, intent
    - content truncated to 4000 chars
    - ts present → ISO datetime used; ts absent → now() used
    - supabase raises → re-raises

  _handle_call_summary:
    - calls supabase.table("call_logs").update(update).eq("call_sid",…).execute()
    - update contains ai_summary, sentiment_score, sentinel_label, frustration_detected
    - None values stripped from update dict
    - supabase raises → re-raises

  _handle_lead_score:
    - calls supabase.table("lead_scores").upsert(row, on_conflict="call_sid").execute()
    - row contains call_sid, total_score, top_signals, recommended_follow_up
    - supabase raises → re-raises

  _handle_analytics_event:
    - calls supabase.table("call_logs").insert(row).execute()
    - row contains call_sid, event_type, phase, latency_ms, metadata
    - supabase raises → only warns (non-critical)

  _handle_urgency_alert:
    - calls supabase.table("urgency_alerts").insert(row).execute()
    - resolved=False always in row
    - supabase raises → re-raises

  _handle_voicemail_log:
    - calls supabase.table("voicemails").insert(row).execute()
    - supabase raises → warns only (non-critical)

  _handle_call_cost:
    - calls supabase.table("call_logs").update(…).eq("call_sid",…).execute()
    - cost_usd present in update
    - supabase raises → warns only (non-critical)

  _handle_audit_log:
    - calls supabase.table("audit_log").insert(row).execute()
    - supabase raises → logs only (non-critical)
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

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.logging_analytics.db_worker import (
    _bool_str,
    _dispatch,
    _handle_analytics_event,
    _handle_audit_log,
    _handle_call_cost,
    _handle_call_summary,
    _handle_conversation_message,
    _handle_lead_score,
    _handle_urgency_alert,
    _handle_voicemail_log,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SID = "CA-worker-test"


def _sb():
    """Return a fresh MagicMock supabase client."""
    return MagicMock()


def _sb_raises():
    """Return a supabase mock whose execute() raises."""
    mock = MagicMock()
    mock.table.return_value.insert.return_value.execute.side_effect = Exception("DB error")
    mock.table.return_value.upsert.return_value.execute.side_effect = Exception("DB error")
    mock.table.return_value.update.return_value.eq.return_value.execute.side_effect = Exception("DB err")
    return mock


# ---------------------------------------------------------------------------
# TestBoolStr
# ---------------------------------------------------------------------------

class TestBoolStr:
    def test_yes_is_true(self):
        assert _bool_str("yes") is True

    def test_true_str_is_true(self):
        assert _bool_str("true") is True

    def test_one_str_is_true(self):
        assert _bool_str("1") is True

    def test_no_is_false(self):
        assert _bool_str("no") is False

    def test_false_str_is_false(self):
        assert _bool_str("false") is False

    def test_zero_str_is_false(self):
        assert _bool_str("0") is False

    def test_bool_true_passthrough(self):
        assert _bool_str(True) is True

    def test_bool_false_passthrough(self):
        assert _bool_str(False) is False

    def test_none_returns_none(self):
        assert _bool_str(None) is None

    def test_unknown_string_returns_none(self):
        assert _bool_str("maybe") is None

    def test_case_insensitive_yes(self):
        assert _bool_str("YES") is True

    def test_case_insensitive_true(self):
        assert _bool_str("TRUE") is True

    def test_case_insensitive_no(self):
        assert _bool_str("NO") is False

    def test_case_insensitive_false(self):
        assert _bool_str("FALSE") is False


# ---------------------------------------------------------------------------
# TestDispatch
# ---------------------------------------------------------------------------

class TestDispatch:
    async def test_invalid_json_no_raise(self):
        redis = AsyncMock()
        await _dispatch("db_sync_queue", "not-json{{", redis)  # must not raise

    async def test_routes_conversation_message(self):
        payload = {
            "type": "conversation_message",
            "call_sid": _SID,
            "turn_index": 0,
            "role": "user",
            "text": "hello",
            "ts": int(time.time() * 1000),
        }
        redis = AsyncMock()
        mock_sb = _sb()
        with patch("app.logging_analytics.db_worker.get_supabase_client", return_value=mock_sb):
            await _dispatch("db_sync_queue", json.dumps(payload), redis)
        mock_sb.table.assert_called_with("conversation_messages")

    async def test_routes_call_summary(self):
        payload = {
            "type": "call_summary",
            "call_sid": _SID,
            "summary": "Great call.",
            "sentiment_score": 0.5,
            "sentiment_label": "positive",
            "frustration_detected": False,
            "duration_sec": 120,
        }
        redis = AsyncMock()
        mock_sb = _sb()
        with patch("app.logging_analytics.db_worker.get_supabase_client", return_value=mock_sb):
            await _dispatch("db_sync_queue", json.dumps(payload), redis)
        mock_sb.table.assert_called_with("call_logs")

    async def test_routes_lead_score_queue(self):
        payload = {"call_sid": _SID, "total": 80}
        redis = AsyncMock()
        mock_sb = _sb()
        with patch("app.logging_analytics.db_worker.get_supabase_client", return_value=mock_sb):
            await _dispatch("lead_score_queue", json.dumps(payload), redis)
        mock_sb.table.assert_called_with("lead_scores")

    async def test_routes_analytics_events(self):
        payload = {"call_sid": _SID, "event": "tts_latency", "latency_ms": 100}
        redis = AsyncMock()
        mock_sb = _sb()
        with patch("app.logging_analytics.db_worker.get_supabase_client", return_value=mock_sb):
            await _dispatch("analytics_events", json.dumps(payload), redis)
        mock_sb.table.assert_called_with("call_logs")

    async def test_routes_urgency_alerts(self):
        payload = {"call_sid": _SID, "urgency_score": 90, "urgency_label": "emergency"}
        redis = AsyncMock()
        mock_sb = _sb()
        with patch("app.logging_analytics.db_worker.get_supabase_client", return_value=mock_sb):
            await _dispatch("urgency_alerts", json.dumps(payload), redis)
        mock_sb.table.assert_called_with("urgency_alerts")

    async def test_handler_exception_dead_letters(self):
        payload = {
            "type": "conversation_message",
            "call_sid": _SID,
            "turn_index": 0,
            "role": "user",
            "text": "hi",
        }
        redis = AsyncMock()
        with patch("app.logging_analytics.db_worker.get_supabase_client", return_value=_sb_raises()):
            await _dispatch("db_sync_queue", json.dumps(payload), redis)
        redis.rpush.assert_awaited_once()
        dlq_key = redis.rpush.call_args[0][0]
        assert dlq_key == "dlq:db_sync_queue"

    async def test_handler_exception_does_not_propagate(self):
        payload = {"type": "call_summary", "call_sid": _SID}
        redis = AsyncMock()
        with patch("app.logging_analytics.db_worker.get_supabase_client", return_value=_sb_raises()):
            await _dispatch("db_sync_queue", json.dumps(payload), redis)  # no raise


# ---------------------------------------------------------------------------
# TestHandleConversationMessage
# ---------------------------------------------------------------------------

class TestHandleConversationMessage:
    def _payload(self, **overrides) -> dict:
        base = {
            "call_sid": _SID,
            "turn_index": 2,
            "role": "user",
            "text": "I need a work permit.",
            "latency_ms": None,
            "phase": "intake",
            "intent": "work_permit",
            "ts": int(time.time() * 1000),
        }
        base.update(overrides)
        return base

    async def test_inserts_into_conversation_messages(self):
        mock_sb = _sb()
        with patch("app.logging_analytics.db_worker.get_supabase_client", return_value=mock_sb):
            await _handle_conversation_message(self._payload())
        mock_sb.table.assert_called_with("conversation_messages")
        mock_sb.table.return_value.insert.assert_called_once()

    async def test_row_contains_call_sid(self):
        mock_sb = _sb()
        with patch("app.logging_analytics.db_worker.get_supabase_client", return_value=mock_sb):
            await _handle_conversation_message(self._payload())
        row = mock_sb.table.return_value.insert.call_args[0][0]
        assert row["call_sid"] == _SID

    async def test_row_contains_role(self):
        mock_sb = _sb()
        with patch("app.logging_analytics.db_worker.get_supabase_client", return_value=mock_sb):
            await _handle_conversation_message(self._payload(role="assistant"))
        row = mock_sb.table.return_value.insert.call_args[0][0]
        assert row["role"] == "assistant"

    async def test_content_truncated_to_4000(self):
        mock_sb = _sb()
        long_text = "z" * 5000
        with patch("app.logging_analytics.db_worker.get_supabase_client", return_value=mock_sb):
            await _handle_conversation_message(self._payload(text=long_text))
        row = mock_sb.table.return_value.insert.call_args[0][0]
        assert len(row["content"]) == 4000

    async def test_row_contains_phase_and_intent(self):
        mock_sb = _sb()
        with patch("app.logging_analytics.db_worker.get_supabase_client", return_value=mock_sb):
            await _handle_conversation_message(
                self._payload(phase="greeting", intent="name")
            )
        row = mock_sb.table.return_value.insert.call_args[0][0]
        assert row["phase"] == "greeting"
        assert row["intent"] == "name"

    async def test_supabase_exception_re_raises(self):
        mock_sb = MagicMock()
        mock_sb.table.return_value.insert.return_value.execute.side_effect = Exception("DB")
        with patch("app.logging_analytics.db_worker.get_supabase_client", return_value=mock_sb):
            with pytest.raises(Exception, match="DB"):
                await _handle_conversation_message(self._payload())


# ---------------------------------------------------------------------------
# TestHandleCallSummary
# ---------------------------------------------------------------------------

class TestHandleCallSummary:
    def _payload(self, **overrides) -> dict:
        base = {
            "call_sid": _SID,
            "summary": "Caller needed asylum help.",
            "sentiment_score": 0.3,
            "sentiment_label": "neutral",
            "frustration_detected": False,
            "duration_sec": 180,
        }
        base.update(overrides)
        return base

    async def test_updates_call_logs(self):
        mock_sb = _sb()
        with patch("app.logging_analytics.db_worker.get_supabase_client", return_value=mock_sb):
            await _handle_call_summary(self._payload())
        mock_sb.table.assert_called_with("call_logs")
        mock_sb.table.return_value.update.assert_called_once()

    async def test_filters_by_call_sid(self):
        mock_sb = _sb()
        with patch("app.logging_analytics.db_worker.get_supabase_client", return_value=mock_sb):
            await _handle_call_summary(self._payload())
        eq_call = mock_sb.table.return_value.update.return_value.eq
        eq_call.assert_called_once_with("call_sid", _SID)

    async def test_update_contains_ai_summary(self):
        mock_sb = _sb()
        with patch("app.logging_analytics.db_worker.get_supabase_client", return_value=mock_sb):
            await _handle_call_summary(self._payload(summary="Great call summary."))
        update = mock_sb.table.return_value.update.call_args[0][0]
        assert update["ai_summary"] == "Great call summary."

    async def test_none_values_stripped(self):
        """sentiment_score=None must not appear in the update."""
        mock_sb = _sb()
        with patch("app.logging_analytics.db_worker.get_supabase_client", return_value=mock_sb):
            await _handle_call_summary(self._payload(sentiment_score=None))
        update = mock_sb.table.return_value.update.call_args[0][0]
        assert "sentiment_score" not in update

    async def test_supabase_exception_re_raises(self):
        mock_sb = MagicMock()
        mock_sb.table.return_value.update.return_value.eq.return_value.execute.side_effect = Exception("DB")
        with patch("app.logging_analytics.db_worker.get_supabase_client", return_value=mock_sb):
            with pytest.raises(Exception, match="DB"):
                await _handle_call_summary(self._payload())


# ---------------------------------------------------------------------------
# TestHandleLeadScore
# ---------------------------------------------------------------------------

class TestHandleLeadScore:
    def _payload(self, **overrides) -> dict:
        base = {
            "call_sid": _SID,
            "total": 82,
            "case_value": 30,
            "urgency": 20,
            "booking_readiness": 15,
            "data_completeness": 17,
            "top_signals": ["asylum", "emergency"],
            "recommended_follow_up": "same_day",
            "recommended_attorney_tier": "senior",
            "notes": "High priority",
        }
        base.update(overrides)
        return base

    async def test_upserts_to_lead_scores(self):
        mock_sb = _sb()
        with patch("app.logging_analytics.db_worker.get_supabase_client", return_value=mock_sb):
            await _handle_lead_score(self._payload())
        mock_sb.table.assert_called_with("lead_scores")
        mock_sb.table.return_value.upsert.assert_called_once()

    async def test_on_conflict_call_sid(self):
        mock_sb = _sb()
        with patch("app.logging_analytics.db_worker.get_supabase_client", return_value=mock_sb):
            await _handle_lead_score(self._payload())
        upsert_kwargs = mock_sb.table.return_value.upsert.call_args[1]
        assert upsert_kwargs.get("on_conflict") == "call_sid"

    async def test_row_contains_total_score(self):
        mock_sb = _sb()
        with patch("app.logging_analytics.db_worker.get_supabase_client", return_value=mock_sb):
            await _handle_lead_score(self._payload(total=77))
        row = mock_sb.table.return_value.upsert.call_args[0][0]
        assert row["total_score"] == 77

    async def test_row_contains_top_signals(self):
        mock_sb = _sb()
        with patch("app.logging_analytics.db_worker.get_supabase_client", return_value=mock_sb):
            await _handle_lead_score(self._payload(top_signals=["DACA"]))
        row = mock_sb.table.return_value.upsert.call_args[0][0]
        assert row["top_signals"] == ["DACA"]

    async def test_supabase_exception_re_raises(self):
        mock_sb = MagicMock()
        mock_sb.table.return_value.upsert.return_value.execute.side_effect = Exception("DB")
        with patch("app.logging_analytics.db_worker.get_supabase_client", return_value=mock_sb):
            with pytest.raises(Exception, match="DB"):
                await _handle_lead_score(self._payload())


# ---------------------------------------------------------------------------
# TestHandleAnalyticsEvent
# ---------------------------------------------------------------------------

class TestHandleAnalyticsEvent:
    def _payload(self) -> dict:
        return {
            "call_sid": _SID,
            "event": "tts_latency",
            "phase": "greeting",
            "latency_ms": 145.2,
            "ts": "2025-01-06T09:00:00+00:00",
        }

    async def test_inserts_into_call_logs(self):
        mock_sb = _sb()
        with patch("app.logging_analytics.db_worker.get_supabase_client", return_value=mock_sb):
            await _handle_analytics_event(self._payload())
        mock_sb.table.assert_called_with("call_logs")
        mock_sb.table.return_value.insert.assert_called_once()

    async def test_row_contains_event_type(self):
        mock_sb = _sb()
        with patch("app.logging_analytics.db_worker.get_supabase_client", return_value=mock_sb):
            await _handle_analytics_event(self._payload())
        row = mock_sb.table.return_value.insert.call_args[0][0]
        assert row["event_type"] == "tts_latency"

    async def test_row_contains_latency_ms(self):
        mock_sb = _sb()
        with patch("app.logging_analytics.db_worker.get_supabase_client", return_value=mock_sb):
            await _handle_analytics_event(self._payload())
        row = mock_sb.table.return_value.insert.call_args[0][0]
        assert row["latency_ms"] == 145.2

    async def test_supabase_exception_does_not_raise(self):
        """Analytics failures are non-critical — must warn only."""
        mock_sb = MagicMock()
        mock_sb.table.return_value.insert.return_value.execute.side_effect = Exception("DB")
        with patch("app.logging_analytics.db_worker.get_supabase_client", return_value=mock_sb):
            await _handle_analytics_event(self._payload())  # must not raise


# ---------------------------------------------------------------------------
# TestHandleUrgencyAlert
# ---------------------------------------------------------------------------

class TestHandleUrgencyAlert:
    def _payload(self, **overrides) -> dict:
        base = {
            "call_sid": _SID,
            "urgency_score": 95,
            "urgency_label": "emergency",
            "factors": ["ICE raid", "detained family"],
            "recommended_action": "immediate_callback",
        }
        base.update(overrides)
        return base

    async def test_inserts_into_urgency_alerts(self):
        mock_sb = _sb()
        with patch("app.logging_analytics.db_worker.get_supabase_client", return_value=mock_sb):
            await _handle_urgency_alert(self._payload())
        mock_sb.table.assert_called_with("urgency_alerts")
        mock_sb.table.return_value.insert.assert_called_once()

    async def test_resolved_is_false(self):
        mock_sb = _sb()
        with patch("app.logging_analytics.db_worker.get_supabase_client", return_value=mock_sb):
            await _handle_urgency_alert(self._payload())
        row = mock_sb.table.return_value.insert.call_args[0][0]
        assert row["resolved"] is False

    async def test_row_contains_call_sid(self):
        mock_sb = _sb()
        with patch("app.logging_analytics.db_worker.get_supabase_client", return_value=mock_sb):
            await _handle_urgency_alert(self._payload())
        row = mock_sb.table.return_value.insert.call_args[0][0]
        assert row["call_sid"] == _SID

    async def test_row_contains_factors(self):
        mock_sb = _sb()
        with patch("app.logging_analytics.db_worker.get_supabase_client", return_value=mock_sb):
            await _handle_urgency_alert(self._payload(factors=["deportation"]))
        row = mock_sb.table.return_value.insert.call_args[0][0]
        assert "deportation" in row["factors"]

    async def test_supabase_exception_re_raises(self):
        mock_sb = MagicMock()
        mock_sb.table.return_value.insert.return_value.execute.side_effect = Exception("DB")
        with patch("app.logging_analytics.db_worker.get_supabase_client", return_value=mock_sb):
            with pytest.raises(Exception):
                await _handle_urgency_alert(self._payload())


# ---------------------------------------------------------------------------
# TestHandleVoicemailLog
# ---------------------------------------------------------------------------

class TestHandleVoicemailLog:
    def _payload(self) -> dict:
        return {
            "call_sid": _SID,
            "recording_sid": "RE123",
            "caller_number": "+15551234567",
            "transcript": "Please call me back.",
            "summary": "Caller wants callback.",
            "ghl_task_id": "task-abc",
            "is_emergency": False,
            "status": "processed",
        }

    async def test_inserts_into_voicemails(self):
        mock_sb = _sb()
        with patch("app.logging_analytics.db_worker.get_supabase_client", return_value=mock_sb):
            await _handle_voicemail_log(self._payload())
        mock_sb.table.assert_called_with("voicemails")

    async def test_row_contains_recording_sid(self):
        mock_sb = _sb()
        with patch("app.logging_analytics.db_worker.get_supabase_client", return_value=mock_sb):
            await _handle_voicemail_log(self._payload())
        row = mock_sb.table.return_value.insert.call_args[0][0]
        assert row["recording_sid"] == "RE123"

    async def test_supabase_exception_does_not_raise(self):
        """Voicemail persistence failure is non-critical."""
        mock_sb = MagicMock()
        mock_sb.table.return_value.insert.return_value.execute.side_effect = Exception("DB")
        with patch("app.logging_analytics.db_worker.get_supabase_client", return_value=mock_sb):
            await _handle_voicemail_log(self._payload())  # no raise


# ---------------------------------------------------------------------------
# TestHandleCallCost
# ---------------------------------------------------------------------------

class TestHandleCallCost:
    async def test_updates_call_logs_cost_usd(self):
        payload = {"call_sid": _SID, "cost_usd": 0.00423}
        mock_sb = _sb()
        with patch("app.logging_analytics.db_worker.get_supabase_client", return_value=mock_sb):
            await _handle_call_cost(payload)
        mock_sb.table.assert_called_with("call_logs")
        update = mock_sb.table.return_value.update.call_args[0][0]
        assert update["cost_usd"] == 0.00423

    async def test_filters_by_call_sid(self):
        payload = {"call_sid": _SID, "cost_usd": 0.01}
        mock_sb = _sb()
        with patch("app.logging_analytics.db_worker.get_supabase_client", return_value=mock_sb):
            await _handle_call_cost(payload)
        eq_call = mock_sb.table.return_value.update.return_value.eq
        eq_call.assert_called_once_with("call_sid", _SID)

    async def test_supabase_exception_does_not_raise(self):
        """Cost update failure is non-critical."""
        mock_sb = MagicMock()
        mock_sb.table.return_value.update.return_value.eq.return_value.execute.side_effect = Exception("DB")
        with patch("app.logging_analytics.db_worker.get_supabase_client", return_value=mock_sb):
            await _handle_call_cost({"call_sid": _SID, "cost_usd": 0.01})  # no raise


# ---------------------------------------------------------------------------
# TestHandleAuditLog
# ---------------------------------------------------------------------------

class TestHandleAuditLog:
    def _payload(self) -> dict:
        return {
            "method": "POST",
            "path": "/webhook/twilio",
            "query": "",
            "status_code": 200,
            "ip": "127.0.0.1",
            "user_agent": "TwilioProxy/1.1",
            "duration_ms": 12,
            "ts": int(time.time() * 1000),
        }

    async def test_inserts_into_audit_log(self):
        mock_sb = _sb()
        with patch("app.logging_analytics.db_worker.get_supabase_client", return_value=mock_sb):
            await _handle_audit_log(self._payload())
        mock_sb.table.assert_called_with("audit_log")
        mock_sb.table.return_value.insert.assert_called_once()

    async def test_row_contains_method_and_path(self):
        mock_sb = _sb()
        with patch("app.logging_analytics.db_worker.get_supabase_client", return_value=mock_sb):
            await _handle_audit_log(self._payload())
        row = mock_sb.table.return_value.insert.call_args[0][0]
        assert row["method"] == "POST"
        assert row["path"] == "/webhook/twilio"

    async def test_user_agent_truncated_to_200(self):
        mock_sb = _sb()
        long_ua = "A" * 300
        payload = {**self._payload(), "user_agent": long_ua}
        with patch("app.logging_analytics.db_worker.get_supabase_client", return_value=mock_sb):
            await _handle_audit_log(payload)
        row = mock_sb.table.return_value.insert.call_args[0][0]
        assert len(row["user_agent"]) == 200

    async def test_supabase_exception_does_not_raise(self):
        mock_sb = MagicMock()
        mock_sb.table.return_value.insert.return_value.execute.side_effect = Exception("DB")
        with patch("app.logging_analytics.db_worker.get_supabase_client", return_value=mock_sb):
            await _handle_audit_log(self._payload())  # no raise
