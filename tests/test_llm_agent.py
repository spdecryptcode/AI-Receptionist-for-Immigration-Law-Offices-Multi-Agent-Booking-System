"""
Unit tests for ImmigrationAgent — signal detection, phase transitions,
and per-phase token limits.

VERIFICATION.md coverage:
  - Test 10: FSM state transitions driven by PHASE:* markers
  - Test 12: Per-state max_tokens values

All tests are pure (no OpenAI API calls): they test the signal/phase logic
that processes the LLM's *text output*, not the streaming itself.
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

from app.agent.llm_agent import (
    ConversationPhase,
    EMERGENCY_SIGNAL,
    SCHEDULE_SIGNAL,
    LANGUAGE_SWITCH_ES,
    LANGUAGE_SWITCH_EN,
    END_CALL_SIGNAL,
    ImmigrationAgent,
    _MAX_TOKENS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_agent(call_sid: str = "CA_llm_test") -> ImmigrationAgent:
    """Construct a minimal agent; language_preference defaults to 'en'."""
    return ImmigrationAgent(call_sid=call_sid, caller_phone="+10000000000")


# ---------------------------------------------------------------------------
# ConversationPhase enum
# ---------------------------------------------------------------------------

class TestConversationPhase:
    def test_all_eight_phases_exist(self):
        phases = list(ConversationPhase)
        assert len(phases) == 8

    def test_phase_values(self):
        assert ConversationPhase.GREETING.value == "greeting"
        assert ConversationPhase.CLOSING.value == "closing"
        assert ConversationPhase.INTAKE.value == "intake"
        assert ConversationPhase.BOOKING.value == "booking"


# ---------------------------------------------------------------------------
# Per-phase max_tokens — VERIFICATION.md Test 12
# ---------------------------------------------------------------------------

class TestMaxTokensPerPhase:
    @pytest.mark.parametrize("phase,expected", [
        (ConversationPhase.GREETING, 75),
        (ConversationPhase.IDENTIFICATION, 80),
        (ConversationPhase.URGENCY_TRIAGE, 100),
        (ConversationPhase.INTAKE, 150),
        (ConversationPhase.CONSULTATION_PITCH, 250),
        (ConversationPhase.BOOKING, 100),
        (ConversationPhase.CONFIRMATION, 100),
        (ConversationPhase.CLOSING, 75),
    ])
    def test_max_tokens_value(self, phase, expected):
        assert _MAX_TOKENS[phase] == expected

    def test_all_phases_have_entry(self):
        for phase in ConversationPhase:
            assert phase in _MAX_TOKENS, f"No max_tokens entry for {phase}"

    def test_greeting_shorter_than_pitch(self):
        assert _MAX_TOKENS[ConversationPhase.GREETING] < _MAX_TOKENS[ConversationPhase.CONSULTATION_PITCH]

    def test_intake_longer_than_booking(self):
        assert _MAX_TOKENS[ConversationPhase.INTAKE] > _MAX_TOKENS[ConversationPhase.BOOKING]


# ---------------------------------------------------------------------------
# check_signals — VERIFICATION.md (signal detection)
# ---------------------------------------------------------------------------

class TestCheckSignals:
    def test_no_signals_in_plain_response(self):
        agent = make_agent()
        sigs = agent.check_signals("Hello, how can I help you today?")
        assert sigs["emergency_transfer"] is False
        assert sigs["schedule_now"] is False
        assert sigs["language_switch_es"] is False
        assert sigs["language_switch_en"] is False
        assert sigs["end_call"] is False

    def test_detects_emergency_transfer_uppercase(self):
        agent = make_agent()
        sigs = agent.check_signals(f"Please hold. {EMERGENCY_SIGNAL} connecting you now.")
        assert sigs["emergency_transfer"] is True

    def test_detects_emergency_transfer_lowercase(self):
        agent = make_agent()
        sigs = agent.check_signals("emergency_transfer")
        assert sigs["emergency_transfer"] is True

    def test_detects_schedule_now(self):
        agent = make_agent()
        sigs = agent.check_signals(f"Great, {SCHEDULE_SIGNAL} let me find a slot.")
        assert sigs["schedule_now"] is True

    def test_detects_language_switch_es(self):
        agent = make_agent()
        sigs = agent.check_signals(f"Claro. {LANGUAGE_SWITCH_ES}")
        assert sigs["language_switch_es"] is True
        assert sigs["language_switch_en"] is False

    def test_detects_language_switch_en(self):
        agent = make_agent()
        sigs = agent.check_signals(f"Sure. {LANGUAGE_SWITCH_EN}")
        assert sigs["language_switch_en"] is True
        assert sigs["language_switch_es"] is False

    def test_detects_end_call(self):
        agent = make_agent()
        sigs = agent.check_signals(f"Goodbye! {END_CALL_SIGNAL}")
        assert sigs["end_call"] is True

    def test_multiple_signals_detected(self):
        agent = make_agent()
        text = f"{EMERGENCY_SIGNAL} and {SCHEDULE_SIGNAL}"
        sigs = agent.check_signals(text)
        assert sigs["emergency_transfer"] is True
        assert sigs["schedule_now"] is True

    def test_returns_all_expected_keys(self):
        agent = make_agent()
        sigs = agent.check_signals("")
        assert set(sigs.keys()) == {
            "emergency_transfer", "schedule_now",
            "language_switch_es", "language_switch_en", "end_call",
        }


# ---------------------------------------------------------------------------
# _maybe_advance_phase — VERIFICATION.md Test 10
# ---------------------------------------------------------------------------

class TestMaybeAdvancePhase:
    def test_phase_stays_greeting_without_marker(self):
        agent = make_agent()
        original_phase = agent.phase
        agent._maybe_advance_phase("Let me help you with your immigration question.")
        assert agent.phase == original_phase

    def test_advances_to_identification(self):
        agent = make_agent()
        agent._maybe_advance_phase("Welcome! PHASE:IDENTIFICATION let's get your details.")
        assert agent.phase == ConversationPhase.IDENTIFICATION

    def test_advances_to_urgency_triage(self):
        agent = make_agent()
        agent._maybe_advance_phase("PHASE:URGENCY_TRIAGE I need to understand your situation.")
        assert agent.phase == ConversationPhase.URGENCY_TRIAGE

    def test_advances_to_intake(self):
        agent = make_agent()
        agent._maybe_advance_phase("PHASE:INTAKE Let me collect your information.")
        assert agent.phase == ConversationPhase.INTAKE

    def test_advances_to_consultation_pitch(self):
        agent = make_agent()
        agent._maybe_advance_phase("PHASE:CONSULTATION_PITCH Based on your situation...")
        assert agent.phase == ConversationPhase.CONSULTATION_PITCH

    def test_advances_to_booking(self):
        agent = make_agent()
        agent._maybe_advance_phase("PHASE:BOOKING Let's schedule a consultation.")
        assert agent.phase == ConversationPhase.BOOKING

    def test_advances_to_confirmation(self):
        agent = make_agent()
        agent._maybe_advance_phase("PHASE:CONFIRMATION Your appointment is set.")
        assert agent.phase == ConversationPhase.CONFIRMATION

    def test_advances_to_closing(self):
        agent = make_agent()
        agent._maybe_advance_phase("PHASE:CLOSING Thank you for calling.")
        assert agent.phase == ConversationPhase.CLOSING

    def test_marker_case_insensitive(self):
        agent = make_agent()
        agent._maybe_advance_phase("phase:intake starting questions")
        assert agent.phase == ConversationPhase.INTAKE

    def test_first_matching_marker_wins(self):
        """If two markers appear, first one in the dict takes effect."""
        agent = make_agent()
        # IDENTIFICATION comes before BOOKING in the phase_map dict
        agent._maybe_advance_phase("PHASE:IDENTIFICATION and also PHASE:BOOKING")
        assert agent.phase == ConversationPhase.IDENTIFICATION


# ---------------------------------------------------------------------------
# _process_signals — language switch side effects
# ---------------------------------------------------------------------------

class TestProcessSignals:
    def test_language_switch_es_changes_language(self):
        agent = make_agent()
        assert agent.language == "en"
        agent._process_signals(f"Claro, {LANGUAGE_SWITCH_ES}")
        assert agent.language == "es"

    def test_language_switch_en_changes_language(self):
        agent = make_agent()
        agent.language = "es"
        agent._process_signals(f"Of course. {LANGUAGE_SWITCH_EN}")
        assert agent.language == "en"

    def test_no_language_change_without_signal(self):
        agent = make_agent()
        agent._process_signals("Let me check your case details.")
        assert agent.language == "en"


# ---------------------------------------------------------------------------
# Initial agent state
# ---------------------------------------------------------------------------

class TestAgentInitialState:
    def test_starts_in_greeting_phase(self):
        agent = make_agent()
        assert agent.phase == ConversationPhase.GREETING

    def test_starts_in_english(self):
        agent = make_agent()
        assert agent.language == "en"

    def test_token_counters_start_at_zero(self):
        agent = make_agent()
        assert agent._total_input_tokens == 0
        assert agent._total_output_tokens == 0

    def test_get_history_for_db_empty_initially(self):
        agent = make_agent()
        history = agent.get_history_for_db()
        assert isinstance(history, list)
        assert len(history) == 0


# ---------------------------------------------------------------------------
# switch_language
# ---------------------------------------------------------------------------

class TestSwitchLanguage:
    def test_switch_to_es(self):
        agent = make_agent()
        agent.switch_language("es")
        assert agent.language == "es"

    def test_switch_to_en(self):
        agent = make_agent()
        agent.language = "es"
        agent.switch_language("en")
        assert agent.language == "en"

    def test_switch_to_unknown_language_ignored(self):
        """switch_language only accepts 'en'/'es' — unknown codes are a no-op."""
        agent = make_agent()
        agent.switch_language("fr")
        assert agent.language == "en"  # unchanged
