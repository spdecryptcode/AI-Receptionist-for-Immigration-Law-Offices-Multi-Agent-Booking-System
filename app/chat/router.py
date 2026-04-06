"""
Web chat router — real-time immigration Q&A widget.

Endpoints:
  GET  /chat           → serve the embeddable chat widget (HTML)
  POST /chat/session   → create chat session, return {session_id, token}
  WS   /chat/ws/{session_id}?token={token}  → streaming chat
  GET  /chat/history/{session_id}           → retrieve chat history

Security:
  - Session tokens are random 32-byte URL-safe strings stored in Redis
  - WebSocket auth uses a one-time ws_token (stored in session, validated on connect)
  - Rate limiting: 30 messages/min per IP
  - Input is truncated at 1000 chars before any processing
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from app.config import settings
from app.dependencies import get_asyncpg_pool, get_openai_client, get_rag_retriever
from app.rag.context_builder import build_rag_context
from app.chat.session import (
    append_turn,
    check_rate_limit,
    create_session,
    delete_session,
    get_session,
    save_session,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/chat", tags=["chat"])

_MAX_INPUT_CHARS = 1000
_MAX_TOKENS_RESPONSE = 600

# Phases that benefit from RAG (skip for opening turns)
_RAG_PHASES = {"URGENCY_TRIAGE", "INTAKE", "CONSULTATION_PITCH", "BOOKING", "CONFIRMATION"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_system_prompt(language: str, mode: str = "client") -> str:
    lang = "es" if language == "es" else "en"
    if mode == "staff":
        prompt_path = Path("prompts") / f"system_prompt_staff_{lang}.md"
        # Fall back to English staff prompt if Spanish not available
        if not prompt_path.exists():
            prompt_path = Path("prompts") / "system_prompt_staff_en.md"
    else:
        prompt_path = Path("prompts") / f"system_prompt_{lang}.md"
    try:
        return prompt_path.read_text(encoding="utf-8").strip()
    except OSError:
        if mode == "staff":
            return (
                f"You are an internal AI assistant for staff at {settings.law_firm_name}. "
                "Help attorneys, paralegals, and receptionists with case information, urgency triage, "
                "and immigration procedure questions. Be direct and factual."
            )
        return (
            f"You are a helpful immigration attorney assistant at {settings.law_firm_name}. "
            "Answer questions clearly and compassionately. "
            "If the user needs legal advice, recommend scheduling a consultation."
        )


def _extract_names(text: str) -> list[str]:
    """Return potential person names found in text (2+ consecutive Title-Case words)."""
    return re.findall(r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b', text)


def _extract_phones(text: str) -> list[str]:
    """Return normalized 10-digit phone numbers found in text."""
    raw = re.findall(r'[\+1]?\s*[\(\-]?\d[\d\s\.\-\(\)]{6,}\d', text)
    out: list[str] = []
    for r in raw:
        digits = re.sub(r'\D', '', r)
        if 10 <= len(digits) <= 11:
            out.append(digits[-10:])
    return list(dict.fromkeys(out))


def _extract_call_sids(text: str) -> list[str]:
    """Return Twilio Call SIDs (CA + 32 hex chars) found in text."""
    return re.findall(r'\bCA[0-9a-fA-F]{32}\b', text)


async def _fetch_crm_context(user_text: str) -> str:
    """
    For staff mode: search the actual DB tables using the real schema.
    Queries conversations (caller_name), call_logs (ai_summary),
    immigration_intakes, and lead_scores by call_sid.
    """
    names = _extract_names(user_text)
    phones = _extract_phones(user_text)
    sids = _extract_call_sids(user_text)
    if not names and not phones and not sids:
        return ""

    pool = get_asyncpg_pool()
    lines: list[str] = []

    if pool:
        try:
            async with pool.acquire() as conn:
                for name in names[:2]:
                    parts = name.split()
                    # Search conversations by caller_name
                    conv_rows = await conn.fetch(
                        """
                        SELECT call_sid, caller_phone, caller_name,
                               urgency_label, urgency_score, lead_score,
                               call_outcome, duration_seconds, channel,
                               started_at, scheduled_at
                        FROM conversations
                        WHERE (
                            caller_name ILIKE $1
                            OR caller_name ILIKE $2
                            OR caller_name ILIKE $3
                        )
                        ORDER BY started_at DESC NULLS LAST
                        LIMIT 10
                        """,
                        f"%{name}%",
                        f"%{parts[0]}%",
                        f"%{parts[-1]}%",
                    )

                    if not conv_rows:
                        lines.append(
                            f"[No records found for '{name}' — "
                            "not in conversations table. Check spelling or phone number.]"
                        )
                        continue

                    # Group by unique person (same phone)
                    seen_phones: set = set()
                    for r in conv_rows:
                        phone = r["caller_phone"] or "N/A"
                        if phone not in seen_phones:
                            seen_phones.add(phone)
                            lines.append(
                                f"\n[Client: {r['caller_name']} | Phone: {phone} | "
                                f"Language: {r.get('language_detected', 'en')}]"
                            )

                    lines.append(f"  Call history ({len(conv_rows)} calls):")
                    for r in conv_rows:
                        started = str(r["started_at"])[:19] if r["started_at"] else "unknown"
                        dur = f"{r['duration_seconds']}s" if r["duration_seconds"] else "N/A"
                        scheduled = str(r["scheduled_at"])[:16] if r["scheduled_at"] else None
                        appt_str = f" | appt: {scheduled}" if scheduled else ""
                        sid = r["call_sid"]

                        lines.append(
                            f"    • {started} | {r['channel']} | {r['call_outcome'] or 'N/A'} | "
                            f"duration: {dur} | urgency: {r['urgency_label']} "
                            f"(score {r['urgency_score']}) | lead score: {r['lead_score']}"
                            f"{appt_str}"
                        )

                        # call_logs ai_summary for this call_sid
                        log_row = await conn.fetchrow(
                            "SELECT ai_summary, sentiment_label FROM call_logs "
                            "WHERE call_sid=$1 AND event_type='call_ended' LIMIT 1",
                            sid,
                        )
                        if log_row and log_row["ai_summary"]:
                            lines.append(f"      Summary: {log_row['ai_summary'][:300]}")

                        # intake data
                        intake = await conn.fetchrow(
                            """
                            SELECT case_type, current_immigration_status, country_of_birth,
                                   urgency_reason, has_attorney, prior_deportation,
                                   family_in_us, criminal_history
                            FROM immigration_intakes WHERE call_sid=$1 LIMIT 1
                            """,
                            sid,
                        )
                        if intake:
                            facts = []
                            if intake["case_type"]:
                                facts.append(f"case: {intake['case_type']}")
                            if intake["current_immigration_status"]:
                                facts.append(f"status: {intake['current_immigration_status']}")
                            if intake["country_of_birth"]:
                                facts.append(f"born: {intake['country_of_birth']}")
                            if intake["urgency_reason"]:
                                facts.append(f"urgency: {intake['urgency_reason']}")
                            if intake["has_attorney"]:
                                facts.append("has attorney")
                            if intake["prior_deportation"]:
                                facts.append("prior deportation")
                            if facts:
                                lines.append(f"      Intake: {' | '.join(facts)}")

                        # lead score
                        score = await conn.fetchrow(
                            """
                            SELECT total_score, recommended_attorney_tier,
                                   recommended_follow_up, top_signals
                            FROM lead_scores WHERE call_sid=$1 LIMIT 1
                            """,
                            sid,
                        )
                        if score:
                            lines.append(
                                f"      Lead score: {score['total_score']} | "
                                f"tier: {score['recommended_attorney_tier'] or 'N/A'} | "
                                f"follow-up: {score['recommended_follow_up'] or 'N/A'}"
                            )
                            if score["top_signals"]:
                                lines.append(f"      Signals: {str(score['top_signals'])[:200]}")

                # --- Phone number lookup ---
                for phone_digits in phones[:2]:
                    phone_rows = await conn.fetch(
                        """
                        SELECT call_sid, caller_name, caller_phone, urgency_label, urgency_score,
                               lead_score, call_outcome, duration_seconds, channel,
                               started_at, scheduled_at
                        FROM conversations
                        WHERE caller_phone LIKE $1 OR caller_phone LIKE $2
                        ORDER BY started_at DESC NULLS LAST LIMIT 5
                        """,
                        f"%{phone_digits}",
                        f"+1{phone_digits}",
                    )
                    if not phone_rows:
                        lines.append(f"[No records found for phone ending in {phone_digits}]")
                        continue
                    lines.append(f"\n[Phone lookup: ...{phone_digits}]")
                    for r in phone_rows:
                        started = str(r["started_at"])[:19] if r["started_at"] else "unknown"
                        dur = f"{r['duration_seconds']}s" if r["duration_seconds"] else "N/A"
                        sched = str(r["scheduled_at"])[:16] if r["scheduled_at"] else None
                        appt_str = f" | appt: {sched}" if sched else ""
                        lines.append(
                            f"  {r['caller_name'] or 'Unknown'} | {started} | "
                            f"{r['call_outcome'] or 'N/A'} | duration: {dur} | "
                            f"urgency: {r['urgency_label']}{appt_str}"
                        )
                        plog = await conn.fetchrow(
                            "SELECT ai_summary FROM call_logs "
                            "WHERE call_sid=$1 AND event_type='call_ended' LIMIT 1",
                            r["call_sid"],
                        )
                        if plog and plog["ai_summary"]:
                            lines.append(f"    Summary: {plog['ai_summary'][:300]}")

                # --- Call SID lookup ---
                for query_sid in sids[:2]:
                    sid_row = await conn.fetchrow(
                        """
                        SELECT call_sid, caller_name, caller_phone, urgency_label, urgency_score,
                               lead_score, call_outcome, duration_seconds, channel,
                               started_at, scheduled_at
                        FROM conversations WHERE call_sid = $1 LIMIT 1
                        """,
                        query_sid,
                    )
                    if not sid_row:
                        lines.append(f"[No record found for call SID: {query_sid}]")
                        continue
                    r = sid_row
                    started = str(r["started_at"])[:19] if r["started_at"] else "unknown"
                    dur = f"{r['duration_seconds']}s" if r["duration_seconds"] else "N/A"
                    lines.append(
                        f"\n[Call SID: {query_sid}]\n"
                        f"  Caller: {r['caller_name'] or 'Unknown'} | "
                        f"Phone: {r['caller_phone'] or 'N/A'}\n"
                        f"  Date: {started} | Channel: {r['channel']} | "
                        f"Outcome: {r['call_outcome'] or 'N/A'}\n"
                        f"  Duration: {dur} | Urgency: {r['urgency_label']} "
                        f"(score {r['urgency_score']}) | Lead score: {r['lead_score']}"
                    )
                    slog = await conn.fetchrow(
                        "SELECT ai_summary, sentiment_label FROM call_logs "
                        "WHERE call_sid=$1 AND event_type='call_ended' LIMIT 1",
                        query_sid,
                    )
                    if slog and slog["ai_summary"]:
                        lines.append(f"  Summary: {slog['ai_summary'][:400]}")
                    sintake = await conn.fetchrow(
                        """
                        SELECT case_type, current_immigration_status, country_of_birth,
                               urgency_reason, has_attorney, prior_deportation
                        FROM immigration_intakes WHERE call_sid=$1 LIMIT 1
                        """,
                        query_sid,
                    )
                    if sintake:
                        facts = []
                        for fld, lbl in [
                            ("case_type", "case"),
                            ("current_immigration_status", "status"),
                            ("country_of_birth", "born"),
                            ("urgency_reason", "urgency"),
                        ]:
                            if sintake[fld]:
                                facts.append(f"{lbl}: {sintake[fld]}")
                        if sintake["has_attorney"]:
                            facts.append("has attorney")
                        if sintake["prior_deportation"]:
                            facts.append("prior deportation")
                        if facts:
                            lines.append(f"  Intake: {' | '.join(facts)}")
                    sscore = await conn.fetchrow(
                        "SELECT total_score, recommended_attorney_tier, "
                        "recommended_follow_up, top_signals "
                        "FROM lead_scores WHERE call_sid=$1 LIMIT 1",
                        query_sid,
                    )
                    if sscore:
                        lines.append(
                            f"  Lead score: {sscore['total_score']} | "
                            f"tier: {sscore['recommended_attorney_tier'] or 'N/A'} | "
                            f"follow-up: {sscore['recommended_follow_up'] or 'N/A'}"
                        )

        except Exception as exc:
            logger.warning(f"DB staff lookup error: {exc}")

    # GHL search as supplement (contact tags / custom fields)
    try:
        from app.crm.ghl_client import get_ghl_client, ghl_is_available
        if ghl_is_available():
            ghl = get_ghl_client()
            for name in names[:1]:
                contacts = await asyncio.wait_for(
                    ghl.search_contacts_by_query(name, limit=2), timeout=2.0
                )
                if contacts:
                    lines.append(f"\n[CRM (GHL) matches for '{name}']")
                    for c in contacts:
                        first = c.get("firstName") or ""
                        last = c.get("lastName") or ""
                        tags = ", ".join(c.get("tags") or []) or "none"
                        lines.append(f"  • {first} {last} | tags: {tags}")
    except asyncio.TimeoutError:
        pass
    except Exception as exc:
        logger.debug(f"GHL lookup skipped: {exc}")

    if not lines:
        parts = []
        if names:
            parts.extend(f"'{n}'" for n in names[:2])
        if phones:
            parts.extend(f"phone ...{p}" for p in phones[:2])
        if sids:
            parts.extend(f"SID {s}" for s in sids[:2])
        query_str = ", ".join(parts) if parts else "the given query"
        lines.append(
            f"[No records found for {query_str}. "
            "Check spelling or try a different search term.]"
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# List-based staff queries (recent callers, urgent cases, appointments, leads,
# callbacks, callers-without-intake)
# ---------------------------------------------------------------------------

_LIST_KEYWORDS = [
    # Recent callers
    "recent caller", "recent call", "who called", "latest caller", "latest call",
    "show caller", "list caller", "last caller", "last call",
    "last person", "most recent caller", "most recent call", "who just called",
    "person who called", "person that called", "who last called", "who was last",
    # Urgent / critical
    "critical case", "critical caller", "urgent case", "detained", "detention",
    "who is urgent", "emergency case", "show urgent", "show critical",
    # Appointments
    "appointment today", "appointments today", "appointment tomorrow",
    "appointments tomorrow", "appointment this week", "scheduled today",
    "today's appointment", "upcoming appointment", "next appointment",
    "consultation today", "scheduled this week",
    # High leads / follow-up
    "high lead", "hot lead", "best lead", "top lead", "priority lead",
    "follow up", "follow-up", "highest score", "top scoring", "best leads",
    "who should we call", "who to call",
    # Callbacks
    "callback", "call back", "pending callback", "requested callback",
    # No-intake
    "no intake", "without intake", "missing intake", "no form",
]


async def _fetch_list_context(user_text: str) -> str:
    """
    For staff mode: return lists of callers matching specific intents —
    recent callers, urgent cases, appointments, top leads, callbacks,
    callers with no intake.
    """
    lower = user_text.lower()
    if not any(kw in lower for kw in _LIST_KEYWORDS):
        return ""

    pool = get_asyncpg_pool()
    if not pool:
        return ""

    def _fmt_row(r, *, show_appt: bool = False) -> str:
        name = r.get("caller_name") or "Unknown"
        phone = r.get("caller_phone") or "N/A"
        ts = r.get("started_at")
        started = str(ts)[:19] if ts else "N/A"
        outcome = r.get("call_outcome") or "N/A"
        urgency = r.get("urgency_label") or "N/A"
        lead = r.get("lead_score", "N/A")
        s = (
            f"  {name} | {phone} | {started} | "
            f"outcome: {outcome} | urgency: {urgency} | lead: {lead}"
        )
        if show_appt:
            apt = r.get("scheduled_at")
            if apt:
                s += f" | appt: {str(apt)[:16]}"
        return s

    lines: list[str] = []
    try:
        async with pool.acquire() as conn:

            # Recent callers
            if any(kw in lower for kw in [
                "recent caller", "recent call", "who called", "latest caller",
                "latest call", "show caller", "list caller", "last caller", "last call",
            ]):
                m = re.search(r'\blast\s+(\d+)', lower)
                limit = min(int(m.group(1)), 20) if m else 10
                rows = await conn.fetch(
                    """
                    SELECT call_sid, caller_name, caller_phone, started_at,
                           call_outcome, duration_seconds, urgency_label, lead_score, scheduled_at
                    FROM conversations
                    WHERE started_at IS NOT NULL
                    ORDER BY started_at DESC LIMIT $1
                    """,
                    limit,
                )
                if rows:
                    lines.append(f"\n[Recent callers — last {len(rows)} calls]")
                    for r in rows:
                        lines.append(_fmt_row(r, show_appt=True))
                else:
                    lines.append("[No calls recorded yet]")

            # Critical / urgent cases
            if any(kw in lower for kw in [
                "critical case", "critical caller", "urgent case", "detained", "detention",
                "who is urgent", "emergency case", "show urgent", "show critical",
            ]):
                levels = []
                if any(kw in lower for kw in ["critical", "detention", "detained", "emergency"]):
                    levels.append("critical")
                if any(kw in lower for kw in ["urgent", "urgent case", "urgent caller"]):
                    levels.append("high")
                if not levels:
                    levels = ["critical", "high"]
                rows = await conn.fetch(
                    """
                    SELECT c.call_sid, c.caller_name, c.caller_phone, c.started_at,
                           c.call_outcome, c.urgency_label, c.lead_score,
                           c.scheduled_at, i.urgency_reason, i.case_type
                    FROM conversations c
                    LEFT JOIN immigration_intakes i ON c.call_sid = i.call_sid
                    WHERE c.urgency_label = ANY($1::text[])
                    ORDER BY c.started_at DESC LIMIT 10
                    """,
                    levels,
                )
                label = "/".join(lv.upper() for lv in levels)
                if rows:
                    lines.append(f"\n[{label} urgency cases — last 10]")
                    for r in rows:
                        base = _fmt_row(r)
                        reason = r.get("urgency_reason") or ""
                        case = r.get("case_type") or ""
                        extra_str = (f" | case: {case}" if case else "") + (
                            f" | reason: {reason[:100]}" if reason else ""
                        )
                        lines.append(base + extra_str)
                else:
                    lines.append(f"[No {label} urgency callers found]")

            # Appointments today
            if any(kw in lower for kw in [
                "appointment today", "appointments today", "scheduled today",
                "today's appointment", "consultation today",
            ]):
                rows = await conn.fetch(
                    """
                    SELECT caller_name, caller_phone, scheduled_at, urgency_label, lead_score
                    FROM conversations
                    WHERE scheduled_at >= CURRENT_DATE
                      AND scheduled_at < CURRENT_DATE + INTERVAL '1 day'
                    ORDER BY scheduled_at ASC
                    """,
                )
                if rows:
                    lines.append(f"\n[Appointments today — {len(rows)} scheduled]")
                    for r in rows:
                        t = str(r["scheduled_at"])[:16]
                        lines.append(
                            f"  {r['caller_name'] or 'Unknown'} | "
                            f"{r['caller_phone'] or 'N/A'} | {t} | "
                            f"urgency: {r['urgency_label'] or 'N/A'}"
                        )
                else:
                    lines.append("[No appointments scheduled for today]")

            # Upcoming appointments (tomorrow / this week)
            if any(kw in lower for kw in [
                "upcoming appointment", "appointment this week", "next appointment",
                "scheduled this week", "appointment tomorrow", "appointments tomorrow",
            ]):
                rows = await conn.fetch(
                    """
                    SELECT caller_name, caller_phone, scheduled_at, urgency_label, lead_score
                    FROM conversations
                    WHERE scheduled_at > NOW()
                    ORDER BY scheduled_at ASC LIMIT 15
                    """,
                )
                if rows:
                    lines.append(f"\n[Upcoming appointments — next {len(rows)}]")
                    for r in rows:
                        t = str(r["scheduled_at"])[:16]
                        lines.append(
                            f"  {r['caller_name'] or 'Unknown'} | "
                            f"{r['caller_phone'] or 'N/A'} | {t} | "
                            f"urgency: {r['urgency_label'] or 'N/A'}"
                        )
                else:
                    lines.append("[No upcoming appointments scheduled]")

            # High-lead / priority callers
            if any(kw in lower for kw in [
                "high lead", "hot lead", "best lead", "top lead", "priority lead",
                "follow up", "follow-up", "highest score", "top scoring", "best leads",
                "who should we call", "who to call",
            ]):
                rows = await conn.fetch(
                    """
                    SELECT ls.total_score, ls.recommended_attorney_tier,
                           ls.recommended_follow_up,
                           c.caller_name, c.caller_phone, c.call_outcome,
                           c.urgency_label, c.started_at, i.case_type
                    FROM lead_scores ls
                    JOIN conversations c ON c.call_sid = ls.call_sid
                    LEFT JOIN immigration_intakes i ON c.call_sid = i.call_sid
                    WHERE ls.total_score IS NOT NULL
                    ORDER BY ls.total_score DESC LIMIT 10
                    """,
                )
                if rows:
                    lines.append(f"\n[Top leads by score — top {len(rows)}]")
                    for r in rows:
                        name = r["caller_name"] or "Unknown"
                        phone = r["caller_phone"] or "N/A"
                        score = r["total_score"]
                        tier = r["recommended_attorney_tier"] or "N/A"
                        followup = r["recommended_follow_up"] or "N/A"
                        case = r["case_type"] or "N/A"
                        started = str(r["started_at"])[:10] if r["started_at"] else "N/A"
                        lines.append(
                            f"  {name} | {phone} | score: {score} | tier: {tier} | "
                            f"follow-up: {followup} | case: {case} | date: {started}"
                        )
                else:
                    lines.append("[No lead scores found]")

            # Pending callbacks
            if any(kw in lower for kw in [
                "callback", "call back", "pending callback", "requested callback",
            ]):
                rows = await conn.fetch(
                    """
                    SELECT call_sid, caller_name, caller_phone, started_at,
                           urgency_label, lead_score
                    FROM conversations
                    WHERE call_outcome = 'callback_requested'
                    ORDER BY started_at DESC LIMIT 15
                    """,
                )
                if rows:
                    lines.append(f"\n[Pending callback requests — {len(rows)} total]")
                    for r in rows:
                        started = str(r["started_at"])[:19] if r["started_at"] else "N/A"
                        lines.append(
                            f"  {r['caller_name'] or 'Unknown'} | "
                            f"{r['caller_phone'] or 'N/A'} | {started} | "
                            f"urgency: {r['urgency_label'] or 'N/A'}"
                        )
                else:
                    lines.append("[No pending callback requests]")

            # Callers with no intake form
            if any(kw in lower for kw in [
                "no intake", "without intake", "missing intake", "no form",
            ]):
                rows = await conn.fetch(
                    """
                    SELECT c.call_sid, c.caller_name, c.caller_phone, c.started_at,
                           c.call_outcome, c.urgency_label
                    FROM conversations c
                    LEFT JOIN immigration_intakes i ON c.call_sid = i.call_sid
                    WHERE i.call_sid IS NULL
                      AND c.started_at >= NOW() - INTERVAL '30 days'
                    ORDER BY c.started_at DESC LIMIT 15
                    """,
                )
                if rows:
                    lines.append(
                        f"\n[Callers with no intake — last 30 days, {len(rows)} found]"
                    )
                    for r in rows:
                        started = str(r["started_at"])[:19] if r["started_at"] else "N/A"
                        lines.append(
                            f"  {r['caller_name'] or 'Unknown'} | "
                            f"{r['caller_phone'] or 'N/A'} | {started} | "
                            f"outcome: {r['call_outcome'] or 'N/A'}"
                        )
                else:
                    lines.append("[All recent callers have intake records]")

    except Exception as exc:
        logger.warning(f"List query error: {exc}")
        return ""

    return "\n".join(lines).strip()


_STATS_KEYWORDS = [
    "how many", "how much", "count", "total", "stats", "statistics",
    "analytics", "breakdown", "report", "summary", "number of", "how often",
    "most common", "top case", "volume", "pipeline", "per week", "per month",
    "last month", "last week", "this month", "this week", "last 30", "last 7",
    "past month", "past week", "past year", "last year", "today", "yesterday",
    "this year", "overall", "average", "avg", "rate", "trend", "conversion",
    "month", "which month", "maximum", "most calls", "busiest", "peak",
    "highest", "best month", "worst month", "monthly", "by month",
    "booking", "booked", "appointment", "cancel", "cancelled", "cancellation",
    "no-show", "transfer", "transferred", "intake", "intakes",
    "lead", "score", "language", "spanish", "english",
    "urgent", "critical", "case", "cases", "caller", "callers",
    "unique", "repeat", "returning", "daily", "weekly", "annual",
    "hour", "hours", "time of day", "peak hour", "busiest hour", "peak time",
    "day of week", "which day", "monday", "tuesday", "wednesday", "thursday", "friday",
    "detained", "detention", "hot lead", "warm lead", "cold lead",
    "lead quality", "lead distribution", "top leads", "priority",
    "removal defense", "asylum", "naturalization", "all time", "in the system",
    "in our system", "records", "all cases", "all calls",
]


async def _fetch_stats_context(user_text: str) -> str:
    """
    For staff mode: detect aggregate/analytics intent and return live DB stats.
    """
    lower = user_text.lower()
    if not any(kw in lower for kw in _STATS_KEYWORDS):
        return ""

    # Time period detection — calendar-accurate
    if "yesterday" in lower:
        tf = "c.started_at >= CURRENT_DATE - INTERVAL '1 day' AND c.started_at < CURRENT_DATE"
        period_label = "yesterday"
    elif "today" in lower:
        tf = "c.started_at >= CURRENT_DATE"
        period_label = "today"
    elif "this week" in lower:
        tf = "c.started_at >= date_trunc('week', NOW())"
        period_label = "this week"
    elif any(kw in lower for kw in ["last week", "past week"]):
        tf = ("c.started_at >= date_trunc('week', NOW() - INTERVAL '7 days') "
              "AND c.started_at < date_trunc('week', NOW())")
        period_label = "last week"
    elif "this month" in lower:
        tf = "c.started_at >= date_trunc('month', NOW())"
        period_label = "this month"
    elif any(kw in lower for kw in ["last month", "past month"]):
        tf = ("c.started_at >= date_trunc('month', NOW() - INTERVAL '1 month') "
              "AND c.started_at < date_trunc('month', NOW())")
        period_label = "last month"
    elif "this year" in lower:
        tf = "c.started_at >= date_trunc('year', NOW())"
        period_label = "this year"
    elif any(kw in lower for kw in ["last year", "past year"]):
        tf = ("c.started_at >= date_trunc('year', NOW() - INTERVAL '1 year') "
              "AND c.started_at < date_trunc('year', NOW())")
        period_label = "last year"
    elif any(kw in lower for kw in ["last 7", "7 days", "per week"]):
        tf = "c.started_at >= NOW() - INTERVAL '7 days'"
        period_label = "last 7 days"
    elif any(kw in lower for kw in ["last 30", "30 days", "per month"]):
        tf = "c.started_at >= NOW() - INTERVAL '30 days'"
        period_label = "last 30 days"
    elif any(kw in lower for kw in ["all time", "ever", "by month", "which month", "monthly breakdown"]):
        tf = "c.started_at IS NOT NULL"
        period_label = "all time"
    else:
        # If the query is specifically about a case type or "records/cases" with no
        # time qualifier, default to all time so counts match the full database.
        _CASE_TYPE_SIGNALS = [
            "removal defense", "asylum", "daca", "naturalization", "tps",
            "family sponsor", "employment visa", "h-1b", "h1b",
            "records", "all cases", "all calls", "total cases", "total calls",
            "overall", "in the system", "in our system",
        ]
        if any(sig in lower for sig in _CASE_TYPE_SIGNALS):
            tf = "c.started_at IS NOT NULL"
            period_label = "all time"
        else:
            tf = "c.started_at >= NOW() - INTERVAL '30 days'"
            period_label = "last 30 days"

    pool = get_asyncpg_pool()
    if not pool:
        return ""

    lines: list[str] = [f"[LIVE STATS — {period_label}]"]
    try:
        async with pool.acquire() as conn:
            total = await conn.fetchval(f"SELECT COUNT(*) FROM conversations c WHERE {tf}")
            lines.append(f"Total calls: {total}")

            booked = await conn.fetchval(
                f"SELECT COUNT(*) FROM conversations c WHERE {tf} AND c.call_outcome = 'booking_made'"
            )
            rate = round(booked / total * 100, 1) if total else 0
            lines.append(f"Bookings made: {booked} ({rate}% booking rate)")

            transferred = await conn.fetchval(
                f"SELECT COUNT(*) FROM conversations c WHERE {tf} AND c.call_outcome = 'transferred_to_staff'"
            )
            lines.append(f"Transferred to staff: {transferred}")

            avg_lead = await conn.fetchval(
                f"SELECT ROUND(AVG(c.lead_score)::numeric, 1) FROM conversations c WHERE {tf} AND c.lead_score IS NOT NULL"
            )
            if avg_lead is not None:
                lines.append(f"Average lead score: {avg_lead}/100")

            avg_dur = await conn.fetchval(
                f"SELECT ROUND(AVG(c.duration_seconds)) FROM conversations c WHERE {tf} AND c.duration_seconds IS NOT NULL"
            )
            if avg_dur:
                dur_s = float(avg_dur)
                lines.append(f"Avg call duration: {int(dur_s)}s ({round(dur_s / 60, 1)} min)")

            outcomes = await conn.fetch(
                f"SELECT c.call_outcome, COUNT(*) AS cnt FROM conversations c WHERE {tf} "
                f"GROUP BY c.call_outcome ORDER BY cnt DESC"
            )
            if outcomes:
                lines.append("\nBy outcome:")
                for row in outcomes:
                    lines.append(f"  {row['call_outcome'] or 'unknown'}: {row['cnt']}")

            cases = await conn.fetch(
                f"SELECT i.case_type, COUNT(*) AS cnt "
                f"FROM immigration_intakes i JOIN conversations c ON c.call_sid = i.call_sid "
                f"WHERE {tf} AND i.case_type IS NOT NULL "
                f"GROUP BY i.case_type ORDER BY cnt DESC LIMIT 20"
            )
            if cases:
                lines.append("\nBy case type:")
                for row in cases:
                    lines.append(f"  {row['case_type']}: {row['cnt']}")

            langs = await conn.fetch(
                f"SELECT c.language_detected, COUNT(*) AS cnt FROM conversations c WHERE {tf} "
                f"GROUP BY c.language_detected ORDER BY cnt DESC"
            )
            if langs:
                lines.append("\nBy language:")
                for row in langs:
                    lines.append(f"  {row['language_detected'] or 'unknown'}: {row['cnt']}")

            urgencies = await conn.fetch(
                f"SELECT c.urgency_label, COUNT(*) AS cnt FROM conversations c WHERE {tf} "
                f"GROUP BY c.urgency_label ORDER BY cnt DESC"
            )
            if urgencies:
                lines.append("\nBy urgency level:")
                for row in urgencies:
                    lines.append(f"  {row['urgency_label'] or 'unknown'}: {row['cnt']}")

            months = await conn.fetch(
                "SELECT to_char(date_trunc('month', started_at), 'Month YYYY') AS month, "
                "COUNT(*) AS cnt FROM conversations "
                "WHERE started_at IS NOT NULL "
                "GROUP BY date_trunc('month', started_at) ORDER BY cnt DESC"
            )
            if months:
                lines.append("\nCalls by month (all time):")
                for row in months:
                    lines.append(f"  {row['month'].strip()}: {row['cnt']}")

            unique = await conn.fetchval(
                f"SELECT COUNT(DISTINCT caller_phone) FROM conversations c "
                f"WHERE {tf} AND caller_phone IS NOT NULL"
            )
            if unique is not None and total:
                repeat = max(0, int(total) - int(unique))
                lines.append(f"\nUnique callers: {unique} (repeat calls: {repeat})")

            no_intake = await conn.fetchval(
                f"SELECT COUNT(*) FROM conversations c "
                f"LEFT JOIN immigration_intakes i ON c.call_sid = i.call_sid "
                f"WHERE {tf} AND i.call_sid IS NULL"
            )
            if no_intake is not None:
                lines.append(f"Calls with no intake form: {no_intake}")

            # --- Conditional: peak calling hours ---
            if any(kw in lower for kw in [
                "hour", "hours", "peak hour", "busiest hour", "peak time",
                "time of day", "when do most", "what time",
            ]):
                hours = await conn.fetch(
                    f"SELECT EXTRACT(hour FROM started_at)::int AS hr, COUNT(*) AS cnt "
                    f"FROM conversations c WHERE {tf} AND started_at IS NOT NULL "
                    f"GROUP BY hr ORDER BY cnt DESC LIMIT 12"
                )
                if hours:
                    lines.append("\nPeak call hours (top 12, UTC):")
                    for row in hours:
                        h = row["hr"]
                        ampm = f"{h % 12 or 12}{'am' if h < 12 else 'pm'}"
                        lines.append(f"  {ampm}: {row['cnt']}")

            # --- Conditional: day-of-week breakdown ---
            if any(kw in lower for kw in [
                "day of week", "which day", "busiest day", "monday", "tuesday",
                "wednesday", "thursday", "friday", "weekly pattern", "day breakdown",
            ]):
                days = await conn.fetch(
                    f"SELECT to_char(started_at, 'Day') AS dow, "
                    f"EXTRACT(isodow FROM started_at)::int AS dow_num, "
                    f"COUNT(*) AS cnt "
                    f"FROM conversations c WHERE {tf} AND started_at IS NOT NULL "
                    f"GROUP BY dow, dow_num ORDER BY dow_num"
                )
                if days:
                    lines.append("\nCalls by day of week:")
                    for row in days:
                        lines.append(f"  {row['dow'].strip()}: {row['cnt']}")

            # --- Conditional: appointment counts ---
            if any(kw in lower for kw in [
                "appointment", "appointments", "consultation", "scheduled", "booking",
                "meeting", "booked",
            ]):
                appt_today = await conn.fetchval(
                    "SELECT COUNT(*) FROM conversations "
                    "WHERE scheduled_at >= CURRENT_DATE "
                    "AND scheduled_at < CURRENT_DATE + INTERVAL '1 day'"
                )
                appt_week = await conn.fetchval(
                    "SELECT COUNT(*) FROM conversations "
                    "WHERE scheduled_at >= CURRENT_DATE "
                    "AND scheduled_at < CURRENT_DATE + INTERVAL '7 days'"
                )
                appt_all = await conn.fetchval(
                    "SELECT COUNT(*) FROM conversations WHERE scheduled_at IS NOT NULL"
                )
                lines.append(
                    f"\nAppointments: today={appt_today}, "
                    f"next 7 days={appt_week}, all time={appt_all}"
                )

            # --- Conditional: critical/detained callers ---
            if any(kw in lower for kw in [
                "detained", "detention", "critical case", "critical caller",
                "emergency", "how many critical",
            ]):
                crit_count = await conn.fetchval(
                    f"SELECT COUNT(*) FROM conversations c "
                    f"WHERE {tf} AND c.urgency_label = 'critical'"
                )
                crit_rows = await conn.fetch(
                    f"SELECT c.caller_name, c.caller_phone, c.started_at, i.urgency_reason "
                    f"FROM conversations c "
                    f"LEFT JOIN immigration_intakes i ON c.call_sid = i.call_sid "
                    f"WHERE {tf} AND c.urgency_label = 'critical' "
                    f"ORDER BY c.started_at DESC LIMIT 5"
                )
                lines.append(f"\nCritical urgency callers: {crit_count}")
                if crit_rows:
                    lines.append("  Most recent critical cases:")
                    for row in crit_rows:
                        name = row["caller_name"] or "Unknown"
                        phone = row["caller_phone"] or "N/A"
                        started = str(row["started_at"])[:10] if row["started_at"] else "N/A"
                        reason = row.get("urgency_reason") or ""
                        lines.append(
                            f"    {name} | {phone} | {started}"
                            + (f" | {reason[:80]}" if reason else "")
                        )

            # --- Conditional: lead quality distribution ---
            if any(kw in lower for kw in [
                "hot lead", "warm lead", "cold lead", "lead quality",
                "lead distribution", "top leads", "lead score distribution",
                "score breakdown", "qualified leads",
            ]):
                dist = await conn.fetchrow(
                    f"SELECT "
                    f"  SUM(CASE WHEN ls.total_score >= 70 THEN 1 ELSE 0 END) AS hot, "
                    f"  SUM(CASE WHEN ls.total_score >= 40 AND ls.total_score < 70 "
                    f"      THEN 1 ELSE 0 END) AS warm, "
                    f"  SUM(CASE WHEN ls.total_score < 40 THEN 1 ELSE 0 END) AS cold, "
                    f"  ROUND(AVG(ls.total_score)::numeric, 1) AS avg_score "
                    f"FROM lead_scores ls "
                    f"JOIN conversations c ON c.call_sid = ls.call_sid "
                    f"WHERE {tf} AND ls.total_score IS NOT NULL"
                )
                if dist and dist["avg_score"] is not None:
                    lines.append(
                        f"\nLead quality: hot (≥70)={dist['hot']}, "
                        f"warm (40–69)={dist['warm']}, cold (<40)={dist['cold']} "
                        f"| avg score={dist['avg_score']}"
                    )
                top_leads = await conn.fetch(
                    f"SELECT ls.total_score, ls.recommended_attorney_tier, "
                    f"c.caller_name, c.caller_phone, i.case_type "
                    f"FROM lead_scores ls "
                    f"JOIN conversations c ON c.call_sid = ls.call_sid "
                    f"LEFT JOIN immigration_intakes i ON c.call_sid = i.call_sid "
                    f"WHERE {tf} AND ls.total_score IS NOT NULL "
                    f"ORDER BY ls.total_score DESC LIMIT 5"
                )
                if top_leads:
                    lines.append("  Top 5 leads:")
                    for row in top_leads:
                        name = row["caller_name"] or "Unknown"
                        phone = row["caller_phone"] or "N/A"
                        tier = row["recommended_attorney_tier"] or "N/A"
                        case = row["case_type"] or "N/A"
                        lines.append(
                            f"    {name} | {phone} | score: {row['total_score']} "
                            f"| tier: {tier} | case: {case}"
                        )

    except Exception as exc:
        logger.warning(f"Stats query error: {exc}")
        return ""

    return "\n".join(lines)


def _build_openai_messages(
    session: dict,
    system_prompt: str,
    rag_context: str,
) -> list[dict]:
    messages: list[dict] = [{"role": "system", "content": system_prompt}]

    context_parts: list[str] = []
    if session.get("intake"):
        collected = ", ".join(
            f"{k}={v!r}" for k, v in session["intake"].items() if v
        )
        context_parts.append(f"[Collected information so far]\n{collected}")
    if rag_context:
        context_parts.append(rag_context)

    if context_parts:
        messages.append({"role": "system", "content": "\n\n".join(context_parts)})

    for turn in session.get("turns", []):
        role = "user" if turn["role"] == "user" else "assistant"
        messages.append({"role": role, "content": turn["content"]})

    return messages


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

class _CreateSessionBody(BaseModel):
    language: str = "en"
    mode: str = "client"  # "client" | "staff"


@router.post("/session", summary="Create a new chat session")
async def create_chat_session(body: _CreateSessionBody) -> JSONResponse:
    data = await create_session(language=body.language)
    data["mode"] = body.mode if body.mode in ("client", "staff") else "client"
    # Generate a single-use WebSocket auth token stored in session
    ws_token = secrets.token_urlsafe(32)
    data["ws_token"] = ws_token
    await save_session(data)
    return JSONResponse(
        {"session_id": data["session_id"], "ws_token": ws_token, "language": data["language"]}
    )


@router.get("/history/{session_id}", summary="Get chat history for a session")
async def get_history(session_id: str) -> JSONResponse:
    data = await get_session(session_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Session not found or expired")
    return JSONResponse(
        {
            "session_id": session_id,
            "language": data.get("language", "en"),
            "turns": data.get("turns", []),
            "phase": data.get("phase", "GREETING"),
        }
    )


@router.websocket("/ws/{session_id}")
async def chat_websocket(
    websocket: WebSocket,
    session_id: str,
    token: str = Query(...),
    request: Request = None,  # type: ignore[assignment]
):
    """
    WebSocket endpoint for streaming chat.

    Protocol:
      Client sends: {"message": "user text"}
      Server sends: {"type": "token", "content": "..."} (streamed)
                 or {"type": "done", "full_response": "..."}
                 or {"type": "error", "detail": "..."}
    """
    # --- Validate session and WS token ---
    session = await get_session(session_id)
    if session is None:
        await websocket.close(code=4401, reason="Session not found")
        return

    stored_token = session.pop("ws_token", None)
    if not stored_token or stored_token != token:
        await websocket.close(code=4403, reason="Invalid token")
        return

    # Invalidate the token (single-use)
    await save_session(session)

    await websocket.accept()
    language = session.get("language", "en")
    mode = session.get("mode", "client")
    system_prompt = _load_system_prompt(language, mode)

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
                user_text = str(msg.get("message", "")).strip()[:_MAX_INPUT_CHARS]
            except (json.JSONDecodeError, AttributeError):
                await websocket.send_text(
                    json.dumps({"type": "error", "detail": "Invalid JSON"})
                )
                continue

            if not user_text:
                continue

            # --- Rate limiting ---
            client_ip = (
                websocket.client.host if websocket.client else "unknown"
            )
            if not await check_rate_limit(client_ip):
                await websocket.send_text(
                    json.dumps({"type": "error", "detail": "Rate limit exceeded"})
                )
                continue

            # --- Append user turn ---
            session = await append_turn(session_id, "user", user_text)
            if session is None:
                await websocket.close(code=4410, reason="Session expired")
                return

            # --- RAG retrieval (always for staff, phase-gated for client) ---
            rag_context = ""
            phase = session.get("phase", "GREETING")
            retriever = get_rag_retriever()
            if retriever and (mode == "staff" or phase in _RAG_PHASES):
                try:
                    chunks = await asyncio.wait_for(
                        retriever.retrieve(
                            query=user_text,
                            language=language,
                            phase=phase,
                            channel="web",
                            session_id=session_id,
                        ),
                        timeout=0.5,
                    )
                    rag_context = build_rag_context(chunks, channel="web")
                except asyncio.TimeoutError:
                    logger.debug(f"[{session_id}] RAG retrieval timeout — proceeding without")
                except Exception as exc:
                    logger.warning(f"[{session_id}] RAG error: {exc}")

            # --- CRM client lookup + live stats (staff mode only) ---
            if mode == "staff":
                crm_context = await _fetch_crm_context(user_text)
                list_context = await _fetch_list_context(user_text)
                stats_context = await _fetch_stats_context(user_text)
                extra = "\n\n".join(filter(None, [stats_context, list_context, crm_context]))
                if extra:
                    rag_context = f"{extra}\n\n{rag_context}".strip()
                else:
                    # Guard against hallucination: if query looks like a data lookup
                    # but nothing was retrieved, tell the LLM explicitly.
                    _DATA_SIGNALS = [
                        "who called", "last caller", "last person", "person who called",
                        "person that called", "who last", "who just", "most recent",
                        "how many", "how much", "total", "average", "booking rate",
                        "lead score", "which month", "busiest", "this week", "today",
                        "last week", "last month", "callback", "appointment", "intake",
                    ]
                    if any(sig in user_text.lower() for sig in _DATA_SIGNALS):
                        rag_context = (
                            "[SYSTEM: A database lookup was attempted for this query but "
                            "returned no results. You must NOT invent caller names, phone "
                            "numbers, case details, or statistics. State clearly that no "
                            "matching record was found and suggest the user try a different "
                            "search (e.g. phone number, different spelling, or 'show recent callers').]"
                            + ("\n\n" + rag_context if rag_context else "")
                        ).strip()

            # --- Build messages and call LLM (streaming) ---
            messages = _build_openai_messages(session, system_prompt, rag_context)
            client = get_openai_client()
            full_response = ""

            try:
                stream = await client.chat.completions.create(
                    model=settings.openai_model,
                    messages=messages,
                    max_tokens=_MAX_TOKENS_RESPONSE,
                    temperature=0.4,
                    stream=True,
                )
                async for chunk in stream:
                    delta = chunk.choices[0].delta.content
                    if delta:
                        full_response += delta
                        await websocket.send_text(
                            json.dumps({"type": "token", "content": delta})
                        )
            except Exception as exc:
                logger.error(f"[{session_id}] LLM stream error: {exc}")
                await websocket.send_text(
                    json.dumps({"type": "error", "detail": "LLM error, please try again"})
                )
                continue

            # --- Persist assistant turn ---
            await append_turn(session_id, "assistant", full_response)

            # --- Advance phase detection (simple keyword scan) ---
            await _maybe_advance_chat_phase(session_id, full_response, user_text)

            await websocket.send_text(
                json.dumps({"type": "done", "full_response": full_response})
            )

    except WebSocketDisconnect:
        logger.debug(f"[{session_id}] WebSocket disconnected")
    except Exception as exc:
        logger.error(f"[{session_id}] WebSocket error: {exc}")
    finally:
        # Optionally: fire-and-forget session cleanup on disconnect
        pass


async def _maybe_advance_chat_phase(
    session_id: str, response_text: str, user_text: str
) -> None:
    """
    Simple phase advancement for web chat sessions based on keyword detection.
    """
    session = await get_session(session_id)
    if session is None:
        return
    current_phase = session.get("phase", "GREETING")

    text = (response_text + " " + user_text).upper()
    phase_map = {
        "PHASE:URGENCY_TRIAGE": "URGENCY_TRIAGE",
        "PHASE:INTAKE": "INTAKE",
        "PHASE:CONSULTATION_PITCH": "CONSULTATION_PITCH",
        "PHASE:BOOKING": "BOOKING",
        "PHASE:CONFIRMATION": "CONFIRMATION",
        "PHASE:CLOSING": "CLOSING",
    }
    for marker, new_phase in phase_map.items():
        if marker in text and new_phase != current_phase:
            session["phase"] = new_phase
            await save_session(session)

            # Speculative RAG prefetch for new phase
            retriever = get_rag_retriever()
            if retriever:
                asyncio.create_task(
                    retriever.prefetch(
                        phase=new_phase,
                        language=session.get("language", "en"),
                        case_type=session.get("case_type"),
                    )
                )
            break


# ---------------------------------------------------------------------------
# Chat widget HTML
# ---------------------------------------------------------------------------

@router.get("", response_class=HTMLResponse, include_in_schema=False)
@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def chat_widget():
    """Serve the embeddable web chat widget."""
    return HTMLResponse(_CHAT_WIDGET_HTML)


_CHAT_WIDGET_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Immigration Chat</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
     background:linear-gradient(135deg,#e8f0fe 0%,#f0f4f8 100%);
     display:flex;justify-content:center;align-items:center;min-height:100vh}
#app{width:420px;max-width:100vw;height:680px;display:flex;flex-direction:column;
     background:#fff;border-radius:16px;position:relative;
     box-shadow:0 16px 48px rgba(26,74,138,.18),0 2px 8px rgba(0,0,0,.06);
     overflow:hidden}

/* ── Header ── */
#header{background:linear-gradient(135deg,#1a4a8a 0%,#1d5db8 100%);
        color:#fff;padding:14px 18px;display:flex;align-items:center;gap:12px;
        position:relative}
#avatar{width:42px;height:42px;border-radius:50%;background:rgba(255,255,255,.18);
        display:flex;align-items:center;justify-content:center;
        font-size:20px;flex-shrink:0;border:2px solid rgba(255,255,255,.3)}
#header-text{flex:1;min-width:0}
#header-text h1{font-size:15px;font-weight:700;letter-spacing:.01em}
#header-text p{font-size:11.5px;opacity:.75;margin-top:1px;white-space:nowrap;
               overflow:hidden;text-overflow:ellipsis}
#status-dot{width:8px;height:8px;border-radius:50%;background:#4ade80;flex-shrink:0;
            box-shadow:0 0 0 2px rgba(74,222,128,.3);transition:background .3s}
#status-dot.connecting{background:#fbbf24;animation:pulse-dot 1s infinite}
#status-dot.offline{background:#f87171;box-shadow:none}
@keyframes pulse-dot{0%,100%{opacity:1}50%{opacity:.4}}
#lang-select{background:rgba(255,255,255,.12);border:1px solid rgba(255,255,255,.25);
             color:#fff;font-size:12px;cursor:pointer;padding:4px 8px;
             border-radius:6px;outline:none}
#lang-select:hover{background:rgba(255,255,255,.2)}
#lang-select option{background:#1a4a8a;color:#fff}

/* ── Status bar (Aria is thinking…) ── */
#status-bar{display:none}
@keyframes spin{to{transform:rotate(360deg)}}

/* ── Messages ── */
#messages{flex:1;overflow-y:auto;padding:16px 14px;display:flex;
          flex-direction:column;gap:10px;scroll-behavior:smooth}
#messages::-webkit-scrollbar{width:4px}
#messages::-webkit-scrollbar-track{background:transparent}
#messages::-webkit-scrollbar-thumb{background:#d0ddef;border-radius:4px}
.msg{max-width:82%;padding:10px 14px;border-radius:14px;font-size:14px;
     line-height:1.55;word-break:break-word;animation:fadeUp .2s ease both}
@keyframes fadeUp{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
.msg.user{align-self:flex-end;background:linear-gradient(135deg,#1a4a8a,#2563be);
          color:#fff;border-bottom-right-radius:4px}
.msg.bot{align-self:flex-start;background:#f4f7fc;color:#1a1a2e;
         border-bottom-left-radius:4px;border:1px solid #e4ecf8}
.msg.bot.streaming{border-color:#bdd1f5}
.msg.error{align-self:flex-start;background:#fff0f0;color:#c0392b;
           border:1px solid #fccaca;font-size:13px}

/* ── Typing indicator bubble ── */
.typing-bubble{align-self:flex-start;background:#f4f7fc;border:1px solid #e4ecf8;
               padding:12px 16px;border-radius:14px 14px 14px 4px;
               display:flex;gap:5px;align-items:center}
.typing-bubble span{width:7px;height:7px;background:#8aaed4;border-radius:50%;
                    animation:typingBounce 1.3s ease infinite}
.typing-bubble span:nth-child(2){animation-delay:.18s}
.typing-bubble span:nth-child(3){animation-delay:.36s}
@keyframes typingBounce{
  0%,60%,100%{transform:translateY(0);background:#8aaed4}
  30%{transform:translateY(-7px);background:#2563be}
}

/* ── Connecting overlay (fixed, outside #app) ── */
#connecting{position:fixed;inset:0;background:rgba(240,244,248,.97);
            display:flex;flex-direction:column;align-items:center;justify-content:center;
            gap:16px;z-index:1000;
            transition:opacity .4s ease,visibility .4s ease}
#connecting.hidden{opacity:0;visibility:hidden;pointer-events:none}
#connect-spinner{width:44px;height:44px;border:4px solid #d0ddef;
                 border-top-color:#1a4a8a;border-radius:50%;
                 animation:spin .8s linear infinite}
#connecting h2{font-size:15px;font-weight:600;color:#1a4a8a;letter-spacing:.01em}
#connecting p{font-size:12px;color:#6b85b0;font-weight:400}

/* ── Footer ── */
#footer{padding:12px 14px;border-top:1px solid #e8ecf0;display:flex;gap:8px;
        background:#fff;position:relative}
#input{flex:1;padding:10px 14px;border:1.5px solid #d0d8e4;border-radius:22px;
       font-size:14px;outline:none;resize:none;line-height:1.4;
       max-height:120px;overflow-y:auto;font-family:inherit;
       transition:border-color .2s,box-shadow .2s}
#input:focus{border-color:#2563be;box-shadow:0 0 0 3px rgba(37,99,190,.1)}
#input:disabled{background:#f8fafc;color:#94a3b8}
#send{width:42px;height:42px;border-radius:50%;
      background:linear-gradient(135deg,#1a4a8a,#2563be);
      color:#fff;border:none;cursor:pointer;display:flex;align-items:center;
      justify-content:center;flex-shrink:0;transition:transform .15s,opacity .2s;
      font-size:17px;box-shadow:0 2px 8px rgba(37,99,190,.35)}
#send:hover:not(:disabled){transform:scale(1.08)}
#send:disabled{opacity:.5;cursor:not-allowed;transform:none;box-shadow:none}
</style>
</head>
<body>
<div id="app">

  <div id="header">
    <div id="avatar">⚖️</div>
    <div id="header-text">
      <h1 id="firm-name">Immigration Assistant</h1>
      <p>Ask us about your case — we respond immediately</p>
    </div>
    <div id="status-dot" class="connecting" title="Connecting…"></div>
    <select id="lang-select" title="Language">
      <option value="en">🇺🇸 EN</option>
      <option value="es">🇲🇽 ES</option>
    </select>
  </div>

  <!-- Status bar (shown while Aria is thinking) -->
  <div id="status-bar">
    <div id="mini-spinner"></div>
    <span id="status-text">Aria is thinking…</span>
  </div>

  <div id="messages">
    <div class="msg bot" id="welcome-msg">
      Hello! I'm Aria, your immigration intake assistant. How can I help you today?
    </div>
  </div>

  <div id="footer">
    <textarea id="input" placeholder="Type your question…" rows="1"></textarea>
    <button id="send" title="Send">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
           stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
        <line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/>
      </svg>
    </button>
  </div>
</div>

<!-- Connecting overlay (fixed over full page) -->
<div id="connecting">
  <div id="connect-spinner"></div>
  <h2>Immigration Assistant</h2>
  <p>Connecting to Aria…</p>
</div>

<script>
(function(){
  const API = window.location.origin;
  let sessionId = null, wsToken = null, ws = null, language = 'en';
  let currentBotMsg = null;

  const messagesEl  = document.getElementById('messages');
  const inputEl     = document.getElementById('input');
  const sendBtn     = document.getElementById('send');
  const langSel     = document.getElementById('lang-select');
  const statusDot   = document.getElementById('status-dot');
  const connectEl   = document.getElementById('connecting');
  const welcomeEl   = document.getElementById('welcome-msg');
  let   typingBubble = null;
  const loadStart   = Date.now();
  const MIN_LOAD_MS = 900; // always show loader for at least 900ms

  function hideConnecting(){
    const wait = MIN_LOAD_MS - (Date.now() - loadStart);
    if(wait > 0) setTimeout(()=>connectEl.classList.add('hidden'), wait);
    else connectEl.classList.add('hidden');
  }

  const WELCOME = {
    en: "Hello! I'm Aria, your immigration intake assistant. How can I help you today?",
    es: '¡Hola! Soy tu asistente de inmigración. ¿En qué puedo ayudarte hoy?'
  };

  function setConnecting(){
    statusDot.className = 'connecting';
    statusDot.title = 'Connecting…';
    sendBtn.disabled = true;
  }
  function setOnline(){
    statusDot.className = '';
    statusDot.title = 'Connected';
    hideConnecting();
    sendBtn.disabled = false;
  }
  function setOffline(){
    statusDot.className = 'offline';
    statusDot.title = 'Disconnected';
    sendBtn.disabled = true;
  }
  function showThinking(on){
    if(on){
      if(typingBubble) return;
      typingBubble = document.createElement('div');
      typingBubble.className = 'typing-bubble';
      typingBubble.innerHTML = '<span></span><span></span><span></span>';
      messagesEl.appendChild(typingBubble);
      requestAnimationFrame(() => { messagesEl.scrollTop = messagesEl.scrollHeight; });
    } else {
      if(typingBubble){ typingBubble.remove(); typingBubble = null; }
    }
  }

  langSel.addEventListener('change', async (e) => {
    language = e.target.value;
    welcomeEl.textContent = WELCOME[language] || WELCOME.en;
    if(ws){ ws.close(); ws = null; }
    sessionId = null; wsToken = null;
    connectEl.classList.remove('hidden');
    setConnecting();
    await initSession();
  });

  async function initSession(){
    try{
      const r = await fetch(API + '/chat/session', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({language})
      });
      const data = await r.json();
      sessionId = data.session_id;
      wsToken   = data.ws_token;
      connectWS();
    } catch(e){
      hideConnecting();
      setOffline();
      appendMsg('error', '\u26a0\ufe0f Unable to connect. Please refresh the page.');
    }
  }

  function connectWS(){
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    ws = new WebSocket(proto + '://' + location.host + '/chat/ws/' + sessionId + '?token=' + wsToken);
    ws.onopen  = () => { setOnline(); inputEl.focus(); };
    ws.onclose = () => { setOffline(); };
    ws.onerror = () => { setOffline(); appendMsg('error', 'Connection lost. Please refresh.'); };
    ws.onmessage = (e) => {
      const msg = JSON.parse(e.data);
      if(msg.type === 'token'){
        if(!currentBotMsg){
          showThinking(false);
          currentBotMsg = appendMsg('bot', '');
          currentBotMsg.classList.add('streaming');
        }
        currentBotMsg.textContent += msg.content;
        scrollBottom();
      } else if(msg.type === 'done'){
        showThinking(false);
        if(currentBotMsg) currentBotMsg.classList.remove('streaming');
        currentBotMsg = null;
        sendBtn.disabled = false;
        inputEl.disabled = false;
        inputEl.focus();
      } else if(msg.type === 'error'){
        showThinking(false);
        appendMsg('error', '\u26a0\ufe0f ' + (msg.detail || 'An error occurred.'));
        sendBtn.disabled = false;
        inputEl.disabled = false;
      }
    };
  }

  function appendMsg(role, text){
    const div = document.createElement('div');
    div.className = 'msg ' + role;
    div.textContent = text;
    messagesEl.appendChild(div);
    scrollBottom();
    return div;
  }

  function scrollBottom(){
    messagesEl.scrollTo({top: messagesEl.scrollHeight, behavior:'smooth'});
  }

  function send(){
    const text = inputEl.value.trim();
    if(!text || !ws || ws.readyState !== WebSocket.OPEN) return;
    appendMsg('user', text);
    inputEl.value = '';
    inputEl.style.height = 'auto';
    sendBtn.disabled = true;
    inputEl.disabled = true;
    showThinking(true);
    ws.send(JSON.stringify({message: text}));
  }

  sendBtn.addEventListener('click', send);
  inputEl.addEventListener('keydown', (e) => {
    if(e.key === 'Enter' && !e.shiftKey){ e.preventDefault(); send(); }
  });
  inputEl.addEventListener('input', function(){
    this.style.height = 'auto';
    this.style.height = Math.min(this.scrollHeight, 120) + 'px';
  });

  initSession();
})();
</script>
</body>
</html>"""
