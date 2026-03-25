"""
Failed task retry script.

Retries items stuck in the `failed_tasks` table (written by db_worker when
a processing attempt fails permanently after its inline retries are exhausted).

Schema of `failed_tasks`:
    id           SERIAL PRIMARY KEY
    queue_name   TEXT    -- original Redis queue the item came from
    payload      JSONB   -- original payload
    error        TEXT    -- last error message
    retry_count  INT     -- number of retry attempts so far
    created_at   TIMESTAMPTZ
    last_tried   TIMESTAMPTZ

Each row is re-promoted to the original Redis queue up to MAX_RETRIES times.
After MAX_RETRIES, the row is marked `abandoned = true` — it will not be
retried further and will be reviewed manually.

Run:
    python -m scripts.retry_failed_tasks [--dry-run] [--max-retries 3] [--batch 100]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logger = logging.getLogger("retry_failed_tasks")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

_MAX_RETRIES_DEFAULT = 3
_BATCH_DEFAULT = 100


async def retry(dry_run: bool, max_retries: int, batch_size: int) -> None:
    from dotenv import load_dotenv
    load_dotenv()

    from app.config import settings
    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
    from sqlalchemy.orm import sessionmaker
    import redis.asyncio as aioredis

    engine = create_async_engine(settings.database_url, echo=False)
    AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)

    retried = 0
    abandoned = 0

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            sa.text(
                """
                SELECT id, queue_name, payload, retry_count
                FROM failed_tasks
                WHERE abandoned = false
                  AND retry_count < :max_retries
                ORDER BY created_at
                LIMIT :batch
                """
            ),
            {"max_retries": max_retries, "batch": batch_size},
        )
        rows = result.fetchall()

        if not rows:
            logger.info("No failed tasks found for retry.")
            await redis_client.aclose()
            await engine.dispose()
            return

        logger.info(f"Found {len(rows)} failed tasks to retry (dry_run={dry_run})")

        for row in rows:
            task_id, queue_name, payload, retry_count = row

            if dry_run:
                logger.info(
                    f"[DRY RUN] Would retry id={task_id} queue={queue_name} "
                    f"retry_count={retry_count}"
                )
                continue

            # Re-push to the original Redis queue
            raw = json.dumps(payload) if isinstance(payload, dict) else payload
            await redis_client.lpush(queue_name, raw)

            # Update retry count and last_tried
            await session.execute(
                sa.text(
                    "UPDATE failed_tasks SET retry_count = retry_count + 1, "
                    "last_tried = :now WHERE id = :id"
                ),
                {"now": datetime.now(timezone.utc), "id": task_id},
            )
            retried += 1
            logger.info(f"Re-queued task id={task_id} → {queue_name} (attempt {retry_count + 1})")

        # Mark exhausted tasks as abandoned
        result2 = await session.execute(
            sa.text(
                """
                UPDATE failed_tasks
                SET abandoned = true, last_tried = :now
                WHERE abandoned = false AND retry_count >= :max_retries
                RETURNING id
                """
            ),
            {"now": datetime.now(timezone.utc), "max_retries": max_retries},
        )
        abandoned_ids = result2.fetchall()
        abandoned = len(abandoned_ids)

        if not dry_run:
            await session.commit()

    await redis_client.aclose()
    await engine.dispose()

    logger.info(f"Retry run complete — retried={retried} abandoned={abandoned}")
    if abandoned:
        logger.warning(
            f"{abandoned} tasks marked abandoned after {max_retries} attempts. "
            "Review `failed_tasks` table manually."
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Retry failed background tasks")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-retries", type=int, default=_MAX_RETRIES_DEFAULT)
    parser.add_argument("--batch", type=int, default=_BATCH_DEFAULT)
    args = parser.parse_args()
    asyncio.run(retry(dry_run=args.dry_run, max_retries=args.max_retries, batch_size=args.batch))


if __name__ == "__main__":
    main()
