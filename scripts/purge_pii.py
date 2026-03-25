"""
PII purge script — GDPR / CCPA / client retention policy compliance.

Purpose: Hard-delete or anonymise personal data in call_logs and
immigration_intake records older than the configured retention period
(default 365 days).

What is purged:
  - call_logs.caller_phone → "+XXXXXXXX" (redacted)
  - conversation_messages rows for the call → hard DELETE
  - immigration_intake rows for the call → hard DELETE
  - Redis call context keys → DEL (if still present)

What is preserved (for legal/billing audit):
  - call_logs row itself (without PII) — duration, timestamps, lead score
  - call_logs.call_sid — needed for Twilio billing reconciliation

Run via cron or manual trigger:
  python -m scripts.purge_pii [--dry-run] [--days 365] [--batch 500]

Requires DATABASE_URL and REDIS_URL env vars (loaded from .env automatically).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Allow running as a script from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logger = logging.getLogger("purge_pii")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


async def purge(dry_run: bool, retention_days: int, batch_size: int) -> None:
    from dotenv import load_dotenv
    load_dotenv()

    # Import after env is loaded
    from app.config import settings
    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
    from sqlalchemy.orm import sessionmaker
    import redis.asyncio as aioredis

    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    logger.info(
        f"Purge PII — cutoff={cutoff.date()} dry_run={dry_run} "
        f"retention={retention_days}d batch={batch_size}"
    )

    engine = create_async_engine(settings.database_url, echo=False)
    AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    total_redacted = 0
    total_deleted_msgs = 0
    total_deleted_intake = 0

    async with AsyncSessionLocal() as session:
        # 1. Find expired call SIDs in batches
        result = await session.execute(
            sa.text(
                """
                SELECT id, call_sid, caller_phone
                FROM call_logs
                WHERE created_at < :cutoff
                  AND caller_phone != '+REDACTED'
                ORDER BY created_at
                LIMIT :batch_size
                """
            ),
            {"cutoff": cutoff, "batch_size": batch_size},
        )
        rows = result.fetchall()

        if not rows:
            logger.info("No records found for purge.")
            await engine.dispose()
            return

        logger.info(f"Found {len(rows)} call_logs rows to anonymise")
        call_ids = [r.id for r in rows]
        call_sids = [r.call_sid for r in rows]

        if dry_run:
            logger.info(f"[DRY RUN] Would redact {len(rows)} call_logs rows")
            logger.info(f"[DRY RUN] Would delete conversation_messages for {len(call_sids)} calls")
            logger.info(f"[DRY RUN] Would delete immigration_intake for {len(call_sids)} calls")
            await engine.dispose()
            return

        # 2. Redact caller_phone in call_logs
        await session.execute(
            sa.text(
                "UPDATE call_logs SET caller_phone = '+REDACTED' WHERE id = ANY(:ids)"
            ),
            {"ids": call_ids},
        )
        total_redacted = len(call_ids)

        # 3. Delete conversation_messages
        result2 = await session.execute(
            sa.text(
                "DELETE FROM conversation_messages WHERE call_sid = ANY(:sids)"
            ),
            {"sids": call_sids},
        )
        total_deleted_msgs = result2.rowcount

        # 4. Delete immigration_intake
        result3 = await session.execute(
            sa.text(
                "DELETE FROM immigration_intake WHERE call_sid = ANY(:sids)"
            ),
            {"sids": call_sids},
        )
        total_deleted_intake = result3.rowcount

        await session.commit()

    # 5. Clean up Redis keys (best-effort)
    try:
        redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
        for sid in call_sids:
            await redis_client.delete(
                f"call:{sid}",
                f"conversation:{sid}",
                f"lead_score:{sid}",
                f"msg_buffer:{sid}",
            )
        await redis_client.aclose()
        logger.info(f"Redis keys cleaned for {len(call_sids)} calls")
    except Exception as exc:
        logger.warning(f"Redis cleanup partial failure: {exc}")

    await engine.dispose()
    logger.info(
        f"Purge complete — redacted={total_redacted} "
        f"msgs_deleted={total_deleted_msgs} "
        f"intake_deleted={total_deleted_intake}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Purge PII from call data")
    parser.add_argument("--dry-run", action="store_true", help="Simulate without writing")
    parser.add_argument("--days", type=int, default=365, help="Retention period in days")
    parser.add_argument("--batch", type=int, default=500, help="Rows per batch")
    args = parser.parse_args()
    asyncio.run(purge(dry_run=args.dry_run, retention_days=args.days, batch_size=args.batch))


if __name__ == "__main__":
    main()
