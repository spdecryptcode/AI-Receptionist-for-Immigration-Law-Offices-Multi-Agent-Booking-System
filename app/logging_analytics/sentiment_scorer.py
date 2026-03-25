"""
Sentiment scoring for completed calls.

GPT-4o function-calling approach:
  - Input: last N turns of conversation
  - Output: JSON with score (-1.0 to 1.0), label, frustration flag,
    specific frustration triggers, and intake gap list

The score is stored on the `call_logs` row and surfaced in the dashboard.
Frustration detection triggers a GHL tag so the attorney knows to be
especially empathetic when reviewing notes.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)

_SENTIMENT_TOOL: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "record_sentiment",
            "description": "Record sentiment analysis results for an immigration intake call.",
            "parameters": {
                "type": "object",
                "properties": {
                    "overall_score": {
                        "type": "number",
                        "description": (
                            "Sentiment score from -1.0 (very negative) to 1.0 (very positive). "
                            "0 = neutral."
                        ),
                    },
                    "label": {
                        "type": "string",
                        "enum": ["positive", "neutral", "negative", "mixed"],
                        "description": "Human-readable sentiment label.",
                    },
                    "frustration_detected": {
                        "type": "boolean",
                        "description": (
                            "True if the caller expressed clear frustration, anxiety, "
                            "fear, or distress during the call."
                        ),
                    },
                    "frustration_triggers": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Specific topics or moments that caused frustration "
                            "(e.g., 'wait times', 'cost', 'prior attorney', 'ICE raid')."
                        ),
                    },
                    "caller_confidence": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                        "description": "How confident the caller seemed in providing information.",
                    },
                    "intake_gaps": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Fields that were asked but never answered "
                            "(caller refused, topic changed, call ended early)."
                        ),
                    },
                    "coaching_note": {
                        "type": "string",
                        "description": (
                            "One-sentence coaching tip for the attorney reviewing this call "
                            "(e.g., 'Caller was anxious about deportation — lead with reassurance')."
                        ),
                    },
                },
                "required": [
                    "overall_score",
                    "label",
                    "frustration_detected",
                    "frustration_triggers",
                    "caller_confidence",
                    "intake_gaps",
                    "coaching_note",
                ],
            },
        },
    }
]


# ─── Public API ───────────────────────────────────────────────────────────────

async def score_conversation(call_sid: str, conversation: list[dict]) -> dict:
    """
    Analyse caller sentiment from the conversation history.

    Returns a dict matching the `record_sentiment` function schema.
    Safe to call even on short conversations; returns neutral defaults on error.
    """
    if not conversation:
        return _neutral_defaults()

    # Use last 20 turns to keep token count manageable
    turns_text = "\n".join(
        f"{m['role'].upper()}: {m['content']}" for m in conversation[-20:]
    )
    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=settings.openai_api_key)
        resp = await client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are an expert legal call-quality analyst. "
                        "Analyse the caller's emotional state and communication patterns "
                        "in this immigration intake call. Be objective and specific."
                    ),
                },
                {"role": "user", "content": turns_text},
            ],
            tools=_SENTIMENT_TOOL,
            tool_choice={"type": "function", "function": {"name": "record_sentiment"}},
            max_tokens=300,
            temperature=0.1,
        )
        tool_call = resp.choices[0].message.tool_calls
        if tool_call:
            result = json.loads(tool_call[0].function.arguments)
            logger.info(
                f"[{call_sid}] Sentiment: {result.get('label')} "
                f"score={result.get('overall_score')} "
                f"frustration={result.get('frustration_detected')}"
            )
            return result
    except Exception as exc:
        logger.error(f"[{call_sid}] Sentiment scoring error: {exc}")

    return _neutral_defaults()


def _neutral_defaults() -> dict:
    return {
        "overall_score": 0.0,
        "label": "neutral",
        "frustration_detected": False,
        "frustration_triggers": [],
        "caller_confidence": "medium",
        "intake_gaps": [],
        "coaching_note": "",
    }
