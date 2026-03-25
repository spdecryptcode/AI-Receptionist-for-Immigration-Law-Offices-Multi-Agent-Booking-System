"""
Unit tests for CallState FSM — VERIFICATION.md Test 10 (FSM transitions).

Tests exercise the pure in-memory logic of CallState without touching Redis
or any external services.
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
import time

import pytest

from app.agent.llm_agent import ConversationPhase
from app.voice.conversation_state import (
    CallState,
    INTAKE_FIELDS,
    UrgencyLabel,
    score_to_urgency_label,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fresh_state(call_sid: str = "CA_test") -> CallState:
    return CallState(call_sid=call_sid)


# ---------------------------------------------------------------------------
# Initial state
# ---------------------------------------------------------------------------

class TestCallStateDefaults:
    def test_default_phase_is_greeting(self):
        s = fresh_state()
        assert s.phase == ConversationPhase.GREETING

    def test_default_language_english(self):
        s = fresh_state()
        assert s.language == "en"

    def test_default_urgency_low(self):
        s = fresh_state()
        assert s.urgency_label == UrgencyLabel.LOW
        assert s.urgency_score == 0

    def test_default_intake_empty(self):
        s = fresh_state()
        assert s.intake == {}

    def test_default_turns_empty(self):
        s = fresh_state()
        assert s.turns == []

    def test_default_phase_turns_zero(self):
        s = fresh_state()
        assert s.phase_turns == 0


# ---------------------------------------------------------------------------
# increment_turns
# ---------------------------------------------------------------------------

class TestIncrementTurns:
    def test_increments_phase_turns(self):
        s = fresh_state()
        s.increment_turns()
        assert s.phase_turns == 1

    def test_increments_multiple(self):
        s = fresh_state()
        for _ in range(5):
            s.increment_turns()
        assert s.phase_turns == 5

    def test_updates_last_updated(self):
        s = fresh_state()
        before = s.last_updated
        time.sleep(0.01)
        s.increment_turns()
        assert s.last_updated >= before


# ---------------------------------------------------------------------------
# record_intake
# ---------------------------------------------------------------------------

class TestRecordIntake:
    def test_stores_known_field(self):
        s = fresh_state()
        s.record_intake("full_name", "Maria Lopez")
        assert s.intake["full_name"] == "Maria Lopez"

    def test_ignores_unknown_field(self):
        s = fresh_state()
        s.record_intake("zodiac_sign", "Scorpio")
        assert "zodiac_sign" not in s.intake

    def test_overwrites_existing_value(self):
        s = fresh_state()
        s.record_intake("full_name", "Old Name")
        s.record_intake("full_name", "New Name")
        assert s.intake["full_name"] == "New Name"

    def test_missing_intake_fields_reflects_stored(self):
        s = fresh_state()
        s.record_intake("full_name", "Ana Cruz")
        missing = s.missing_intake_fields()
        assert "full_name" not in missing
        assert "case_type" in missing


# ---------------------------------------------------------------------------
# intake_complete
# ---------------------------------------------------------------------------

class TestIntakeComplete:
    def test_not_complete_when_empty(self):
        s = fresh_state()
        assert not s.intake_complete()

    def test_complete_when_critical_fields_filled(self):
        s = fresh_state()
        s.record_intake("full_name", "Ana Cruz")
        s.record_intake("country_of_birth", "Mexico")
        s.record_intake("current_immigration_status", "undocumented")
        s.record_intake("case_type", "asylum")
        assert s.intake_complete()

    def test_not_complete_when_one_critical_missing(self):
        s = fresh_state()
        s.record_intake("full_name", "Ana Cruz")
        s.record_intake("country_of_birth", "Mexico")
        s.record_intake("current_immigration_status", "undocumented")
        # case_type missing
        assert not s.intake_complete()


# ---------------------------------------------------------------------------
# advance_phase — VERIFICATION.md Test 10
# ---------------------------------------------------------------------------

class TestAdvancePhase:
    def test_cannot_advance_before_min_turns(self):
        s = fresh_state()
        # GREETING requires min 1 turn — zero turns → None
        result = s.advance_phase()
        assert result is None
        assert s.phase == ConversationPhase.GREETING

    def test_advance_after_min_turns_met(self):
        s = fresh_state()
        s.increment_turns()  # phase_turns = 1, min for GREETING = 1
        new_phase = s.advance_phase()
        assert new_phase == ConversationPhase.IDENTIFICATION
        assert s.phase == ConversationPhase.IDENTIFICATION

    def test_phase_turns_reset_on_advance(self):
        s = fresh_state()
        s.increment_turns()
        s.advance_phase()
        assert s.phase_turns == 0

    def test_full_phase_sequence(self):
        """All 8 phases can be visited in order."""
        expected = [
            ConversationPhase.IDENTIFICATION,
            ConversationPhase.URGENCY_TRIAGE,
            ConversationPhase.INTAKE,
            ConversationPhase.CONSULTATION_PITCH,
            ConversationPhase.BOOKING,
            ConversationPhase.CONFIRMATION,
            ConversationPhase.CLOSING,
        ]
        s = fresh_state()
        # INTAKE requires 3 turns; others 0 or 1
        min_turns_map = {
            ConversationPhase.GREETING: 1,
            ConversationPhase.IDENTIFICATION: 1,
            ConversationPhase.URGENCY_TRIAGE: 1,
            ConversationPhase.INTAKE: 3,
            ConversationPhase.CONSULTATION_PITCH: 1,
            ConversationPhase.BOOKING: 1,
            ConversationPhase.CONFIRMATION: 1,
        }
        for expected_phase in expected:
            current = s.phase
            min_t = min_turns_map.get(current, 1)
            for _ in range(min_t):
                s.increment_turns()
            result = s.advance_phase()
            assert result == expected_phase, (
                f"Expected advance from {current} → {expected_phase}, got {result}"
            )

    def test_no_advance_at_last_phase(self):
        s = fresh_state()
        s.force_phase(ConversationPhase.CLOSING)
        result = s.advance_phase()
        assert result is None

    def test_force_phase_jumps_directly(self):
        s = fresh_state()
        s.force_phase(ConversationPhase.BOOKING)
        assert s.phase == ConversationPhase.BOOKING
        assert s.phase_turns == 0

    def test_force_phase_same_phase_no_reset(self):
        s = fresh_state()
        s.increment_turns()
        s.force_phase(ConversationPhase.GREETING)  # same phase — no-op
        assert s.phase_turns == 1  # unchanged


# ---------------------------------------------------------------------------
# score_to_urgency_label
# ---------------------------------------------------------------------------

class TestScoreToUrgencyLabel:
    @pytest.mark.parametrize("score,expected", [
        (0, UrgencyLabel.LOW),
        (2, UrgencyLabel.LOW),
        (3, UrgencyLabel.MEDIUM),
        (5, UrgencyLabel.MEDIUM),
        (6, UrgencyLabel.HIGH),
        (8, UrgencyLabel.HIGH),
        (9, UrgencyLabel.EMERGENCY),
        (10, UrgencyLabel.EMERGENCY),
    ])
    def test_score_mapping(self, score, expected):
        assert score_to_urgency_label(score) == expected


# ---------------------------------------------------------------------------
# Redis round-trip
# ---------------------------------------------------------------------------

class TestRedisRoundTrip:
    def test_to_redis_and_back(self):
        s = fresh_state("CA_roundtrip")
        s.phase = ConversationPhase.INTAKE
        s.language = "es"
        s.record_intake("full_name", "Juan García")
        s.urgency_score = 7
        s.urgency_label = UrgencyLabel.HIGH
        s.phase_turns = 4
        s.turns = [
            {"role": "user", "content": "Hola"},
            {"role": "assistant", "content": "Buenos días"},
        ]
        s.summary = "Caller is Juan, urgency high."

        mapping = s.to_redis_mapping()
        # All values must be strings
        for k, v in mapping.items():
            assert isinstance(v, str), f"Expected str for key {k!r}, got {type(v)}"

        restored = CallState.from_redis_mapping("CA_roundtrip", mapping)
        assert restored.phase == ConversationPhase.INTAKE
        assert restored.language == "es"
        assert restored.intake["full_name"] == "Juan García"
        assert restored.urgency_score == 7
        assert restored.urgency_label == UrgencyLabel.HIGH
        assert restored.phase_turns == 4
        assert len(restored.turns) == 2
        assert restored.summary == "Caller is Juan, urgency high."

    def test_from_empty_mapping_returns_defaults(self):
        s = CallState.from_redis_mapping("CA_empty", {})
        assert s.phase == ConversationPhase.GREETING
        assert s.language == "en"
        assert s.intake == {}
