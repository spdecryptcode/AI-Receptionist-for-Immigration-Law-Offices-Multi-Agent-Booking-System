"""
RAG knowledge-base backfill script.

Ingests ALL existing data from the application database into the RAG
knowledge base (knowledge_documents + knowledge_chunks tables).

Data sources processed (in order):
  1. Prompt files   — prompts/*.md (firm_policy)
  2. Transcripts    — conversation_messages joined to conversations,
                      filtered by immigration keywords  (conversation_transcript)
  3. Intake records — immigration_intake grouped by case type, one
                      structured summary document per case-type bucket  (case_guide)
  4. Call summaries — call_logs.ai_summary (where not null)  (faq)
  5. Lead-score     — lead_scores.reasoning + score breakdown  (case_guide)
  6. Urgency alerts — urgency_alerts.details grouped by alert_type  (faq)
  7. Intake patterns (all-time) — same aggregation as nightly job but over
                                   the full history  (faq)

Idempotent: the DocumentIngester skips documents whose content hash already
exists in knowledge_documents.  Re-running is safe.

Usage:
    python -m scripts.backfill_rag [options]

Options:
    --dry-run           Print what would be ingested without writing to DB.
    --batch N           Number of transcripts/summaries processed concurrently
                        (default 5 — keep OpenAI rate limits in mind).
    --skip-transcripts  Skip conversation transcript ingestion.
    --skip-intakes      Skip intake record ingestion.
    --skip-summaries    Skip call summary ingestion.
    --skip-scores       Skip lead-score reasoning ingestion.
    --skip-alerts       Skip urgency alert ingestion.
    --skip-patterns     Skip all-time intake pattern aggregation.
    --min-messages N    Minimum conversation turns required to ingest a
                        transcript (default 4).
    --since YYYY-MM-DD  Only backfill records created on or after this date.

Requires DATABASE_URL, REDIS_URL, and OPENAI_API_KEY env vars
(loaded from .env automatically).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Allow running directly from the repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logger = logging.getLogger("backfill_rag")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt(n: int, label: str) -> str:
    return f"{n:,} {label}"


# ---------------------------------------------------------------------------
# Source 1: Prompt files
# ---------------------------------------------------------------------------

async def backfill_prompts(ingester, dry_run: bool) -> int:
    """Ingest all prompts/*.md files as firm_policy documents."""
    prompts_dir = Path("prompts")
    if not prompts_dir.exists():
        logger.warning("prompts/ directory not found — skipping")
        return 0

    md_files = sorted(prompts_dir.glob("*.md"))
    if not md_files:
        logger.info("No .md files found in prompts/ — skipping")
        return 0

    ingested = 0
    for md_file in md_files:
        content = md_file.read_text(encoding="utf-8").strip()
        if not content:
            continue
        language = "es" if "_es" in md_file.stem else "en"
        title = md_file.stem.replace("_", " ").title()
        logger.info(f"  [prompt] {md_file.name} → '{title}' ({language})")
        if not dry_run:
            doc_id = await ingester.ingest_document(
                title=title,
                source_type="firm_policy",
                language=language,
                content=content,
                metadata={"source_file": md_file.name, "backfill": True},
            )
            if doc_id:
                ingested += 1
        else:
            ingested += 1
    return ingested


# ---------------------------------------------------------------------------
# Source 2: Conversation transcripts
# ---------------------------------------------------------------------------

async def backfill_transcripts(
    ingester,
    pool,
    dry_run: bool,
    concurrency: int,
    min_messages: int,
    since: Optional[datetime],
) -> int:
    """Ingest historical call transcripts that contain immigration keywords."""
    since_clause = "AND c.created_at >= $2" if since else ""
    params: list = [min_messages]
    if since:
        params.append(since)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT
                c.call_sid,
                c.caller_name,
                c.created_at,
                c.call_outcome,
                COUNT(cm.id) AS msg_count
            FROM conversations c
            JOIN conversation_messages cm ON cm.call_sid = c.call_sid
            WHERE c.call_sid IS NOT NULL
              {since_clause}
            GROUP BY c.call_sid, c.caller_name, c.created_at, c.call_outcome
            HAVING COUNT(cm.id) >= $1
            ORDER BY c.created_at DESC
            """,
            *params,
        )

    if not rows:
        logger.info("  No transcripts found meeting criteria.")
        return 0

    logger.info(f"  Found {len(rows):,} transcripts with ≥{min_messages} messages.")

    sem = asyncio.Semaphore(concurrency)
    ingested_count = 0
    skipped_count = 0

    async def _process(row) -> bool:
        call_sid = row["call_sid"]
        caller_name = row.get("caller_name") or ""
        async with sem:
            # Fetch the full transcript
            async with pool.acquire() as conn:
                msg_rows = await conn.fetch(
                    """
                    SELECT cm.role, cm.content, cm.turn_index
                    FROM conversation_messages cm
                    WHERE cm.call_sid = $1
                    ORDER BY cm.turn_index
                    """,
                    call_sid,
                )

            if not msg_rows:
                return False

            turns = [
                f"{'Caller' if r['role'] == 'caller' else 'Agent'}: {r['content']}"
                for r in msg_rows
            ]
            transcript = "\n".join(turns)

            outcome = row.get("call_outcome") or "unknown"
            date_str = row["created_at"].strftime("%Y-%m-%d") if row["created_at"] else "unknown"
            call_sid_short = (call_sid or "")[:8]
            label_parts = [p for p in [caller_name, date_str, outcome] if p]
            title = f"Call transcript {call_sid_short}" + (f" ({', '.join(label_parts)})" if label_parts else "")
            # Prepend caller name so RAG can find by name
            header = f"Caller: {caller_name}\n" if caller_name else ""
            content_with_header = header + transcript

            if dry_run:
                logger.info(f"    [DRY] transcript {call_sid[:8]}… caller={caller_name!r} ({len(turns)} turns)")
                return True

            doc_id = await ingester.ingest_document(
                title=title,
                source_type="conversation_transcript",
                language="en",
                content=content_with_header,
                metadata={
                    "call_sid": call_sid,
                    "caller_name": caller_name,
                    "call_outcome": outcome,
                    "backfill": True,
                },
            )
            return doc_id is not None

    results = await asyncio.gather(*[_process(r) for r in rows], return_exceptions=True)
    for r in results:
        if isinstance(r, Exception):
            logger.error(f"  Transcript error: {r}")
        elif r is True:
            ingested_count += 1
        else:
            skipped_count += 1

    logger.info(
        f"  Transcripts: {ingested_count:,} ingested, {skipped_count:,} skipped "
        f"(no keywords or duplicate)."
    )
    return ingested_count


# ---------------------------------------------------------------------------
# Source 3: Intake records grouped by case type
# ---------------------------------------------------------------------------

async def backfill_intakes(
    ingester,
    pool,
    dry_run: bool,
    since: Optional[datetime],
) -> int:
    """
    Generate one structured case_guide document per case-type by aggregating
    all intake records for that type.
    """
    since_clause = "AND ii.created_at >= $1" if since else ""
    params: list = [since] if since else []

    import asyncpg as _asyncpg
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT
                    ii.case_type,
                    ii.current_immigration_status,
                    ii.country_of_birth,
                    ii.prior_deportation,
                    ii.criminal_history,
                    ii.has_attorney,
                    ii.family_in_us,
                    ii.urgency_reason,
                    ii.full_name,
                    ii.call_sid,
                    ii.created_at,
                    cv.urgency_label
                FROM immigration_intakes ii
                LEFT JOIN conversations cv ON cv.call_sid = ii.call_sid
                WHERE ii.case_type IS NOT NULL
                  {since_clause}
                ORDER BY ii.case_type, ii.created_at DESC
                """,
                *params,
            )
    except _asyncpg.UndefinedTableError:
        logger.info("  immigration_intakes table does not exist — skipping.")
        return 0

    if not rows:
        logger.info("  No intake records found.")
        return 0

    # Group by case_type
    from collections import defaultdict
    by_type: dict[str, list] = defaultdict(list)
    for r in rows:
        by_type[r["case_type"]].append(r)

    logger.info(f"  Found {len(rows):,} intake records across {len(by_type)} case types.")

    ingested = 0
    for case_type, intakes in by_type.items():
        urgency_counts: dict[str, int] = {}
        prior_dep_count = sum(1 for i in intakes if i["prior_deportation"])
        criminal_count = sum(1 for i in intakes if i["criminal_history"])
        attorney_count = sum(1 for i in intakes if i["has_attorney"])
        family_count = sum(1 for i in intakes if i["family_in_us"])

        for intake in intakes:
            u = intake["urgency_label"] or "routine"
            urgency_counts[u] = urgency_counts.get(u, 0) + 1

        # Country distribution
        countries: dict[str, int] = {}
        for i in intakes:
            c = i["country_of_birth"] or "unknown"
            countries[c] = countries.get(c, 0) + 1

        # Immigration status distribution
        statuses: dict[str, int] = {}
        for i in intakes:
            s = i["current_immigration_status"] or "unknown"
            statuses[s] = statuses.get(s, 0) + 1

        lines = [
            f"## Case Type: {case_type} — Intake Summary ({len(intakes)} cases)",
            "",
            f"Total intake records: {len(intakes)}",
            "",
            "### Urgency distribution",
        ]
        for u, cnt in sorted(urgency_counts.items(), key=lambda x: -x[1]):
            lines.append(f"- {u}: {cnt} cases ({cnt * 100 // len(intakes)}%)")

        lines += [
            "",
            "### Key indicators",
            f"- Prior deportation order: {prior_dep_count}",
            f"- Criminal history noted: {criminal_count}",
            f"- Already has attorney: {attorney_count}",
            f"- Family in US: {family_count}",
        ]

        if countries:
            lines += ["", "### Countries of birth (top 5)"]
            for c, cnt in sorted(countries.items(), key=lambda x: -x[1])[:5]:
                lines.append(f"- {c}: {cnt}")

        if statuses:
            lines += ["", "### Current immigration statuses"]
            for s, cnt in sorted(statuses.items(), key=lambda x: -x[1])[:5]:
                lines.append(f"- {s}: {cnt}")

        # Sample urgency reasons
        reasons = list({i["urgency_reason"] for i in intakes if i["urgency_reason"]})[:5]
        if reasons:
            lines += ["", "### Sample urgency reasons"]
            for r in reasons:
                lines.append(f"- {r}")

        content = "\n".join(lines)
        title = f"Case guide: {case_type} ({len(intakes)} cases)"

        logger.info(f"  [intake] {case_type}: {len(intakes)} records → '{title}'")

        if not dry_run:
            doc_id = await ingester.ingest_document(
                title=title,
                source_type="case_guide",
                language="en",
                content=content,
                metadata={
                    "case_type": case_type,
                    "record_count": len(intakes),
                    "backfill": True,
                },
            )
            if doc_id:
                ingested += 1
        else:
            ingested += 1

    return ingested


# ---------------------------------------------------------------------------
# Source 4: Call summaries
# ---------------------------------------------------------------------------

async def backfill_call_summaries(
    ingester,
    pool,
    dry_run: bool,
    concurrency: int,
    since: Optional[datetime],
) -> int:
    """Ingest ai_summary fields from call_logs as individual FAQ documents."""
    since_clause = "AND cl.created_at >= $1" if since else ""
    params = [since] if since else []

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT
                cl.call_sid,
                cl.ai_summary,
                cl.sentiment_label,
                cl.created_at
            FROM call_logs cl
            WHERE cl.ai_summary IS NOT NULL
              AND LENGTH(cl.ai_summary) > 50
              {since_clause}
            ORDER BY cl.created_at DESC
            """,
            *params,
        )

    if not rows:
        logger.info("  No call summaries found.")
        return 0

    logger.info(f"  Found {len(rows):,} call summaries.")

    sem = asyncio.Semaphore(concurrency)
    ingested = 0

    async def _ingest_summary(row) -> bool:
        async with sem:
            call_sid = row["call_sid"]
            date_str = row["created_at"].strftime("%Y-%m-%d") if row["created_at"] else ""
            sentiment = row["sentiment_label"] or "neutral"

            lines = [f"## Call Summary ({date_str}, sentiment={sentiment})", ""]
            lines.append(row["ai_summary"].strip())

            content = "\n".join(lines)
            call_sid_short = (call_sid or "")[:8]
            title = f"Call summary {call_sid_short} ({date_str})"

            if dry_run:
                logger.info(f"    [DRY] summary {call_sid_short}…")
                return True

            doc_id = await ingester.ingest_document(
                title=title,
                source_type="faq",
                language="en",
                content=content,
                metadata={
                    "call_sid": call_sid,
                    "backfill": True,
                },
            )
            return doc_id is not None

    results = await asyncio.gather(*[_ingest_summary(r) for r in rows], return_exceptions=True)
    for r in results:
        if isinstance(r, Exception):
            logger.error(f"  Summary error: {r}")
        elif r:
            ingested += 1

    logger.info(f"  Summaries: {ingested:,} ingested.")
    return ingested


# ---------------------------------------------------------------------------
# Source 5: Lead score reasoning
# ---------------------------------------------------------------------------

async def backfill_lead_scores(
    ingester,
    pool,
    dry_run: bool,
    since: Optional[datetime],
) -> int:
    """
    Group lead score reasoning by qualification_status + routing_recommendation
    and ingest one case_guide document per bucket.
    """
    since_clause = "AND ls.created_at >= $1" if since else ""
    params = [since] if since else []

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT
                ls.total_score,
                ls.recommended_attorney_tier,
                ls.recommended_follow_up,
                ls.notes,
                ls.top_signals,
                ls.call_sid
            FROM lead_scores ls
            WHERE ls.notes IS NOT NULL
              AND LENGTH(ls.notes) > 20
              {since_clause}
            ORDER BY ls.created_at DESC
            LIMIT 2000
            """,
            *params,
        )

    if not rows:
        logger.info("  No lead score reasoning found.")
        return 0

    logger.info(f"  Found {len(rows):,} lead score records with reasoning.")

    from collections import defaultdict
    buckets: dict[str, list] = defaultdict(list)
    for r in rows:
        tier = r["recommended_attorney_tier"] or "unassigned"
        follow_up = r["recommended_follow_up"] or "unassigned"
        key = f"{tier}_{follow_up}"
        buckets[key].append(r)

    ingested = 0
    for bucket_key, bucket_rows in buckets.items():
        scores = [r["total_score"] for r in bucket_rows if r["total_score"] is not None]
        avg_score = sum(scores) / len(scores) if scores else 0
        tier = bucket_rows[0]["recommended_attorney_tier"] or "unassigned"
        follow_up = bucket_rows[0]["recommended_follow_up"] or "unassigned"

        lines = [
            f"## Lead Scoring Guide: {tier} / {follow_up}",
            f"Based on {len(bucket_rows)} assessed leads. Average score: {avg_score:.1f}/100",
            "",
            "### Sample notes from this tier",
        ]

        # Include up to 10 representative notes
        seen: set[str] = set()
        for r in bucket_rows[:20]:
            note = (r["notes"] or "").strip()
            if not note or note in seen:
                continue
            seen.add(note)
            lines.append(f"\n**score={r['total_score']}:** {note}")
            if len(seen) >= 10:
                break

        content = "\n".join(lines)
        title = f"Lead scoring: {tier} / {follow_up} ({len(bucket_rows)} cases)"

        logger.info(f"  [score] {title}")

        if not dry_run:
            doc_id = await ingester.ingest_document(
                title=title,
                source_type="case_guide",
                language="en",
                content=content,
                metadata={
                    "attorney_tier": tier,
                    "follow_up": follow_up,
                    "sample_count": len(bucket_rows),
                    "avg_score": round(avg_score, 1),
                    "backfill": True,
                },
            )
            if doc_id:
                ingested += 1
        else:
            ingested += 1

    return ingested


# ---------------------------------------------------------------------------
# Source 6: Urgency alerts
# ---------------------------------------------------------------------------

async def backfill_urgency_alerts(
    ingester,
    pool,
    dry_run: bool,
    since: Optional[datetime],
) -> int:
    """
    Group urgency alerts by alert_type and generate one FAQ document per type
    describing how each category has been handled historically.
    """
    since_clause = "AND ua.alerted_at >= $1" if since else ""
    params = [since] if since else []

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT
                ua.urgency_label,
                ua.urgency_score,
                ua.factors,
                ua.recommended_action,
                ua.call_sid,
                ua.alerted_at,
                ua.resolved
            FROM urgency_alerts ua
            WHERE ua.factors IS NOT NULL
              {since_clause}
            ORDER BY ua.alerted_at DESC
            """,
            *params,
        )

    if not rows:
        logger.info("  No urgency alerts with details found.")
        return 0

    logger.info(f"  Found {len(rows):,} urgency alert records.")

    from collections import defaultdict
    by_label: dict[str, list] = defaultdict(list)
    for r in rows:
        by_label[r["urgency_label"] or "unknown"].append(r)

    ingested = 0
    for urgency_label, alert_rows in by_label.items():
        action_counts: dict[str, int] = {}
        for r in alert_rows:
            action = r["recommended_action"] or "unspecified"
            action_counts[action] = action_counts.get(action, 0) + 1

        lines = [
            f"## Urgency Level: {urgency_label}",
            f"Total occurrences: {len(alert_rows)}",
            "",
            "### Recommended actions distribution",
        ]
        for action, cnt in sorted(action_counts.items(), key=lambda x: -x[1]):
            lines.append(f"- {action}: {cnt} times")

        lines += ["", "### Representative urgency factors (most recent)"]
        seen_factors: set[str] = set()
        for r in alert_rows[:15]:
            factors = r["factors"]
            if not factors:
                continue
            factor_str = str(factors)[:300]
            if factor_str in seen_factors:
                continue
            seen_factors.add(factor_str)
            score = r["urgency_score"] or 0
            lines.append(f"\n**score={score}:** {factor_str}")
            if len(seen_factors) >= 8:
                break

        content = "\n".join(lines)
        title = f"Urgency factors: {urgency_label} ({len(alert_rows)} cases)"

        logger.info(f"  [alert] {title}")

        if not dry_run:
            doc_id = await ingester.ingest_document(
                title=title,
                source_type="faq",
                language="en",
                content=content,
                metadata={
                    "urgency_label": urgency_label,
                    "occurrence_count": len(alert_rows),
                    "backfill": True,
                },
            )
            if doc_id:
                ingested += 1
        else:
            ingested += 1

    return ingested


# ---------------------------------------------------------------------------
# Source 7: All-time intake pattern aggregation
# ---------------------------------------------------------------------------

async def backfill_intake_patterns(
    ingester,
    pool,
    dry_run: bool,
    since: Optional[datetime],
) -> int:
    """
    Run the same aggregation as aggregate_intake_patterns() but over the full
    historical window (no 30-day limit) and with a lower frequency threshold.
    """
    since_clause = "AND cl.created_at >= $2" if since else ""
    params: list = [1]  # min frequency = 1 for backfill (no threshold)
    if since:
        params.append(since)

    import asyncpg as _asyncpg
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT
                    ii.case_type,
                    cv.urgency_label,
                    ii.prior_deportation,
                    ii.criminal_history,
                    ii.has_attorney,
                    COUNT(*) AS frequency
                FROM immigration_intakes ii
                LEFT JOIN conversations cv ON cv.call_sid = ii.call_sid
                WHERE ii.case_type IS NOT NULL
                  {since_clause}
                GROUP BY
                    ii.case_type, cv.urgency_label, ii.prior_deportation,
                    ii.criminal_history, ii.has_attorney
                HAVING COUNT(*) >= $1
                ORDER BY frequency DESC
                LIMIT 200
                """,
                *params,
            )
    except _asyncpg.UndefinedTableError:
        logger.info("  immigration_intakes table does not exist — skipping.")
        return 0

    if not rows:
        logger.info("  No intake patterns found.")
        return 0

    total_calls = sum(r["frequency"] for r in rows)
    logger.info(f"  Found {len(rows):,} unique intake pattern combinations ({total_calls:,} total cases).")

    lines = [
        "## All-Time Immigration Intake Patterns",
        f"Aggregated from {total_calls:,} total intake records across {len(rows):,} pattern buckets.",
        "",
        "### Top patterns by frequency",
    ]
    for r in rows:
        urgency = r["urgency_label"] or "routine"
        prior_dep = " + prior deportation" if r["prior_deportation"] else ""
        criminal = " + criminal history" if r["criminal_history"] else ""
        attorney = " (has attorney)" if r["has_attorney"] else ""
        lines.append(
            f"- {r['case_type']}{prior_dep}{criminal}{attorney} | "
            f"urgency={urgency} → {r['frequency']} cases"
        )

    content = "\n".join(lines)
    title = f"All-time intake patterns ({total_calls:,} cases, backfill)"

    logger.info(f"  [patterns] {title}")

    if dry_run:
        return 1

    doc_id = await ingester.ingest_document(
        title=title,
        source_type="faq",
        language="en",
        content=content,
        metadata={
            "pattern_count": len(rows),
            "total_cases": total_calls,
            "backfill": True,
            "all_time": True,
        },
    )
    return 1 if doc_id else 0


# ---------------------------------------------------------------------------
# Caller profiles (one document per unique caller — covers metadata-only calls)
# ---------------------------------------------------------------------------

async def backfill_caller_profiles(
    ingester,
    pool,
    dry_run: bool,
    since: Optional[datetime],
) -> int:
    """
    Create one RAG document per unique caller from conversation metadata.
    This covers the 100+ conversations that have no stored transcript messages,
    so staff can still ask "Who is Viktor Petrov?" and get a useful answer.
    """
    since_clause = "AND cv.started_at >= $1" if since else ""
    params: list = [since] if since else []

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT
                cv.call_sid,
                cv.caller_name,
                cv.caller_phone,
                cv.language_detected,
                cv.urgency_label,
                cv.urgency_score,
                cv.lead_score,
                cv.call_outcome,
                cv.duration_seconds,
                cv.channel,
                cv.started_at,
                cv.scheduled_at,
                cl.ai_summary,
                cl.sentiment_label,
                ii.case_type,
                ii.current_immigration_status,
                ii.country_of_birth,
                ii.nationality,
                ii.prior_deportation,
                ii.criminal_history,
                ii.has_attorney,
                ii.urgency_reason,
                ii.family_in_us,
                ii.employer_sponsor,
                ls.total_score,
                ls.recommended_attorney_tier,
                ls.recommended_follow_up,
                ls.top_signals,
                ls.notes AS score_notes
            FROM conversations cv
            LEFT JOIN call_logs cl
                ON cl.call_sid = cv.call_sid AND cl.event_type = 'call_ended'
            LEFT JOIN immigration_intakes ii
                ON ii.call_sid = cv.call_sid
            LEFT JOIN lead_scores ls
                ON ls.call_sid = cv.call_sid
            WHERE cv.caller_name IS NOT NULL
            {since_clause}
            ORDER BY cv.started_at DESC NULLS LAST
            """,
            *params,
        )

    if not rows:
        logger.info("  [caller-profiles] No conversations found.")
        return 0

    # Group by (caller_name, caller_phone) — same person may have called multiple times
    from collections import defaultdict
    callers: dict[tuple, list] = defaultdict(list)
    for r in rows:
        key = (r["caller_name"] or "Unknown", r["caller_phone"] or "")
        callers[key].append(r)

    logger.info(f"  [caller-profiles] {len(rows)} conversations → {len(callers)} unique callers")

    ingested = 0
    for (name, phone), calls in callers.items():
        lines: list[str] = [
            f"Caller: {name}",
            f"Phone: {phone or 'unknown'}",
            f"Language: {calls[0]['language_detected'] or 'unknown'}",
            f"Total calls: {len(calls)}",
        ]

        # Most-recent call summary
        latest = calls[0]
        lines.append(f"\nLead Score: {latest['lead_score'] or latest['total_score'] or 'N/A'}")
        if latest["recommended_attorney_tier"]:
            lines.append(f"Attorney Tier: {latest['recommended_attorney_tier']}")
        if latest["recommended_follow_up"]:
            lines.append(f"Recommended Follow-up: {latest['recommended_follow_up']}")

        # Intake / case info (from most recent call that has intake data)
        intake_row = next((c for c in calls if c["case_type"]), None)
        if intake_row:
            lines.append(f"\nCase Type: {intake_row['case_type']}")
            if intake_row["current_immigration_status"]:
                lines.append(f"Immigration Status: {intake_row['current_immigration_status']}")
            if intake_row["country_of_birth"]:
                lines.append(f"Country of Birth: {intake_row['country_of_birth']}")
            if intake_row["nationality"]:
                lines.append(f"Nationality: {intake_row['nationality']}")
            flags = []
            if intake_row["prior_deportation"]:
                flags.append("prior deportation")
            if intake_row["criminal_history"]:
                flags.append("criminal history")
            if intake_row["has_attorney"]:
                flags.append("has attorney")
            if intake_row["family_in_us"]:
                flags.append("family in US")
            if intake_row["employer_sponsor"]:
                flags.append("employer sponsor")
            if flags:
                lines.append(f"Flags: {', '.join(flags)}")
            if intake_row["urgency_reason"]:
                lines.append(f"Urgency Reason: {intake_row['urgency_reason']}")

        # Lead score signals
        if latest["top_signals"]:
            lines.append(f"\nTop Signals: {latest['top_signals']}")
        if latest["score_notes"]:
            lines.append(f"Score Notes: {latest['score_notes']}")

        # Per-call history
        lines.append("\nCall History:")
        for c in calls:
            date_str = c["started_at"].strftime("%Y-%m-%d %H:%M") if c["started_at"] else "unknown date"
            outcome = c["call_outcome"] or "unknown outcome"
            urgency = c["urgency_label"] or "unknown"
            dur = f"{c['duration_seconds']}s" if c["duration_seconds"] else "?"
            lines.append(
                f"  - {date_str} | {outcome} | urgency={urgency} | {dur}"
            )
            if c["ai_summary"]:
                lines.append(f"    Summary: {c['ai_summary']}")
            if c["sentiment_label"]:
                lines.append(f"    Sentiment: {c['sentiment_label']}")
            if c["scheduled_at"]:
                lines.append(f"    Appointment: {c['scheduled_at'].strftime('%Y-%m-%d %H:%M')}")

        content = "\n".join(lines)
        title = f"Caller profile: {name} ({len(calls)} call{'s' if len(calls) != 1 else ''})"

        logger.info(f"  [caller-profiles] {title}")

        if dry_run:
            ingested += 1
            continue

        doc_id = await ingester.ingest_document(
            title=title,
            source_type="caller_profile",
            language=calls[0]["language_detected"] or "en",
            content=content,
            metadata={
                "caller_name": name,
                "caller_phone": phone,
                "call_count": len(calls),
                "latest_call_sid": calls[0]["call_sid"],
                "backfill": True,
            },
        )
        if doc_id:
            ingested += 1

    return ingested


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run(args: argparse.Namespace) -> None:
    from dotenv import load_dotenv
    load_dotenv()

    from app.config import settings

    since: Optional[datetime] = None
    if args.since:
        try:
            since = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
        except ValueError:
            logger.error(f"Invalid --since date '{args.since}'. Use YYYY-MM-DD.")
            sys.exit(1)

    if not settings.database_url:
        logger.error(
            "DATABASE_URL is not set.\n\n"
            "Get the connection string from:\n"
            "  Supabase dashboard → Project Settings → Database → URI\n\n"
            "Then add it to your .env file:\n"
            "  DATABASE_URL=postgresql://postgres:[PASSWORD]@db.nvcsmcnwwqedsfxpzfwa.supabase.co:5432/postgres\n"
        )
        sys.exit(1)

    logger.info(
        f"RAG backfill starting — dry_run={args.dry_run} "
        f"batch={args.batch} since={args.since or 'all time'}"
    )

    # ── Initialise clients ─────────────────────────────────────────────
    import asyncpg
    import openai
    import redis.asyncio as aioredis

    redis_client = aioredis.from_url(settings.redis_url, decode_responses=False)
    openai_client = openai.AsyncOpenAI(api_key=settings.openai_api_key)

    pool = await asyncpg.create_pool(
        settings.database_url,
        min_size=2,
        max_size=8,
        command_timeout=60,
    )
    logger.info("Database pool and clients ready.")

    # Monkey-patch the module-level dependency accessors so DocumentIngester
    # picks up the clients we just created
    import app.dependencies as deps
    deps._asyncpg_pool = pool
    deps._openai_client = openai_client
    deps._redis_client = redis_client

    from app.rag.ingestion import DocumentIngester
    ingester = DocumentIngester()

    totals: dict[str, int] = {}

    # ── 1. Prompt files ────────────────────────────────────────────────
    logger.info("\n[1/8] Backfilling prompt files…")
    n = await backfill_prompts(ingester, dry_run=args.dry_run)
    totals["prompts"] = n
    logger.info(f"  → {_fmt(n, 'documents')}")

    # ── 2. Transcripts ─────────────────────────────────────────────────
    if not args.skip_transcripts:
        logger.info("\n[2/8] Backfilling conversation transcripts…")
        n = await backfill_transcripts(
            ingester, pool,
            dry_run=args.dry_run,
            concurrency=args.batch,
            min_messages=args.min_messages,
            since=since,
        )
        totals["transcripts"] = n
        logger.info(f"  → {_fmt(n, 'documents')}")
    else:
        logger.info("\n[2/8] Transcripts — SKIPPED")

    # ── 3. Intake records ──────────────────────────────────────────────
    if not args.skip_intakes:
        logger.info("\n[3/8] Backfilling intake records…")
        n = await backfill_intakes(
            ingester, pool,
            dry_run=args.dry_run,
            since=since,
        )
        totals["intakes"] = n
        logger.info(f"  → {_fmt(n, 'documents')}")
    else:
        logger.info("\n[3/8] Intake records — SKIPPED")

    # ── 4. Call summaries ──────────────────────────────────────────────
    if not args.skip_summaries:
        logger.info("\n[4/8] Backfilling call summaries…")
        n = await backfill_call_summaries(
            ingester, pool,
            dry_run=args.dry_run,
            concurrency=args.batch,
            since=since,
        )
        totals["summaries"] = n
        logger.info(f"  → {_fmt(n, 'documents')}")
    else:
        logger.info("\n[4/8] Call summaries — SKIPPED")

    # ── 5. Lead score reasoning ────────────────────────────────────────
    if not args.skip_scores:
        logger.info("\n[5/8] Backfilling lead score reasoning…")
        n = await backfill_lead_scores(
            ingester, pool,
            dry_run=args.dry_run,
            since=since,
        )
        totals["lead_scores"] = n
        logger.info(f"  → {_fmt(n, 'documents')}")
    else:
        logger.info("\n[5/8] Lead scores — SKIPPED")

    # ── 6. Urgency alerts ──────────────────────────────────────────────
    if not args.skip_alerts:
        logger.info("\n[6/8] Backfilling urgency alerts…")
        n = await backfill_urgency_alerts(
            ingester, pool,
            dry_run=args.dry_run,
            since=since,
        )
        totals["urgency_alerts"] = n
        logger.info(f"  → {_fmt(n, 'documents')}")
    else:
        logger.info("\n[6/8] Urgency alerts — SKIPPED")

    # ── 7. All-time intake patterns ────────────────────────────────────
    if not args.skip_patterns:
        logger.info("\n[7/8] Backfilling all-time intake patterns…")
        n = await backfill_intake_patterns(
            ingester, pool,
            dry_run=args.dry_run,
            since=since,
        )
        totals["intake_patterns"] = n
        logger.info(f"  → {_fmt(n, 'documents')}")
    else:
        logger.info("\n[7/8] Intake patterns — SKIPPED")

    # ── 8. Caller profiles ────────────────────────────────────────────
    if not args.skip_profiles:
        logger.info("\n[8/8] Backfilling caller profiles (all conversations)…")
        n = await backfill_caller_profiles(
            ingester, pool,
            dry_run=args.dry_run,
            since=since,
        )
        totals["caller_profiles"] = n
        logger.info(f"  → {_fmt(n, 'documents')}")
    else:
        logger.info("\n[8/8] Caller profiles — SKIPPED")

    # ── Summary ────────────────────────────────────────────────────────
    grand_total = sum(totals.values())
    logger.info("\n" + "=" * 60)
    logger.info(f"RAG BACKFILL {'(DRY RUN) ' if args.dry_run else ''}COMPLETE")
    logger.info("=" * 60)
    for source, count in totals.items():
        logger.info(f"  {source:<22} {count:>6,} documents")
    logger.info(f"  {'TOTAL':<22} {grand_total:>6,} documents")
    if args.dry_run:
        logger.info("\nDry-run mode: no data was written to the database.")

    await pool.close()
    await redis_client.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill existing database records into the RAG knowledge base.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be ingested — do not write to DB.",
    )
    parser.add_argument(
        "--batch", type=int, default=5, metavar="N",
        help="Max concurrent OpenAI requests (default: 5).",
    )
    parser.add_argument(
        "--min-messages", type=int, default=1, metavar="N",
        help="Minimum conversation turns to include a transcript (default: 4).",
    )
    parser.add_argument(
        "--since", metavar="YYYY-MM-DD",
        help="Only backfill records created on or after this date.",
    )
    parser.add_argument("--skip-transcripts", action="store_true")
    parser.add_argument("--skip-intakes", action="store_true")
    parser.add_argument("--skip-summaries", action="store_true")
    parser.add_argument("--skip-scores", action="store_true")
    parser.add_argument("--skip-alerts", action="store_true")
    parser.add_argument("--skip-patterns", action="store_true")
    parser.add_argument("--skip-profiles", action="store_true")

    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
