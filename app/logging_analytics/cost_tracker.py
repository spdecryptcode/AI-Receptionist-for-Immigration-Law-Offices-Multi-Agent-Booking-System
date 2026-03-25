"""
Per-call cost tracker.

Accumulates token and audio usage across a single call and writes a
total cost estimate to the `call_logs.cost_usd` column at call end.

Pricing constants (as of early 2026 — update when rates change):
  Deepgram nova-3:  $0.0043 / minute
  OpenAI GPT-4o:    $2.50 / 1M input tokens,  $10.00 / 1M output tokens
  ElevenLabs:       $0.30 / 1K characters (Flash v2.5)
  Twilio Media:     $0.0085 / minute (call leg, billed separately by Twilio account)

Usage:
    tracker = CallCostTracker(call_sid)
    tracker.add_deepgram_seconds(18.4)
    tracker.add_openai_tokens(input=320, output=95)
    tracker.add_elevenlabs_chars(210)
    total = tracker.total_usd()
    await tracker.persist(redis)      # writes to db_sync_queue
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

# ─── Pricing (USD) ───────────────────────────────────────────────────────────
_DG_COST_PER_SEC = 0.0043 / 60          # $0.0043/min
_OAI_INPUT_COST_PER_TOKEN = 2.50 / 1e6  # $2.50/1M
_OAI_OUTPUT_COST_PER_TOKEN = 10.0 / 1e6 # $10.00/1M
_EL_COST_PER_CHAR = 0.30 / 1000         # $0.30/1K chars


class CallCostTracker:
    """
    Lightweight accumulator — one instance per call, held on `CallSession`.
    Thread-safe only within asyncio (no locking needed).
    """

    __slots__ = (
        "call_sid",
        "_deepgram_seconds",
        "_openai_input_tokens",
        "_openai_output_tokens",
        "_elevenlabs_chars",
    )

    def __init__(self, call_sid: str) -> None:
        self.call_sid = call_sid
        self._deepgram_seconds: float = 0.0
        self._openai_input_tokens: int = 0
        self._openai_output_tokens: int = 0
        self._elevenlabs_chars: int = 0

    # ── Accumulator methods ───────────────────────────────────────────────

    def add_deepgram_seconds(self, seconds: float) -> None:
        self._deepgram_seconds += max(0.0, seconds)

    def add_openai_tokens(self, input: int = 0, output: int = 0) -> None:
        self._openai_input_tokens += max(0, input)
        self._openai_output_tokens += max(0, output)

    def add_elevenlabs_chars(self, chars: int) -> None:
        self._elevenlabs_chars += max(0, chars)

    # ── Cost calculation ─────────────────────────────────────────────────

    def deepgram_usd(self) -> float:
        return self._deepgram_seconds * _DG_COST_PER_SEC

    def openai_usd(self) -> float:
        return (
            self._openai_input_tokens * _OAI_INPUT_COST_PER_TOKEN
            + self._openai_output_tokens * _OAI_OUTPUT_COST_PER_TOKEN
        )

    def elevenlabs_usd(self) -> float:
        return self._elevenlabs_chars * _EL_COST_PER_CHAR

    def total_usd(self) -> float:
        return round(self.deepgram_usd() + self.openai_usd() + self.elevenlabs_usd(), 6)

    def breakdown(self) -> dict:
        return {
            "deepgram_seconds": round(self._deepgram_seconds, 2),
            "openai_input_tokens": self._openai_input_tokens,
            "openai_output_tokens": self._openai_output_tokens,
            "elevenlabs_chars": self._elevenlabs_chars,
            "deepgram_usd": round(self.deepgram_usd(), 6),
            "openai_usd": round(self.openai_usd(), 6),
            "elevenlabs_usd": round(self.elevenlabs_usd(), 6),
            "total_usd": self.total_usd(),
        }

    # ── Persistence ───────────────────────────────────────────────────────

    async def persist(self, redis) -> None:
        """
        Push a cost update payload to `db_sync_queue`.
        db_worker will UPDATE call_logs SET cost_usd = ... WHERE call_sid = ...
        """
        payload = json.dumps({
            "type": "call_cost",
            "call_sid": self.call_sid,
            "cost_usd": self.total_usd(),
            "breakdown": self.breakdown(),
        })
        try:
            await redis.rpush("db_sync_queue", payload)
            logger.info(
                f"[{self.call_sid}] Cost persisted: ${self.total_usd():.5f} "
                f"(dg={self._deepgram_seconds:.1f}s "
                f"oai={self._openai_input_tokens}+{self._openai_output_tokens}t "
                f"el={self._elevenlabs_chars}c)"
            )
        except Exception as exc:
            logger.error(f"[{self.call_sid}] Cost persist failed: {exc}")
