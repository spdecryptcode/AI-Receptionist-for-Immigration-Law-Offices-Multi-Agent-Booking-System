"""
Appointment slot cache — Redis-backed availability cache.

GHL's slot API has a ~200ms p99 latency and a 100 req/min rate limit.
We cache available slots in Redis as a sorted set (score = ISO timestamp epoch)
so the agent can offer slots without hitting the API on every turn.

Key schema:
  slots:{calendar_id}:{YYYY-MM-DD}   — Sorted set, score=epoch, member=ISO string
  TTL: 1 hour (slots are invalidated by the booking webhook from GHL)

The booking webhook (ghl_webhooks.py) should call `invalidate_date()` when an
appointment.create event arrives to prevent showing booked slots.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

from app.config import settings

logger = logging.getLogger(__name__)

_SLOT_TTL = 3600  # 1 hour
_SLOT_KEY_PREFIX = "slots"


def _slot_key(calendar_id: str, day: str) -> str:
    return f"{_SLOT_KEY_PREFIX}:{calendar_id}:{day}"


async def cache_slots(
    calendar_id: str,
    day: str,  # "YYYY-MM-DD"
    slots: list[dict],  # [{startTime: ISO, endTime: ISO}, ...]
    redis,
) -> None:
    """
    Store a list of available slots in Redis sorted set.
    `slots` from GHL's /appointments/slots endpoint.
    """
    key = _slot_key(calendar_id, day)
    pipe = redis.pipeline()
    # Clear any stale entries for this day
    pipe.delete(key)
    for slot in slots:
        start_iso = slot.get("startTime") or slot.get("start_time", "")
        if not start_iso:
            continue
        try:
            dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
            epoch = dt.timestamp()
            # Store full slot JSON as the member, with epoch as score for ordering
            pipe.zadd(key, {json.dumps(slot): epoch})
        except (ValueError, TypeError) as exc:
            logger.debug(f"Skipping unparseable slot {start_iso}: {exc}")
    pipe.expire(key, _SLOT_TTL)
    await pipe.execute()
    logger.debug(f"Cached {len(slots)} slots for {calendar_id}/{day}")


async def get_cached_slots(
    calendar_id: str,
    day: str,
    redis,
    now_epoch: float | None = None,
) -> list[dict]:
    """
    Return cached available slots for a given day.
    Filters out slots that are in the past.
    """
    key = _slot_key(calendar_id, day)
    now = now_epoch or time.time()
    try:
        members = await redis.zrangebyscore(key, now, "+inf")
        return [json.loads(m) for m in members]
    except Exception as exc:
        logger.warning(f"Failed to read slot cache for {calendar_id}/{day}: {exc}")
        return []


async def remove_slot(calendar_id: str, day: str, start_iso: str, redis) -> None:
    """Remove a specific slot from cache (called after booking)."""
    key = _slot_key(calendar_id, day)
    try:
        # We need to find and remove the member matching this start time
        all_members = await redis.zrange(key, 0, -1)
        for member in all_members:
            slot = json.loads(member)
            if slot.get("startTime") == start_iso or slot.get("start_time") == start_iso:
                await redis.zrem(key, member)
                logger.debug(f"Removed slot {start_iso} from cache")
                return
    except Exception as exc:
        logger.warning(f"Failed to remove slot {start_iso} from cache: {exc}")


async def invalidate_date(calendar_id: str, day: str, redis) -> None:
    """Invalidate all cached slots for a day (called on booking webhook)."""
    key = _slot_key(calendar_id, day)
    try:
        await redis.delete(key)
        logger.debug(f"Invalidated slot cache for {calendar_id}/{day}")
    except Exception as exc:
        logger.warning(f"Failed to invalidate slot cache for {calendar_id}/{day}: {exc}")


def get_next_business_days(n: int = 5, tz: ZoneInfo | None = None) -> list[str]:
    """
    Return ISO date strings for the next `n` business days (Mon-Fri),
    starting from today (inclusive) so same-day slots are offered.
    """
    tz = tz or settings.tz
    today = datetime.now(tz).date()
    result: list[str] = []
    d = today
    while len(result) < n:
        if d.weekday() < 5:  # Mon=0, Fri=4
            result.append(d.isoformat())
        d += timedelta(days=1)
    return result
