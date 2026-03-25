"""
Sliding-window context manager for multi-turn LLM conversations.

Design:
  - Keep the last HISTORY_MAX_TURNS turns verbatim in CallState.turns
  - When turn count exceeds the limit, summarize the oldest half via GPT-4o
  - Summary is prepended as a system-level context message to every LLM call
  - Hard token cap: ~2000 tokens total for combined summary + recent turns
    (OpenAI context limit is 128k, but we keep it tight for cost + latency)

Summary prompt:
  We use a slim 40-token "summarize" system message so it does NOT consume the
  prompt-cache slot of the main immigration system prompt.
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from app.config import settings
from app.dependencies import get_openai_client

if TYPE_CHECKING:
    from app.voice.conversation_state import CallState

logger = logging.getLogger(__name__)

# Number of most-recent turns to keep verbatim
HISTORY_MAX_TURNS = 6

# When we exceed HISTORY_MAX_TURNS, compress the oldest N turns into summary
_TURNS_TO_COMPRESS = 4

# Soft cap on summary characters (roughly 500 tokens)
_SUMMARY_MAX_CHARS = 2000


class ContextManager:
    """
    Manages the conversation history window for a single call.

    Usage:
        ctx = ContextManager(state)
        await ctx.add_turn("user", transcript)
        await ctx.add_turn("assistant", response)
        messages = ctx.build_messages(system_prompt)
    """

    def __init__(self, state: "CallState"):
        self._state = state

    async def add_turn(self, role: str, content: str) -> None:
        """
        Append a turn to the history. If the window is full, compress oldest turns.
        Mutates state.turns and state.summary in place (caller must save to Redis).
        """
        self._state.turns.append({"role": role, "content": content})

        if len(self._state.turns) > HISTORY_MAX_TURNS:
            await self._compress_old_turns()

    async def _compress_old_turns(self) -> None:
        """
        Summarize the N oldest turns and merge into state.summary.
        Removes those turns from state.turns.
        """
        to_compress = self._state.turns[:_TURNS_TO_COMPRESS]
        self._state.turns = self._state.turns[_TURNS_TO_COMPRESS:]

        try:
            new_fragment = await self._summarize(to_compress)
            if self._state.summary:
                # Merge old summary with the new fragment — keep to char cap
                merged = f"{self._state.summary}\n{new_fragment}"
                if len(merged) > _SUMMARY_MAX_CHARS:
                    # Truncate from the front to stay within budget
                    merged = "...(earlier context omitted)...\n" + merged[-_SUMMARY_MAX_CHARS:]
                self._state.summary = merged
            else:
                self._state.summary = new_fragment
            logger.debug(
                f"[{self._state.call_sid}] Context compressed. "
                f"Summary len={len(self._state.summary)} recent_turns={len(self._state.turns)}"
            )
        except Exception as exc:
            logger.warning(
                f"[{self._state.call_sid}] Context compression failed: {exc} — "
                "keeping turns verbatim (history will grow)"
            )
            # Don't discard turns — just restore them and continue
            self._state.turns = to_compress + self._state.turns

    async def _summarize(self, turns: list[dict[str, str]]) -> str:
        """Call GPT-4o to produce a compact summary of the given turns."""
        client = get_openai_client()

        # Format turns as readable dialogue
        dialogue = "\n".join(
            f"{'Caller' if t['role'] == 'user' else 'Agent'}: {t['content']}"
            for t in turns
        )

        messages = [
            {
                "role": "system",
                "content": (
                    "Summarize the following immigration call dialogue in 3-5 sentences. "
                    "Focus on: caller's situation, case type, urgency, and any key facts collected. "
                    "Be concise. Output plain text only."
                ),
            },
            {"role": "user", "content": dialogue},
        ]

        resp = await client.chat.completions.create(
            model=settings.openai_model,
            messages=messages,
            max_tokens=150,
            temperature=0.2,
        )
        return resp.choices[0].message.content.strip()

    def get_full_history(self) -> list[dict]:
        """
        Return the complete conversation history available in the current window.
        Note: turns older than HISTORY_MAX_TURNS are compressed into state.summary
        and are not recoverable verbatim. This returns the verbatim window only.
        """
        return list(self._state.turns)

    def build_messages(
        self,
        system_prompt: str,
        extra_context: str = "",
    ) -> list[dict[str, str]]:
        """
        Build the `messages` list to send to OpenAI.

        Structure:
          [0] system: main immigration system prompt (static → prompt caching)
          [1] system: context injection (summary + intake so far + extra hints)
          [2..N] user/assistant turns (verbatim recent history)
        """
        messages: list[dict[str, str]] = [
            {"role": "system", "content": system_prompt},
        ]

        # Context injection (separate system message to avoid polluting cache)
        context_parts: list[str] = []
        if self._state.summary:
            context_parts.append(f"[Earlier conversation summary]\n{self._state.summary}")
        if self._state.intake:
            collected = ", ".join(
                f"{k}={v!r}" for k, v in self._state.intake.items() if v
            )
            context_parts.append(f"[Intake collected so far]\n{collected}")
        if self._state.urgency_score >= 6:
            context_parts.append(
                f"[Urgency]\nScore={self._state.urgency_score} "
                f"label={self._state.urgency_label.value} — handle with priority."
            )
        if extra_context:
            context_parts.append(extra_context)

        if context_parts:
            messages.append({
                "role": "system",
                "content": "\n\n".join(context_parts),
            })

        # Verbatim recent turns
        messages.extend(self._state.turns)

        return messages

    @property
    def recent_turns(self) -> list[dict[str, str]]:
        return list(self._state.turns)

    @property
    def has_summary(self) -> bool:
        return bool(self._state.summary)
