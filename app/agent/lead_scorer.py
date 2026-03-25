"""
Post-call lead scorer — runs after call ends, not during the call.

Produces a 0-100 lead score with a detailed breakdown that feeds into:
  - GHL contact priority tags
  - Attorney queue ordering
  - Follow-up scheduling urgency

Score is written to:
  - Redis cache: `lead_score:{call_sid}` (30 min TTL)
  - lead_scores table via DB worker queue
  - GHL contact tags (via CRM update queue)

Scoring dimensions:
  - Case complexity / value (0-25): type of case, prior filings
  - Urgency (0-25): from urgency_classifier result
  - Readiness to book (0-25): asked for consult, time sensitivity
  - Data completeness (0-25): how many intake fields were captured

GPT-4o is used to evaluate the unstructured signals in the conversation;
the four dimension scores are deterministic calculations on top of known data.
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, asdict

from app.config import settings
from app.dependencies import get_openai_client
from app.voice.conversation_state import CallState, UrgencyLabel

logger = logging.getLogger(__name__)

# Redis TTL for cached lead score (30 minutes)
_LEAD_SCORE_TTL = 30 * 60


# ---------------------------------------------------------------------------
# Score breakdown dataclass
# ---------------------------------------------------------------------------

@dataclass
class LeadScoreBreakdown:
    total: int  # 0-100
    case_value: int  # 0-25: complexity/type
    urgency: int  # 0-25: mirrors urgency score
    booking_readiness: int  # 0-25: bought signals
    data_completeness: int  # 0-25
    # Qualitative signals from GPT
    top_signals: list[str]
    recommended_follow_up: str  # "immediate" | "same_day" | "next_day" | "this_week"
    recommended_attorney_tier: str  # "senior" | "associate" | "paralegal"
    notes: str

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


# ---------------------------------------------------------------------------
# Case type value map
# ---------------------------------------------------------------------------

# Higher = more complex / higher value case for the firm
_CASE_TYPE_VALUE: dict[str, int] = {
    "removal_defense": 25,
    "asylum": 22,
    "employment": 20,
    "family": 18,
    "daca": 15,
    "tps": 12,
    "citizenship": 10,
    "other": 8,
    "unknown": 5,
}


# ---------------------------------------------------------------------------
# OpenAI function tool for qualitative signals
# ---------------------------------------------------------------------------

_SCORING_TOOL = {
    "type": "function",
    "function": {
        "name": "score_lead",
        "description": "Evaluate lead quality signals from an immigration intake conversation.",
        "parameters": {
            "type": "object",
            "properties": {
                "booking_readiness_score": {
                    "type": "integer",
                    "description": (
                        "0-25: How ready is the caller to book a consultation? "
                        "25 = explicitly asked to book or mentioned urgency + budget. "
                        "0 = just browsing, no commitment signals."
                    ),
                    "minimum": 0,
                    "maximum": 25,
                },
                "top_signals": {
                    "type": "array",
                    "description": "Up to 5 key signals that influence lead quality (positive or negative).",
                    "items": {"type": "string"},
                    "maxItems": 5,
                },
                "recommended_follow_up": {
                    "type": "string",
                    "enum": ["immediate", "same_day", "next_day", "this_week"],
                    "description": "Recommended follow-up urgency.",
                },
                "recommended_attorney_tier": {
                    "type": "string",
                    "enum": ["senior", "associate", "paralegal"],
                    "description": (
                        "senior: complex/urgent/high-value. "
                        "associate: standard cases. "
                        "paralegal: admin/status-only queries."
                    ),
                },
                "notes": {
                    "type": "string",
                    "description": "One-sentence summary for the attorney reviewing this lead.",
                    "maxLength": 200,
                },
            },
            "required": [
                "booking_readiness_score",
                "top_signals",
                "recommended_follow_up",
                "recommended_attorney_tier",
                "notes",
            ],
        },
    },
}


# ---------------------------------------------------------------------------
# Lead Scorer
# ---------------------------------------------------------------------------

class LeadScorer:
    """
    Computes post-call lead score from CallState.

    Called from _finalize_call() after the call ends. Never called during
    an active call as it adds latency.
    """

    def __init__(self, call_sid: str):
        self.call_sid = call_sid
        self._client = get_openai_client()

    async def score(self, state: CallState, redis) -> LeadScoreBreakdown:
        """
        Compute the lead score. Writes result to Redis and DB queue.
        Returns the LeadScoreBreakdown.
        """
        # ---------------------------------------------------------------
        # Deterministic sub-scores
        # ---------------------------------------------------------------

        # 1. Case value (from case_type)
        case_type_raw = str(state.intake.get("case_type", "unknown")).lower()
        case_type_key = "unknown"
        for key in _CASE_TYPE_VALUE:
            if key in case_type_raw:
                case_type_key = key
                break
        case_value_score = _CASE_TYPE_VALUE.get(case_type_key, 5)

        # 2. Urgency (map 0-10 → 0-25)
        urgency_score_25 = math.ceil(state.urgency_score * 2.5)
        urgency_score_25 = min(urgency_score_25, 25)

        # 3. Data completeness
        total_fields = len(state.intake)
        completeness_pct = total_fields / max(len([
            "full_name", "country_of_birth", "current_immigration_status",
            "case_type", "entry_date_us", "email",
        ]), 1)
        data_completeness = min(int(completeness_pct * 25), 25)

        # ---------------------------------------------------------------
        # GPT-4o: booking readiness + qualitative signals
        # ---------------------------------------------------------------
        booking_readiness = 10  # default
        top_signals: list[str] = []
        recommended_follow_up = "next_day"
        recommended_attorney_tier = "associate"
        notes = ""

        try:
            if state.turns:
                dialogue = "\n".join(
                    f"{'Caller' if t['role'] == 'user' else 'Agent'}: {t['content']}"
                    for t in state.turns[-12:]
                )
                if state.summary:
                    dialogue = f"[Summary of earlier conversation]\n{state.summary}\n\n{dialogue}"

                intake_str = json.dumps(state.intake, default=str)

                messages = [
                    {
                        "role": "system",
                        "content": (
                            "You are an expert immigration law firm intake analyst. "
                            "Score lead quality from the intake conversation below."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Intake data collected: {intake_str}\n\n"
                            f"Conversation:\n{dialogue}"
                        ),
                    },
                ]

                resp = await self._client.chat.completions.create(
                    model=settings.openai_model,
                    messages=messages,
                    tools=[_SCORING_TOOL],
                    tool_choice={"type": "function", "function": {"name": "score_lead"}},
                    max_tokens=300,
                    temperature=0.1,
                )

                tc = resp.choices[0].message.tool_calls
                if tc:
                    result = json.loads(tc[0].function.arguments)
                    booking_readiness = int(result.get("booking_readiness_score", 10))
                    top_signals = result.get("top_signals", [])
                    recommended_follow_up = result.get("recommended_follow_up", "next_day")
                    recommended_attorney_tier = result.get("recommended_attorney_tier", "associate")
                    notes = result.get("notes", "")

        except Exception as exc:
            logger.error(f"[{self.call_sid}] LeadScorer GPT call failed: {exc}", exc_info=True)

        # ---------------------------------------------------------------
        # Compose total
        # ---------------------------------------------------------------
        total = case_value_score + urgency_score_25 + booking_readiness + data_completeness
        total = min(max(total, 0), 100)

        breakdown = LeadScoreBreakdown(
            total=total,
            case_value=case_value_score,
            urgency=urgency_score_25,
            booking_readiness=booking_readiness,
            data_completeness=data_completeness,
            top_signals=top_signals,
            recommended_follow_up=recommended_follow_up,
            recommended_attorney_tier=recommended_attorney_tier,
            notes=notes,
        )

        # ---------------------------------------------------------------
        # Persist
        # ---------------------------------------------------------------
        state.lead_score = total
        await _cache_lead_score(self.call_sid, breakdown, redis)
        await _queue_lead_score_db(self.call_sid, breakdown, redis)

        logger.info(
            f"[{self.call_sid}] Lead score: {total}/100 "
            f"(case={case_value_score} urgency={urgency_score_25} "
            f"booking={booking_readiness} completeness={data_completeness})"
        )

        return breakdown


async def _cache_lead_score(call_sid: str, breakdown: LeadScoreBreakdown, redis) -> None:
    key = f"lead_score:{call_sid}"
    try:
        await redis.setex(key, _LEAD_SCORE_TTL, json.dumps(breakdown.to_dict()))
    except Exception as exc:
        logger.warning(f"[{call_sid}] Failed to cache lead score: {exc}")


async def _queue_lead_score_db(call_sid: str, breakdown: LeadScoreBreakdown, redis) -> None:
    """Push to DB worker queue for async persistence to lead_scores table."""
    payload = {"call_sid": call_sid, **breakdown.to_dict()}
    try:
        await redis.rpush("lead_score_queue", json.dumps(payload, default=str))
    except Exception as exc:
        logger.warning(f"[{call_sid}] Failed to queue lead score for DB: {exc}")
