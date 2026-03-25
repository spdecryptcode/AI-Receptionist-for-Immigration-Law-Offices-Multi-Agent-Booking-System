"""
Per-call and per-turn logging.

Responsibilities:
  1. Log each conversation turn (user utterance + AI reply) to the
     `conversation_messages` table via the db_worker Redis queue.
     Buffered in Redis on DB failure; flushed at call end.

  2. Orchestrate the post-call analytics pipeline:
       AI summary  →  lead score  →  sentiment analysis  →
       structured data extraction  →  confirmation SMS  →  GHL sync

All public functions are async and meant to be called as fire-and-forget
`asyncio.create_task()` calls from websocket_handler.py.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional

import redis.asyncio as aioredis

from app.config import settings

logger = logging.getLogger(__name__)

_MSG_BUFFER_PREFIX = "msg_buffer:"   # key = msg_buffer:{call_sid}
_MSG_BUFFER_TTL = 600                # 10 min — safeguard against orphaned buffers


# ─── Per-turn logging ─────────────────────────────────────────────────────────

async def log_turn(
    call_sid: str,
    turn_index: int,
    role: str,          # "user" or "assistant"
    text: str,
    latency_ms: Optional[int] = None,
    phase: str = "",
    intent: str = "",
) -> None:
    """
    Queue one conversation turn for persistence to `conversation_messages`.

    First attempts to push directly to `db_sync_queue`.
    Falls back to `msg_buffer:{call_sid}` in Redis if the queue is unavailable.

    Non-blocking — failures are logged and swallowed.
    """
    payload = json.dumps({
        "type": "conversation_message",
        "call_sid": call_sid,
        "turn_index": turn_index,
        "role": role,
        "text": text[:4000],                   # guard against excessive length
        "latency_ms": latency_ms,
        "phase": phase,
        "intent": intent,
        "ts": _now_ms(),
    })
    try:
        redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
        async with redis_client:
            await redis_client.rpush("db_sync_queue", payload)
    except Exception:
        # Primary queue unavailable — buffer locally per-call
        await _buffer_turn(call_sid, payload)


async def _buffer_turn(call_sid: str, payload: str) -> None:
    """Push to per-call buffer when the primary db queue is unavailable."""
    try:
        redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
        async with redis_client:
            key = f"{_MSG_BUFFER_PREFIX}{call_sid}"
            await redis_client.rpush(key, payload)
            await redis_client.expire(key, _MSG_BUFFER_TTL)
    except Exception as exc:
        logger.error(f"[{call_sid}] Failed to buffer turn: {exc}")


async def flush_turn_buffer(call_sid: str) -> int:
    """
    Move buffered turns from `msg_buffer:{call_sid}` back to `db_sync_queue`.
    Called at call end to recover any turns that missed the primary queue.
    Returns the number of turns flushed.
    """
    key = f"{_MSG_BUFFER_PREFIX}{call_sid}"
    flushed = 0
    try:
        redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
        async with redis_client:
            while True:
                item = await redis_client.lpop(key)
                if item is None:
                    break
                await redis_client.rpush("db_sync_queue", item)
                flushed += 1
        if flushed:
            logger.info(f"[{call_sid}] Flushed {flushed} buffered turns to db_sync_queue")
    except Exception as exc:
        logger.error(f"[{call_sid}] Turn buffer flush failed: {exc}")
    return flushed


# ─── Post-call analytics pipeline ────────────────────────────────────────────

async def run_post_call_pipeline(
    call_sid: str,
    conversation: list[dict],   # [{"role": ..., "content": ...}, ...]
    intake: dict,
    language: str = "en",
    phone: str = "",
    ghl_contact_id: str = "",
    duration_sec: int = 0,
) -> None:
    """
    Orchestrate the full post-call analytics pipeline.
    Designed to be run as `asyncio.create_task(run_post_call_pipeline(...))`.

    Steps (all errors caught per-step so one failure doesn't block others):
      1. Flush any buffered turns
      2. Generate AI conversation summary
      3. Extract structured immigration data
      4. Analyse sentiment
      5. Write final call_log row to db_sync_queue
      6. Send confirmation SMS (if appointment booked)
    """
    logger.info(f"[{call_sid}] Post-call pipeline starting ({len(conversation)} turns)")

    # Step 1: Flush buffer
    await flush_turn_buffer(call_sid)

    # Steps 2-4 can run concurrently
    summary, structured, sentiment_result = await asyncio.gather(
        _generate_summary(call_sid, conversation, language),
        _extract_structured_data(call_sid, conversation, intake, language),
        _analyse_sentiment(call_sid, conversation),
        return_exceptions=True,
    )

    # Normalise exceptions to None
    summary = summary if isinstance(summary, str) else None
    structured = structured if isinstance(structured, dict) else {}
    sentiment_result = sentiment_result if isinstance(sentiment_result, dict) else {}

    # Step 5: Final call_log update
    await _write_call_summary_row(
        call_sid=call_sid,
        summary=summary,
        structured=structured,
        sentiment=sentiment_result,
        duration_sec=duration_sec,
    )

    logger.info(f"[{call_sid}] Post-call pipeline complete")


# ─── Step 2: AI summary ───────────────────────────────────────────────────────

async def _generate_summary(
    call_sid: str,
    conversation: list[dict],
    language: str = "en",
) -> str:
    """Produce a 3-5 sentence call summary for the GHL contact notes field."""
    if not conversation:
        return ""
    lang_hint = "English" if language == "en" else "Spanish"
    transcript_text = "\n".join(
        f"{m['role'].upper()}: {m['content']}" for m in conversation[-30:]
    )
    system_prompt = (
        f"You are a legal intake coordinator. Summarize this call in {lang_hint} "
        "in 3-5 sentences. Include: caller's stated problem, key facts gathered, "
        "any appointments booked, and recommended next steps. Be concise."
    )
    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=settings.openai_api_key)
        resp = await client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": transcript_text},
            ],
            max_tokens=250,
            temperature=0.2,
        )
        return resp.choices[0].message.content.strip()
    except Exception as exc:
        logger.error(f"[{call_sid}] Summary generation error: {exc}")
        return ""


# ─── Step 3: Structured data extraction ──────────────────────────────────────

async def _extract_structured_data(
    call_sid: str,
    conversation: list[dict],
    intake: dict,
    language: str = "en",
) -> dict:
    """
    Extract clean structured JSON from the conversation for the immigration_intake table.
    Merges with the already-captured `intake` dict so slot-filled data is not lost.
    Delegates to structured_data module.
    """
    try:
        from app.logging_analytics.structured_data import extract_structured_intake
        return await extract_structured_intake(call_sid, conversation, intake, language)
    except Exception as exc:
        logger.error(f"[{call_sid}] Structured extraction error: {exc}")
        return intake  # fall back to raw intake


# ─── Step 4: Sentiment analysis ───────────────────────────────────────────────

async def _analyse_sentiment(call_sid: str, conversation: list[dict]) -> dict:
    """
    Delegates to sentiment_scorer module.
    Returns {"score": float, "label": str, "frustration_detected": bool, ...}
    """
    try:
        from app.logging_analytics.sentiment_scorer import score_conversation
        return await score_conversation(call_sid, conversation)
    except Exception as exc:
        logger.error(f"[{call_sid}] Sentiment analysis error: {exc}")
        return {}


# ─── Step 5: Write call summary row ─────────────────────────────────────────

async def _write_call_summary_row(
    call_sid: str,
    summary: Optional[str],
    structured: dict,
    sentiment: dict,
    duration_sec: int,
) -> None:
    payload = json.dumps({
        "type": "call_summary",
        "call_sid": call_sid,
        "summary": summary or "",
        "structured": structured,
        "sentiment_score": sentiment.get("score"),
        "sentiment_label": sentiment.get("label", ""),
        "frustration_detected": sentiment.get("frustration_detected", False),
        "duration_sec": duration_sec,
        "ts": _now_ms(),
    })
    try:
        redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
        async with redis_client:
            await redis_client.rpush("db_sync_queue", payload)
    except Exception as exc:
        logger.error(f"[{call_sid}] Failed to queue call summary row: {exc}")


# ─── Utility ──────────────────────────────────────────────────────────────────

def _now_ms() -> int:
    return int(time.time() * 1000)
