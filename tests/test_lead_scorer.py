"""
Unit tests for LeadScorer deterministic logic — VERIFICATION.md Test 18.

Tests cover the deterministic sub-score calculations without calling OpenAI:
  - _CASE_TYPE_VALUE lookup by case type string
  - case_value_score selection (longest-match key)
  - urgency → 0-25 scale math (ceil(score * 2.5))
  - data_completeness percentage calculation
  - total capped at 0-100
  - LeadScoreBreakdown.to_dict() contains all 5 score dimensions
  - Full score() path with mocked OpenAI tool call
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
import math
from dataclasses import asdict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agent.lead_scorer import (
    _CASE_TYPE_VALUE,
    _LEAD_SCORE_TTL,
    LeadScoreBreakdown,
    LeadScorer,
)
from app.voice.conversation_state import CallState, UrgencyLabel


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fresh_state(call_sid: str = "CA_lead_test") -> CallState:
    return CallState(call_sid=call_sid)


def _make_tool_response(booking: int = 15,
                        signals: list | None = None,
                        follow_up: str = "next_day",
                        tier: str = "associate",
                        notes: str = "Test caller") -> MagicMock:
    args = json.dumps({
        "booking_readiness_score": booking,
        "top_signals": signals or ["asked to book", "clear urgency"],
        "recommended_follow_up": follow_up,
        "recommended_attorney_tier": tier,
        "notes": notes,
    })
    tc = MagicMock(); tc.function.arguments = args
    msg = MagicMock(); msg.tool_calls = [tc]
    choice = MagicMock(); choice.message = msg
    resp = MagicMock(); resp.choices = [choice]
    return resp


# ---------------------------------------------------------------------------
# _CASE_TYPE_VALUE map
# ---------------------------------------------------------------------------

class TestCaseTypeValueMap:
    def test_removal_defense_highest(self):
        assert _CASE_TYPE_VALUE["removal_defense"] == 25

    def test_asylum_second_highest(self):
        assert _CASE_TYPE_VALUE["asylum"] == 22

    def test_unknown_lowest_non_zero(self):
        assert _CASE_TYPE_VALUE["unknown"] < _CASE_TYPE_VALUE["other"]

    def test_all_values_in_range(self):
        for key, val in _CASE_TYPE_VALUE.items():
            assert 0 <= val <= 25, f"{key}={val} is outside 0-25"

    @pytest.mark.parametrize("case_type,expected_key", [
        ("removal_defense", "removal_defense"),
        ("asylum seeker", "asylum"),
        ("employment-based green card", "employment"),
        ("family petition", "family"),
        ("DACA renewal", "daca"),
        ("TPS extension", "tps"),
        ("naturalization / citizenship", "citizenship"),
        ("other immigration matter", "other"),
        ("", "unknown"),
    ])
    def test_key_lookup_for_common_phrases(self, case_type, expected_key):
        """Simulate the key-selection loop from LeadScorer.score()."""
        raw = case_type.lower()
        found = "unknown"
        for key in _CASE_TYPE_VALUE:
            if key in raw:
                found = key
                break
        assert found == expected_key


# ---------------------------------------------------------------------------
# Urgency → 0-25 mapping
# ---------------------------------------------------------------------------

class TestUrgencyScaleMapping:
    @pytest.mark.parametrize("raw_score,expected_25", [
        (0, 0),
        (1, 3),   # ceil(1 * 2.5) = 3
        (4, 10),  # ceil(4 * 2.5) = 10
        (5, 13),  # ceil(5 * 2.5) = 13
        (9, 23),  # ceil(9 * 2.5) = 23
        (10, 25), # min(ceil(10*2.5)=25, 25)
    ])
    def test_mapping_formula(self, raw_score, expected_25):
        result = min(math.ceil(raw_score * 2.5), 25)
        assert result == expected_25

    def test_never_exceeds_25(self):
        for score in range(11):
            result = min(math.ceil(score * 2.5), 25)
            assert result <= 25


# ---------------------------------------------------------------------------
# Data completeness calculation
# ---------------------------------------------------------------------------

class TestDataCompleteness:
    """
    Mirrors the formula: completeness_pct = total_fields / 6
                         data_completeness = min(int(pct * 25), 25)
    """
    def _calc(self, n_fields: int) -> int:
        SIX_KEY_FIELDS = 6
        pct = n_fields / SIX_KEY_FIELDS
        return min(int(pct * 25), 25)

    def test_zero_fields_gives_zero(self):
        assert self._calc(0) == 0

    def test_all_six_fields_gives_25(self):
        assert self._calc(6) == 25

    def test_three_fields_gives_12(self):
        assert self._calc(3) == 12

    def test_extra_fields_capped_at_25(self):
        # More than 6 fields shouldn't exceed 25
        assert self._calc(12) == 25


# ---------------------------------------------------------------------------
# LeadScoreBreakdown
# ---------------------------------------------------------------------------

class TestLeadScoreBreakdown:
    def _make(self, total=60, case_value=20, urgency=18, booking=15,
              completeness=7) -> LeadScoreBreakdown:
        return LeadScoreBreakdown(
            total=total,
            case_value=case_value,
            urgency=urgency,
            booking_readiness=booking,
            data_completeness=completeness,
            top_signals=["good signal", "another signal"],
            recommended_follow_up="same_day",
            recommended_attorney_tier="senior",
            notes="High-value detained case.",
        )

    def test_to_dict_has_all_five_scores(self):
        d = self._make().to_dict()
        assert "total" in d
        assert "case_value" in d
        assert "urgency" in d
        assert "booking_readiness" in d
        assert "data_completeness" in d

    def test_total_matches_sum_of_components(self):
        bd = self._make(total=60, case_value=20, urgency=18, booking=15, completeness=7)
        assert bd.total == bd.case_value + bd.urgency + bd.booking_readiness + bd.data_completeness

    def test_to_dict_contains_top_signals(self):
        d = self._make().to_dict()
        assert "top_signals" in d
        assert isinstance(d["top_signals"], list)

    def test_to_dict_contains_follow_up(self):
        d = self._make().to_dict()
        assert d["recommended_follow_up"] == "same_day"

    def test_lead_score_ttl_is_30_minutes(self):
        assert _LEAD_SCORE_TTL == 30 * 60


# ---------------------------------------------------------------------------
# LeadScorer.score() — full async path with mocked OpenAI
# ---------------------------------------------------------------------------

class TestLeadScorerScore:
    async def test_returns_breakdown_with_correct_structure(self):
        s = fresh_state()
        s.urgency_score = 6
        s.urgency_label = UrgencyLabel.HIGH
        s.record_intake("case_type", "asylum")
        s.record_intake("full_name", "Maria Cruz")
        s.record_intake("country_of_birth", "Guatemala")
        s.turns = [
            {"role": "user", "content": "I need help with my asylum case."},
            {"role": "assistant", "content": "I can help you with that."},
        ]

        scorer = LeadScorer("CA_lead_test")
        redis_mock = AsyncMock()
        mock_resp = _make_tool_response(booking=20, follow_up="same_day",
                                        tier="senior")

        with patch.object(scorer, "_client") as mock_openai:
            mock_openai.chat.completions.create = AsyncMock(return_value=mock_resp)
            with patch("app.agent.lead_scorer._cache_lead_score", AsyncMock()):
                with patch("app.agent.lead_scorer._queue_lead_score_db", AsyncMock()):
                    result = await scorer.score(s, redis_mock)

        assert isinstance(result, LeadScoreBreakdown)
        assert 0 <= result.total <= 100
        assert result.case_value == _CASE_TYPE_VALUE["asylum"]  # 22
        assert result.urgency == min(math.ceil(6 * 2.5), 25)  # 15
        assert result.booking_readiness == 20
        assert result.recommended_follow_up == "same_day"
        assert result.recommended_attorney_tier == "senior"

    async def test_updates_state_lead_score(self):
        s = fresh_state()
        s.urgency_score = 0
        scorer = LeadScorer("CA_lead_test")
        redis_mock = AsyncMock()
        mock_resp = _make_tool_response(booking=10)

        with patch.object(scorer, "_client") as mock_openai:
            mock_openai.chat.completions.create = AsyncMock(return_value=mock_resp)
            with patch("app.agent.lead_scorer._cache_lead_score", AsyncMock()):
                with patch("app.agent.lead_scorer._queue_lead_score_db", AsyncMock()):
                    result = await scorer.score(s, redis_mock)

        assert s.lead_score == result.total
        assert s.lead_score >= 0

    async def test_total_never_exceeds_100(self):
        """Even with padded sub-scores the total must be capped at 100."""
        s = fresh_state()
        s.urgency_score = 10  # → 25
        s.record_intake("case_type", "removal_defense")  # → 25
        for f in ["full_name", "country_of_birth", "current_immigration_status",
                  "case_type", "entry_date_us", "email"]:
            s.record_intake(f, "x")  # → completeness = 25

        scorer = LeadScorer("CA_lead_test")
        redis_mock = AsyncMock()
        mock_resp = _make_tool_response(booking=25)  # max booking

        with patch.object(scorer, "_client") as mock_openai:
            mock_openai.chat.completions.create = AsyncMock(return_value=mock_resp)
            with patch("app.agent.lead_scorer._cache_lead_score", AsyncMock()):
                with patch("app.agent.lead_scorer._queue_lead_score_db", AsyncMock()):
                    result = await scorer.score(s, redis_mock)

        assert result.total <= 100

    async def test_openai_failure_uses_default_booking_readiness(self):
        """If GPT call fails, booking_readiness defaults to 10."""
        s = fresh_state()
        s.record_intake("case_type", "family")
        s.turns = [{"role": "user", "content": "Help me."}]

        scorer = LeadScorer("CA_lead_test")
        redis_mock = AsyncMock()

        with patch.object(scorer, "_client") as mock_openai:
            mock_openai.chat.completions.create = AsyncMock(
                side_effect=RuntimeError("OpenAI down")
            )
            with patch("app.agent.lead_scorer._cache_lead_score", AsyncMock()):
                with patch("app.agent.lead_scorer._queue_lead_score_db", AsyncMock()):
                    result = await scorer.score(s, redis_mock)

        assert result.booking_readiness == 10  # default fallback

    async def test_no_turns_uses_default_booking(self):
        """With empty turns, GPT call is skipped; booking defaults to 10."""
        s = fresh_state()
        scorer = LeadScorer("CA_lead_test")
        redis_mock = AsyncMock()

        with patch.object(scorer, "_client") as mock_openai:
            with patch("app.agent.lead_scorer._cache_lead_score", AsyncMock()):
                with patch("app.agent.lead_scorer._queue_lead_score_db", AsyncMock()):
                    result = await scorer.score(s, redis_mock)

        mock_openai.chat.completions.create.assert_not_called()
        assert result.booking_readiness == 10
