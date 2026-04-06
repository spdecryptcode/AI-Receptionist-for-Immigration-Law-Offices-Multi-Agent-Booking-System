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

    # Step 6: Auto-ingest completed call into the RAG knowledge base.
    # a) Transcript — index for all calls that had actual conversation turns.
    # b) Caller profile — always upsert so staff chat can find any caller by name.
    if conversation:
        try:
            from app.rag.ingestion import DocumentIngester
            _ingester = DocumentIngester()
            asyncio.create_task(
                _ingester.ingest_conversation_transcript(call_sid)
            )
            asyncio.create_task(
                _ingest_caller_profile(call_sid, structured, summary, phone, language, _ingester)
            )
        except Exception as _exc:
            logger.debug(f"[{call_sid}] RAG ingestion skipped: {_exc}")


# ─── RAG caller-profile ingestion ────────────────────────────────────────────

async def _ingest_caller_profile(
    call_sid: str,
    structured: dict,
    summary: Optional[str],
    phone: str,
    language: str,
    ingester,
) -> None:
    """
    Build and upsert a caller-profile RAG document immediately after a call ends.
    Uses the structured-data extraction result so no extra DB round-trip is needed
    for the intake fields we already have in memory.
    """
    try:
        from app.dependencies import get_asyncpg_pool
        pool = await get_asyncpg_pool()
        async with pool.acquire() as conn:
            cv = await conn.fetchrow(
                """
                SELECT caller_name, caller_phone, language_detected,
                       urgency_label, urgency_score, lead_score,
                       call_outcome, duration_seconds, started_at, scheduled_at
                FROM conversations WHERE call_sid = $1
                """,
                call_sid,
            )
            cl = await conn.fetchrow(
                "SELECT ai_summary, sentiment_label FROM call_logs "
                "WHERE call_sid = $1 AND event_type = 'call_ended'",
                call_sid,
            )
            ii = await conn.fetchrow(
                """
                SELECT case_type, current_immigration_status, country_of_birth,
                       nationality, prior_deportation, criminal_history,
                       has_attorney, urgency_reason, family_in_us, employer_sponsor
                FROM immigration_intakes WHERE call_sid = $1
                """,
                call_sid,
            )
            ls = await conn.fetchrow(
                """
                SELECT total_score, recommended_attorney_tier,
                       recommended_follow_up, top_signals, notes
                FROM lead_scores WHERE call_sid = $1
                """,
                call_sid,
            )

        if not cv:
            return

        name = cv["caller_name"] or "Unknown"
        lines = [
            f"Caller: {name}",
            f"Phone: {cv['caller_phone'] or phone or 'unknown'}",
            f"Language: {cv['language_detected'] or language}",
            f"Total calls: 1",
        ]

        score = (cv["lead_score"] or (ls["total_score"] if ls else None))
        if score:
            lines.append(f"\nLead Score: {score}")
        if ls and ls["recommended_attorney_tier"]:
            lines.append(f"Attorney Tier: {ls['recommended_attorney_tier']}")
        if ls and ls["recommended_follow_up"]:
            lines.append(f"Recommended Follow-up: {ls['recommended_follow_up']}")

        if ii and ii["case_type"]:
            lines.append(f"\nCase Type: {ii['case_type']}")
            if ii["current_immigration_status"]:
                lines.append(f"Immigration Status: {ii['current_immigration_status']}")
            if ii["country_of_birth"]:
                lines.append(f"Country of Birth: {ii['country_of_birth']}")
            flags = [
                k for k, v in {
                    "prior deportation": ii["prior_deportation"],
                    "criminal history": ii["criminal_history"],
                    "has attorney": ii["has_attorney"],
                    "family in US": ii["family_in_us"],
                    "employer sponsor": ii["employer_sponsor"],
                }.items() if v
            ]
            if flags:
                lines.append(f"Flags: {', '.join(flags)}")
            if ii["urgency_reason"]:
                lines.append(f"Urgency Reason: {ii['urgency_reason']}")

        if ls and ls["top_signals"]:
            lines.append(f"\nTop Signals: {ls['top_signals']}")

        date_str = cv["started_at"].strftime("%Y-%m-%d %H:%M") if cv["started_at"] else "unknown date"
        outcome = cv["call_outcome"] or "unknown"
        urgency = cv["urgency_label"] or "unknown"
        dur = f"{cv['duration_seconds']}s" if cv["duration_seconds"] else "?"
        lines.append("\nCall History:")
        lines.append(f"  - {date_str} | {outcome} | urgency={urgency} | {dur}")
        ai_sum = (cl["ai_summary"] if cl else None) or summary
        if ai_sum:
            lines.append(f"    Summary: {ai_sum}")
        if cl and cl["sentiment_label"]:
            lines.append(f"    Sentiment: {cl['sentiment_label']}")
        if cv["scheduled_at"]:
            lines.append(f"    Appointment: {cv['scheduled_at'].strftime('%Y-%m-%d %H:%M')}")

        await ingester.ingest_document(
            title=f"Caller profile: {name} (1 call)",
            source_type="caller_profile",
            language=cv["language_detected"] or language,
            content="\n".join(lines),
            metadata={
                "caller_name": name,
                "caller_phone": cv["caller_phone"] or phone,
                "call_count": 1,
                "latest_call_sid": call_sid,
                "backfill": False,
            },
        )
        logger.info(f"[{call_sid}] RAG caller profile upserted for {name}")
    except Exception as exc:
        logger.warning(f"[{call_sid}] RAG caller profile ingestion failed: {exc}")


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
