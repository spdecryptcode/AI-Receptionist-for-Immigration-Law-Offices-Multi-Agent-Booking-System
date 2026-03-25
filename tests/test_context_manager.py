"""
Unit tests for ContextManager — VERIFICATION.md Test 11 (context window).

Tests the pure synchronous methods (build_messages, get_full_history,
recent_turns, has_summary) without triggering any async GPT-4o summarization.
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
import pytest

from app.agent.llm_agent import ConversationPhase
from app.voice.context_manager import ContextManager, HISTORY_MAX_TURNS, _TURNS_TO_COMPRESS
from app.voice.conversation_state import CallState, UrgencyLabel


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fresh_state(call_sid: str = "CA_ctx_test") -> CallState:
    return CallState(call_sid=call_sid)


def state_with_turns(n: int) -> CallState:
    """Return a CallState with n verbatim turns (alternating user/assistant)."""
    s = fresh_state()
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        s.turns.append({"role": role, "content": f"Message {i}"})
    return s


def state_with_summary(summary: str, n_turns: int = 2) -> CallState:
    """Return a CallState that already has a summary and n_turns verbatim turns."""
    s = state_with_turns(n_turns)
    s.summary = summary
    return s


# ---------------------------------------------------------------------------
# Constants sanity
# ---------------------------------------------------------------------------

class TestConstants:
    def test_history_max_turns_positive(self):
        assert HISTORY_MAX_TURNS > 0

    def test_turns_to_compress_less_than_max(self):
        assert _TURNS_TO_COMPRESS < HISTORY_MAX_TURNS


# ---------------------------------------------------------------------------
# get_full_history
# ---------------------------------------------------------------------------

class TestGetFullHistory:
    def test_empty_turns_returns_empty_list(self):
        s = fresh_state()
        ctx = ContextManager(s)
        assert ctx.get_full_history() == []

    def test_returns_copy_of_turns(self):
        s = state_with_turns(3)
        ctx = ContextManager(s)
        hist = ctx.get_full_history()
        assert len(hist) == 3

    def test_does_not_include_summary_in_history(self):
        """get_full_history returns verbatim turns only, not the summary text."""
        s = state_with_summary("Earlier summary text", n_turns=2)
        ctx = ContextManager(s)
        hist = ctx.get_full_history()
        assert len(hist) == 2
        for entry in hist:
            assert "Earlier summary text" not in entry["content"]

    def test_returns_defensive_copy(self):
        """Mutating the returned list should not affect the state."""
        s = state_with_turns(2)
        ctx = ContextManager(s)
        hist = ctx.get_full_history()
        hist.append({"role": "user", "content": "extra"})
        assert len(s.turns) == 2  # original unchanged


# ---------------------------------------------------------------------------
# build_messages — VERIFICATION.md Test 11
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = "You are Sofia, an immigration assistant."


class TestBuildMessages:
    def test_always_starts_with_system_prompt(self):
        s = fresh_state()
        ctx = ContextManager(s)
        msgs = ctx.build_messages(SYSTEM_PROMPT)
        assert msgs[0]["role"] == "system"
        assert msgs[0]["content"] == SYSTEM_PROMPT

    def test_no_context_injection_when_empty_state(self):
        """With no summary, intake, urgency, or extra_context → only 1 system msg."""
        s = fresh_state()
        ctx = ContextManager(s)
        msgs = ctx.build_messages(SYSTEM_PROMPT)
        system_msgs = [m for m in msgs if m["role"] == "system"]
        # Only the static system prompt; no injected context
        assert len(system_msgs) == 1

    def test_turns_appended_after_system_message(self):
        s = state_with_turns(3)
        ctx = ContextManager(s)
        msgs = ctx.build_messages(SYSTEM_PROMPT)
        # Last 3 messages should be the verbatim turns
        turn_messages = [m for m in msgs if m["role"] in ("user", "assistant")]
        assert len(turn_messages) == 3

    def test_summary_injected_as_second_system_message(self):
        summary = "Caller is Maria, urgency high, needs DACA renewal."
        s = state_with_summary(summary, n_turns=1)
        ctx = ContextManager(s)
        msgs = ctx.build_messages(SYSTEM_PROMPT)
        system_msgs = [m for m in msgs if m["role"] == "system"]
        assert len(system_msgs) == 2
        assert summary in system_msgs[1]["content"]

    def test_intake_injected_in_context_message(self):
        s = fresh_state()
        s.record_intake("full_name", "Elena Ruiz")
        s.record_intake("case_type", "asylum")
        ctx = ContextManager(s)
        msgs = ctx.build_messages(SYSTEM_PROMPT)
        context_msg = next(
            (m for m in msgs[1:] if m["role"] == "system"), None
        )
        assert context_msg is not None
        assert "Elena Ruiz" in context_msg["content"]
        assert "asylum" in context_msg["content"]

    def test_high_urgency_injected_in_context(self):
        s = fresh_state()
        s.urgency_score = 8
        s.urgency_label = UrgencyLabel.HIGH
        ctx = ContextManager(s)
        msgs = ctx.build_messages(SYSTEM_PROMPT)
        context_text = " ".join(
            m["content"] for m in msgs if m["role"] == "system"
        )
        assert "8" in context_text
        assert "high" in context_text.lower()

    def test_low_urgency_not_injected(self):
        """Urgency scores < 6 should NOT add an urgency block."""
        s = fresh_state()
        s.urgency_score = 3
        s.urgency_label = UrgencyLabel.MEDIUM
        ctx = ContextManager(s)
        msgs = ctx.build_messages(SYSTEM_PROMPT)
        context_text = " ".join(
            m["content"] for m in msgs if m["role"] == "system"
        )
        # urgency block only injected for score >= 6
        assert "Score=3" not in context_text

    def test_extra_context_appended(self):
        s = fresh_state()
        ctx = ContextManager(s)
        extra = "[Next intake question (EN)]\nField: full_name\nAsk: What is your name?"
        msgs = ctx.build_messages(SYSTEM_PROMPT, extra_context=extra)
        context_text = " ".join(
            m["content"] for m in msgs if m["role"] == "system"
        )
        assert "full_name" in context_text
        assert "What is your name?" in context_text

    def test_message_order_system_then_turns(self):
        """System messages must come before user/assistant turns."""
        s = state_with_turns(4)
        s.summary = "Summary here"
        s.record_intake("full_name", "Test")
        ctx = ContextManager(s)
        msgs = ctx.build_messages(SYSTEM_PROMPT)
        saw_turn = False
        for m in msgs:
            if m["role"] in ("user", "assistant"):
                saw_turn = True
            if saw_turn and m["role"] == "system":
                pytest.fail("System message appeared after user/assistant turn")


# ---------------------------------------------------------------------------
# recent_turns property
# ---------------------------------------------------------------------------

class TestRecentTurns:
    def test_returns_all_turns_under_limit(self):
        n = HISTORY_MAX_TURNS - 2
        s = state_with_turns(n)
        ctx = ContextManager(s)
        assert len(ctx.recent_turns) == n

    def test_returns_defensive_copy(self):
        s = state_with_turns(2)
        ctx = ContextManager(s)
        recent = ctx.recent_turns
        recent.append({"role": "user", "content": "extra"})
        assert len(s.turns) == 2


# ---------------------------------------------------------------------------
# has_summary property
# ---------------------------------------------------------------------------

class TestHasSummary:
    def test_false_when_no_summary(self):
        s = fresh_state()
        ctx = ContextManager(s)
        assert ctx.has_summary is False

    def test_true_when_summary_present(self):
        s = state_with_summary("Previous conversation summary.", n_turns=2)
        ctx = ContextManager(s)
        assert ctx.has_summary is True


# ---------------------------------------------------------------------------
# async add_turn — stays under HISTORY_MAX_TURNS (no GPT call needed)
# ---------------------------------------------------------------------------

class TestAddTurnWindowManagement:
    async def test_turns_stay_bounded_without_compression(self):
        """
        With fewer than HISTORY_MAX_TURNS turns, turns are appended directly.
        """
        s = fresh_state()
        ctx = ContextManager(s)
        for i in range(HISTORY_MAX_TURNS):
            await ctx.add_turn("user" if i % 2 == 0 else "assistant", f"msg {i}")
        # Should have exactly HISTORY_MAX_TURNS turns
        assert len(s.turns) == HISTORY_MAX_TURNS

    async def test_exceeding_limit_triggers_compression(self, monkeypatch):
        """
        When turns exceed HISTORY_MAX_TURNS, _compress_old_turns is called.
        We monkeypatch _summarize to return a fixed string so no GPT call occurs.
        """
        s = fresh_state()
        ctx = ContextManager(s)

        async def fake_summarize(turns):
            return "Compressed: " + " | ".join(t["content"] for t in turns)

        monkeypatch.setattr(ctx, "_summarize", fake_summarize)

        # Add HISTORY_MAX_TURNS + 1 turns to trigger compression
        for i in range(HISTORY_MAX_TURNS + 1):
            await ctx.add_turn("user" if i % 2 == 0 else "assistant", f"turn {i}")

        # After compression, recent turns should be ≤ HISTORY_MAX_TURNS
        assert len(s.turns) <= HISTORY_MAX_TURNS
        # Summary should now contain compressed content
        assert "Compressed:" in s.summary

    async def test_compression_failure_restores_turns(self, monkeypatch):
        """If _summarize raises, turns are restored (no data loss)."""
        s = fresh_state()
        ctx = ContextManager(s)

        async def failing_summarize(turns):
            raise RuntimeError("GPT unreachable")

        monkeypatch.setattr(ctx, "_summarize", failing_summarize)

        total = HISTORY_MAX_TURNS + 1
        for i in range(total):
            await ctx.add_turn("user", f"msg {i}")

        # Turns should not be permanently discarded
        assert len(s.turns) == total
        # Summary remains empty (compression failed)
        assert s.summary == ""
