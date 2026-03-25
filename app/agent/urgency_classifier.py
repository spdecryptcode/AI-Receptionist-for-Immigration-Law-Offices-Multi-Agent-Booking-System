"""
Urgency classifier — runs in parallel with TTS to score caller urgency.

Uses GPT-4o function calling (tools API) to extract a structured urgency
assessment from the conversation so far. The result is stored in CallState
and influences:
  - Phase transitions (EMERGENCY forces immediate transfer path)
  - Which intake questions are shown (criminal history, deportation history)
  - Lead prioritisation (urgency feeds into lead_scorer)
  - SLA alerting (HIGH/EMERGENCY creates an UrgencyAlert DB row)

Performance:
  The classifier is launched as asyncio.create_task() while TTS is playing,
  so it runs concurrently and typically completes before the next caller turn.
  It should never block the main pipeline loop.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

from app.config import settings
from app.dependencies import get_openai_client
from app.voice.conversation_state import CallState, UrgencyLabel, score_to_urgency_label, save_call_state

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OpenAI function tool definition
# ---------------------------------------------------------------------------

_URGENCY_TOOL = {
    "type": "function",
    "function": {
        "name": "score_urgency",
        "description": (
            "Evaluate the urgency of an immigration caller's situation based on the conversation so far."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "urgency_score": {
                    "type": "integer",
                    "description": (
                        "Urgency score from 0 (routine inquiry) to 10 (life-safety emergency). "
                        "Use 9-10 for detained family members, imminent deportation, "
                        "domestic violence / trafficking. Use 6-8 for pending hearings <30 days, "
                        "expired status, removal orders. Use 3-5 for active cases, renewals. "
                        "Use 0-2 for purely informational calls."
                    ),
                    "minimum": 0,
                    "maximum": 10,
                },
                "urgency_factors": {
                    "type": "array",
                    "description": "Short list of specific urgency factors detected (max 5).",
                    "items": {"type": "string"},
                    "maxItems": 5,
                },
                "recommended_action": {
                    "type": "string",
                    "enum": [
                        "routine_intake",
                        "expedite_consultation",
                        "immediate_attorney_callback",
                        "emergency_transfer",
                    ],
                    "description": "Recommended next action based on urgency.",
                },
                "detected_case_type": {
                    "type": "string",
                    "description": (
                        "Best-guess case type from conversation: asylum | removal_defense | "
                        "family | employment | citizenship | daca | tps | other | unknown"
                    ),
                },
            },
            "required": ["urgency_score", "urgency_factors", "recommended_action", "detected_case_type"],
        },
    },
}


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------

class UrgencyClassifier:
    """
    Runs urgency classification on a CallState's conversation turns.

    Designed to be fire-and-forget (asyncio.create_task) — the result is
    written back into the CallState and persisted to Redis.
    """

    def __init__(self, call_sid: str):
        self.call_sid = call_sid
        self._client = get_openai_client()

    async def classify(self, state: CallState, redis) -> None:
        """
        Assess urgency and update state.urgency_score / urgency_label in place.
        Also saves state to Redis on completion.

        Should be called after at least 2 turns (URGENCY_TRIAGE phase).
        """
        if len(state.turns) < 2:
            return  # Not enough context

        # Build dialogue snippet for classification (last 10 turns max)
        relevant_turns = state.turns[-10:]
        dialogue = "\n".join(
            f"{'Caller' if t['role'] == 'user' else 'Agent'}: {t['content']}"
            for t in relevant_turns
        )

        if state.summary:
            dialogue = f"[Earlier summary]\n{state.summary}\n\n{dialogue}"

        messages = [
            {
                "role": "system",
                "content": (
                    "You are an expert immigration intake analyst. "
                    "Assess the urgency of the caller's situation based on the conversation below."
                ),
            },
            {"role": "user", "content": dialogue},
        ]

        try:
            response = await self._client.chat.completions.create(
                model=settings.openai_model,
                messages=messages,
                tools=[_URGENCY_TOOL],
                tool_choice={"type": "function", "function": {"name": "score_urgency"}},
                max_tokens=200,
                temperature=0.1,
            )

            tool_call = response.choices[0].message.tool_calls
            if not tool_call:
                logger.warning(f"[{self.call_sid}] Urgency classifier: no tool call in response")
                return

            result = json.loads(tool_call[0].function.arguments)
            score = int(result.get("urgency_score", 0))
            label = score_to_urgency_label(score)
            factors = result.get("urgency_factors", [])
            action = result.get("recommended_action", "routine_intake")
            case_type_hint = result.get("detected_case_type", "unknown")

            # Update state
            state.urgency_score = score
            state.urgency_label = label

            # Opportunistically fill case_type if not already set
            if not state.intake.get("case_type") and case_type_hint not in ("unknown", "other"):
                state.record_intake("case_type", case_type_hint)

            await save_call_state(state, redis)

            logger.info(
                f"[{self.call_sid}] Urgency classified: score={score} label={label.value} "
                f"action={action} factors={factors}"
            )

            # Queue SLA alert for high/emergency urgency
            if label in (UrgencyLabel.HIGH, UrgencyLabel.EMERGENCY):
                await _queue_urgency_alert(
                    call_sid=self.call_sid,
                    score=score,
                    label=label.value,
                    factors=factors,
                    action=action,
                    redis=redis,
                )

        except Exception as exc:
            logger.error(
                f"[{self.call_sid}] Urgency classification failed: {exc}",
                exc_info=True,
            )


async def _queue_urgency_alert(
    call_sid: str,
    score: int,
    label: str,
    factors: list[str],
    action: str,
    redis,
) -> None:
    """
    Push an urgency alert to a Redis list for the background DB worker to persist.
    Key: urgency_alerts (list), each element is a JSON blob.
    """
    alert = {
        "call_sid": call_sid,
        "urgency_score": score,
        "urgency_label": label,
        "factors": factors,
        "recommended_action": action,
    }
    try:
        await redis.rpush("urgency_alerts", json.dumps(alert))
        logger.info(f"[{call_sid}] Urgency alert queued for DB persistence")
    except Exception as exc:
        logger.error(f"[{call_sid}] Failed to queue urgency alert: {exc}")


def create_urgency_task(state: CallState, redis) -> asyncio.Task:
    """
    Launch urgency classification as a background asyncio.Task.
    The task writes results back into `state` (shared object) and saves to Redis.

    Usage:
        urgency_task = create_urgency_task(state, redis)
        # ... pipeline continues while classifier runs concurrently ...
    """
    classifier = UrgencyClassifier(state.call_sid)
    task = asyncio.create_task(
        classifier.classify(state, redis),
        name=f"urgency_classify:{state.call_sid}",
    )
    return task
