"""
Conversation state machine — persisted in Redis.

Each call has a `CallState` stored as a Redis hash at key `conversation:{call_sid}`.
The FSM defines valid phase transitions and tracks which intake fields have been
collected, enabling the intake_flow to skip fields already answered.

State key layout (Redis hash `conversation:{call_sid}`):
  phase           — current ConversationPhase enum value
  language        — "en" | "es"
  turns           — JSON-encoded list of {role, content} for last N turns
  summary         — running GPT-4 summary of older turns (once > HISTORY_MAX_TURNS)
  intake_*        — individual intake field values as they are collected
  transferred_at  — ISO timestamp if emergency-transferred
  scheduled_at    — ISO timestamp if appointment booked
  urgency_score   — int 0-10 from urgency classifier
  urgency_label   — "low" | "medium" | "high" | "emergency"
  lead_score      — int 0-100 from lead scorer (post-call)
  last_updated    — float epoch
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.agent.llm_agent import ConversationPhase

logger = logging.getLogger(__name__)

# Redis TTL for conversation state — 48 h post-call
_CONV_TTL_SECONDS = 48 * 3600

# Intake fields we actively collect (in rough order of priority)
INTAKE_FIELDS: list[str] = [
    "full_name",
    "date_of_birth",
    "country_of_birth",
    "nationality",
    "current_immigration_status",
    "case_type",
    "entry_date_us",
    "prior_applications",
    "has_attorney",
    "urgency_reason",
    "preferred_language",
    "preferred_contact_time",
    "email",
    "address",
    "employer_sponsor",
    "family_in_us",
    "prior_deportation",
    "criminal_history",
]

# Phases that allow forward transitions
_PHASE_ORDER: list[ConversationPhase] = [
    ConversationPhase.GREETING,
    ConversationPhase.IDENTIFICATION,
    ConversationPhase.URGENCY_TRIAGE,
    ConversationPhase.INTAKE,
    ConversationPhase.CONSULTATION_PITCH,
    ConversationPhase.BOOKING,
    ConversationPhase.CONFIRMATION,
    ConversationPhase.CLOSING,
]

# Minimum turns in each phase before we allow advancing
_MIN_TURNS_PER_PHASE: dict[ConversationPhase, int] = {
    ConversationPhase.GREETING: 1,
    ConversationPhase.IDENTIFICATION: 1,
    ConversationPhase.URGENCY_TRIAGE: 1,
    ConversationPhase.INTAKE: 3,
    ConversationPhase.CONSULTATION_PITCH: 1,
    ConversationPhase.BOOKING: 1,
    ConversationPhase.CONFIRMATION: 1,
    ConversationPhase.CLOSING: 0,
}


# ---------------------------------------------------------------------------
# Urgency label helper
# ---------------------------------------------------------------------------

class UrgencyLabel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    EMERGENCY = "emergency"


def score_to_urgency_label(score: int) -> UrgencyLabel:
    if score >= 9:
        return UrgencyLabel.EMERGENCY
    if score >= 6:
        return UrgencyLabel.HIGH
    if score >= 3:
        return UrgencyLabel.MEDIUM
    return UrgencyLabel.LOW


# ---------------------------------------------------------------------------
# In-memory state object (loaded from / saved to Redis)
# ---------------------------------------------------------------------------

@dataclass
class CallState:
    """Mutable conversation state for a single call. Sync with Redis via save/load."""

    call_sid: str
    phase: ConversationPhase = ConversationPhase.GREETING
    language: str = "en"
    # Recent turns kept verbatim (last HISTORY_MAX_TURNS)
    turns: list[dict[str, str]] = field(default_factory=list)
    # Running summary of older turns (built by ContextManager)
    summary: str = ""
    # Inline intake data collected so far
    intake: dict[str, Any] = field(default_factory=dict)
    # Urgency
    urgency_score: int = 0
    urgency_label: UrgencyLabel = UrgencyLabel.LOW
    # Scheduling
    scheduled_at: str = ""
    appointment_id: str = ""
    # Transfer
    transferred_at: str = ""
    # Lead score (post-call)
    lead_score: int = -1
    # Phase turn counter (resets on phase advance)
    phase_turns: int = 0
    # Monotonically increasing total turn counter (never resets)
    turn_count: int = 0
    # Unix epoch of last update
    last_updated: float = field(default_factory=time.time)

    # -----------------------------------------------------------------------
    # Phase management
    # -----------------------------------------------------------------------

    def advance_phase(self) -> ConversationPhase | None:
        """
        Move to the next phase if minimum turn requirement is met.
        Returns the new phase, or None if already at the last phase or not ready.
        """
        min_turns = _MIN_TURNS_PER_PHASE.get(self.phase, 0)
        if self.phase_turns < min_turns:
            return None

        try:
            idx = _PHASE_ORDER.index(self.phase)
        except ValueError:
            return None

        if idx + 1 >= len(_PHASE_ORDER):
            return None

        self.phase = _PHASE_ORDER[idx + 1]
        self.phase_turns = 0
        logger.debug(f"[{self.call_sid}] Phase advanced to {self.phase}")
        return self.phase

    def force_phase(self, new_phase: ConversationPhase) -> None:
        """Jump directly to a phase (e.g., when urgency detected = EMERGENCY)."""
        if new_phase != self.phase:
            self.phase = new_phase
            self.phase_turns = 0
            logger.info(f"[{self.call_sid}] Phase forced → {new_phase}")

    def increment_turns(self) -> None:
        self.phase_turns += 1
        self.turn_count += 1
        self.last_updated = time.time()

    def record_intake(self, key: str, value: Any) -> None:
        """Store a collected intake field. Silently ignores unknown keys."""
        if key in INTAKE_FIELDS:
            self.intake[key] = value
            self.last_updated = time.time()

    def missing_intake_fields(self) -> list[str]:
        """Return fields not yet collected, in priority order."""
        return [f for f in INTAKE_FIELDS if f not in self.intake or not self.intake[f]]

    def intake_complete(self) -> bool:
        """True when all high-priority intake fields are filled."""
        critical = ["full_name", "country_of_birth", "current_immigration_status", "case_type"]
        return all(self.intake.get(f) for f in critical)

    # -----------------------------------------------------------------------
    # Redis serialisation
    # -----------------------------------------------------------------------

    def to_redis_mapping(self) -> dict[str, str]:
        """Flatten state to a Redis hash mapping (all string values)."""
        return {
            "phase": self.phase.value,
            "language": self.language,
            "turns": json.dumps(self.turns, ensure_ascii=False),
            "summary": self.summary,
            "intake": json.dumps(self.intake, ensure_ascii=False, default=str),
            "urgency_score": str(self.urgency_score),
            "urgency_label": self.urgency_label.value,
            "scheduled_at": self.scheduled_at,
            "appointment_id": self.appointment_id,
            "transferred_at": self.transferred_at,
            "lead_score": str(self.lead_score),
            "phase_turns": str(self.phase_turns),
            "turn_count": str(self.turn_count),
            "last_updated": str(self.last_updated),
        }

    @classmethod
    def from_redis_mapping(cls, call_sid: str, data: dict[str, str]) -> "CallState":
        """Reconstruct from Redis hash data."""
        state = cls(call_sid=call_sid)
        if not data:
            return state

        state.phase = ConversationPhase(data.get("phase", "greeting"))
        state.language = data.get("language", "en")
        state.turns = json.loads(data.get("turns", "[]"))
        state.summary = data.get("summary", "")
        state.intake = json.loads(data.get("intake", "{}"))
        state.urgency_score = int(data.get("urgency_score", 0))
        state.urgency_label = UrgencyLabel(data.get("urgency_label", "low"))
        state.scheduled_at = data.get("scheduled_at", "")
        state.appointment_id = data.get("appointment_id", "")
        state.transferred_at = data.get("transferred_at", "")
        state.lead_score = int(data.get("lead_score", -1))
        state.phase_turns = int(data.get("phase_turns", 0))
        state.turn_count = int(data.get("turn_count", 0))
        state.last_updated = float(data.get("last_updated", time.time()))
        return state


# ---------------------------------------------------------------------------
# Redis persistence helpers
# ---------------------------------------------------------------------------

async def load_call_state(call_sid: str, redis) -> CallState:
    """Load CallState from Redis. Returns a fresh state if not found."""
    data = await redis.hgetall(f"conversation:{call_sid}")
    return CallState.from_redis_mapping(call_sid, data)


async def save_call_state(state: CallState, redis, ttl: int = _CONV_TTL_SECONDS) -> None:
    """Persist CallState to Redis with TTL."""
    key = f"conversation:{state.call_sid}"
    pipe = redis.pipeline()
    pipe.hset(key, mapping=state.to_redis_mapping())
    pipe.expire(key, ttl)
    await pipe.execute()
