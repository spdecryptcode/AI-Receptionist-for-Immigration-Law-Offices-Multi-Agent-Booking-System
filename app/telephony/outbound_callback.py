"""
Outbound callback queue consumer.

Reads from the `callback_queue` Redis list (BRPOP, blocking pop with 5s timeout).
Each item is a JSON payload describing a caller who requested a callback.

Queue item schema:
    {
        "caller_number": "+15551234567",
        "caller_name": "Maria Garcia",          # optional
        "ghl_contact_id": "abc123",             # optional
        "language": "es",
        "reason": "new consultation",           # free-text
        "requested_at": "2025-01-15T10:23:00Z",
        "retries": 0
    }

Retry logic:
  - Max 3 attempts per item
  - If no answer / busy / failed: re-queue with retries += 1 and delay 1 hour
  - After 3 failures: create a GHL task for manual follow-up, stop retrying

Office hours check:
  - Will not call outside office hours; item is re-queued for next open window
  - Uses the same `is_office_open()` from call_router

Call mechanics:
  - Twilio REST `calls.create(url=twiml_url)` where twiml_url points to /twilio/callback-connect
  - The callback-connect webhook fires when the callee answers and connects them to the AI

Loop runs as a background asyncio task, created in main.py lifespan.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import redis.asyncio as aioredis

from app.config import settings
from app.telephony.call_router import is_office_open

logger = logging.getLogger(__name__)

_QUEUE_KEY = "callback_queue"
_MAX_RETRIES = 3
_RETRY_DELAY_HOURS = 1
_BETWEEN_CALLS_SLEEP = 15  # seconds between outbound dials


# ─── Background consumer ──────────────────────────────────────────────────────

async def callback_queue_loop(redis_url: str = "") -> None:
    """
    Blocking consumer — runs forever, designed to be run as an asyncio task.
    Call `asyncio.create_task(callback_queue_loop())` from the app lifespan.
    """
    url = redis_url or settings.redis_url
    redis_client = aioredis.from_url(url, decode_responses=True)
    logger.info("Callback queue consumer started")

    while True:
        try:
            item = await _pop_item(redis_client)
            if item is None:
                continue  # BRPOP timed out, loop again

            await _process_item(redis_client, item)
            await asyncio.sleep(_BETWEEN_CALLS_SLEEP)

        except asyncio.CancelledError:
            logger.info("Callback queue consumer shutting down")
            break
        except Exception as exc:
            logger.error(f"Callback queue consumer error: {exc}", exc_info=True)
            await asyncio.sleep(5)

    await redis_client.aclose()


async def _pop_item(redis_client: aioredis.Redis) -> Optional[dict]:
    """BRPOP with 5s timeout.  Returns the parsed payload or None on timeout."""
    result = await redis_client.brpop(_QUEUE_KEY, timeout=5)
    if result is None:
        return None
    _, raw = result
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error(f"Malformed callback queue item: {exc} — raw: {raw[:200]}")
        return None


# ─── Item processing ──────────────────────────────────────────────────────────

async def _process_item(redis_client: aioredis.Redis, item: dict) -> None:
    caller_number: str = item.get("caller_number", "")
    retries: int = int(item.get("retries", 0))
    language: str = item.get("language", "en")

    if not caller_number:
        logger.warning(f"Callback item missing caller_number: {item}")
        return

    # Guard: do not call outside office hours
    if not is_office_open():
        delay_min = _minutes_until_office_open()
        logger.info(
            f"Office closed — re-queuing callback for {caller_number} in {delay_min}min"
        )
        await _requeue(redis_client, item, delay_minutes=delay_min)
        return

    # Guard: too many retries
    if retries >= _MAX_RETRIES:
        logger.warning(f"Max retries reached for {caller_number} — creating GHL task")
        await _create_ghl_fallback_task(item)
        return

    # Place outbound call
    call_sid = await _place_outbound_call(caller_number, language, item)
    if call_sid:
        logger.info(f"Outbound callback placed: call_sid={call_sid} to={caller_number}")
    else:
        logger.warning(f"Outbound call failed for {caller_number} — re-queuing (retry {retries + 1})")
        item["retries"] = retries + 1
        await _requeue(redis_client, item, delay_minutes=_RETRY_DELAY_HOURS * 60)


# ─── Outbound call placement ──────────────────────────────────────────────────

async def _place_outbound_call(
    caller_number: str,
    language: str,
    item: dict,
) -> Optional[str]:
    """
    Place outbound call via Twilio REST.
    Returns call SID on success, None on failure.

    The call connects to /twilio/callback-connect which serves TwiML to
    rejoin the AI WebSocket.  Caller name + reason are passed as params.
    """
    name = item.get("caller_name", "")
    reason = item.get("reason", "callback request")
    ghl_contact_id = item.get("ghl_contact_id", "")

    # Build the callback-connect URL with context for the greeting
    cb_url = (
        f"https://{settings.base_host}/twilio/callback-connect"
        f"?lang={language}"
        f"&name={_url_encode(name)}"
        f"&reason={_url_encode(reason)}"
        f"&ghl_contact_id={ghl_contact_id}"
    )
    status_url = f"https://{settings.base_host}/twilio/call-status"

    try:
        loop = asyncio.get_event_loop()
        call_sid = await loop.run_in_executor(
            None,
            _create_call_sync,
            caller_number,
            cb_url,
            status_url,
        )
        return call_sid
    except Exception as exc:
        logger.error(f"Twilio outbound call error to {caller_number}: {exc}")
        return None


def _create_call_sync(to: str, url: str, status_url: str) -> str:
    from twilio.rest import Client as TwilioClient
    client = TwilioClient(settings.twilio_account_sid, settings.twilio_auth_token)
    call = client.calls.create(
        to=to,
        from_=settings.twilio_phone_number,
        url=url,
        status_callback=status_url,
        status_callback_method="POST",
        status_callback_event=["completed", "no-answer", "busy", "failed"],
        timeout=30,
        machine_detection="DetectMessageEnd",
    )
    return call.sid


# ─── Re-queue with delay ──────────────────────────────────────────────────────

async def _requeue(redis_client: aioredis.Redis, item: dict, delay_minutes: int = 60) -> None:
    """
    Push item back to queue after `delay_minutes`.
    Uses a scored sorted set `callback_delayed` to hold items until ready.
    A separate check loop promotes ready items back to the main queue.
    """
    due_at = (datetime.now(timezone.utc) + timedelta(minutes=delay_minutes)).timestamp()
    await redis_client.zadd("callback_delayed", {json.dumps(item): due_at})


async def promote_delayed_callbacks(redis_client: aioredis.Redis) -> int:
    """
    Move items from `callback_delayed` to `callback_queue` if their due time has passed.
    Called periodically by the consumer loop.
    Returns the number of items promoted.
    """
    now_ts = datetime.now(timezone.utc).timestamp()
    ready = await redis_client.zrangebyscore("callback_delayed", 0, now_ts)
    if not ready:
        return 0
    pipe = redis_client.pipeline()
    for raw in ready:
        pipe.lpush(_QUEUE_KEY, raw)
        pipe.zrem("callback_delayed", raw)
    await pipe.execute()
    return len(ready)


# ─── GHL fallback task ────────────────────────────────────────────────────────

async def _create_ghl_fallback_task(item: dict) -> None:
    """Create a GHL manual follow-up task after max retries exhausted."""
    try:
        from app.crm.ghl_client import get_ghl_client
        from datetime import date, timedelta
        ghl = get_ghl_client()
        caller = item.get("caller_number", "Unknown")
        ghl_contact_id = item.get("ghl_contact_id")

        if not ghl_contact_id:
            contacts = await ghl.search_contacts(phone=caller)
            if contacts:
                ghl_contact_id = contacts[0].get("id")

        if not ghl_contact_id:
            logger.warning(f"Cannot create GHL task — no contact for {caller}")
            return

        # Next business day
        d = date.today() + timedelta(days=1)
        while d.weekday() >= 5:
            d += timedelta(days=1)

        await ghl.create_task(
            contact_id=ghl_contact_id,
            title=f"Manual callback needed — {caller}",
            body=(
                f"Automated callback failed after {_MAX_RETRIES} attempts.\n"
                f"Reason: {item.get('reason', 'not specified')}\n"
                f"Requested at: {item.get('requested_at', 'unknown')}\n"
                f"Language: {item.get('language', 'en')}"
            ),
            due_date=d.isoformat(),
            assignee_id=getattr(settings, "ghl_default_assignee_id", None) or "",
        )
        logger.info(f"GHL fallback task created for {caller}")
    except Exception as exc:
        logger.error(f"Failed to create GHL fallback task: {exc}")


# ─── Utility ──────────────────────────────────────────────────────────────────

def _minutes_until_office_open() -> int:
    """Return minutes until next office open time (9:00 AM local Mon-Fri)."""
    from app.telephony.call_router import OFFICE_OPEN_HOUR, OFFICE_CLOSE_HOUR, OFFICE_TZ
    import zoneinfo
    tz = zoneinfo.ZoneInfo(OFFICE_TZ)
    now = datetime.now(tz)
    # Find next opening: today if before open, else tomorrow
    target = now.replace(hour=OFFICE_OPEN_HOUR, minute=0, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    # Skip weekends
    while target.weekday() >= 5:
        target += timedelta(days=1)
    delta = target - now
    return max(1, int(delta.total_seconds() / 60))


def _url_encode(text: str) -> str:
    from urllib.parse import quote_plus
    return quote_plus(text)


# ─── Public helper: enqueue a new callback request ───────────────────────────

async def enqueue_callback(
    redis_client: aioredis.Redis,
    caller_number: str,
    language: str = "en",
    caller_name: str = "",
    ghl_contact_id: str = "",
    reason: str = "callback request",
) -> None:
    """
    Add a new callback request to the queue.
    Called from the Twilio webhook handler when a caller presses the callback option.
    """
    payload = json.dumps({
        "caller_number": caller_number,
        "caller_name": caller_name,
        "ghl_contact_id": ghl_contact_id,
        "language": language,
        "reason": reason,
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "retries": 0,
    })
    await redis_client.lpush(_QUEUE_KEY, payload)
    logger.info(f"Callback enqueued for {caller_number}")
