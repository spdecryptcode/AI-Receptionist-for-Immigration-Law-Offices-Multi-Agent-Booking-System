"""
Background DB persistence worker — drains Redis queues to Supabase/PostgreSQL.

Consumes these Redis list queues:
  - db_sync_queue        → clients + immigration_intakes + call_logs tables
  - lead_score_queue     → lead_scores table
  - analytics_events     → call_logs event rows
  - urgency_alerts       → urgency_alerts table
  - follow_up_queue      → no-show SMS follow-up via GHL + Twilio
  - twilio_sms_queue     → Twilio REST SMS fallback

The worker is a long-running asyncio task started from main.py lifespan.
It processes each queue in round-robin, sleeping briefly between iterations.

Design notes:
  - Uses BLPOP (blocking pop) with 1s timeout to avoid tight polling
  - Each item is processed in a try/except — failures are logged but don't
    stop the worker (dead-letter to Redis list `dlq:{queue}` for inspection)
  - Supabase client is used for upserts; SQLAlchemy is reserved for complex queries
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import settings
from app.dependencies import get_redis_client, get_supabase_client

logger = logging.getLogger(__name__)

_QUEUES = [
    "db_sync_queue",
    "lead_score_queue",
    "analytics_events",
    "urgency_alerts",
    "twilio_sms_queue",
    "voicemail_log_queue",
    "audit_log_queue",
    "follow_up_queue",
]

_WORKER_SLEEP = 0.1  # seconds between queue drains


# ─── Worker entrypoint ────────────────────────────────────────────────────────

async def db_worker_loop() -> None:
    """
    Long-running coroutine. Start with asyncio.create_task(db_worker_loop()).
    Drains the Redis queues and persists items to Supabase.
    """
    logger.info("DB persistence worker started")
    redis = get_redis_client()

    while True:
        try:
            for queue in _QUEUES:
                item = await redis.lpop(queue)
                if item:
                    await _dispatch(queue, item, redis)
            await asyncio.sleep(_WORKER_SLEEP)
        except asyncio.CancelledError:
            logger.info("DB worker cancelled — shutting down")
            raise
        except Exception as exc:
            logger.error(f"DB worker loop error: {exc}", exc_info=True)
            await asyncio.sleep(1)  # Backoff on unexpected errors


# ─── Dispatcher ───────────────────────────────────────────────────────────────

async def _dispatch(queue: str, raw: str, redis) -> None:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        logger.error(f"DB worker: invalid JSON in queue {queue}: {raw[:200]}")
        return

    try:
        msg_type = payload.get("type", "")

        if queue == "db_sync_queue" and msg_type == "conversation_message":
            await _handle_conversation_message(payload)
        elif queue == "db_sync_queue" and msg_type == "call_summary":
            await _handle_call_summary(payload)
        elif queue == "db_sync_queue" and msg_type == "call_cost":
            await _handle_call_cost(payload)
        elif queue == "db_sync_queue":
            await _handle_db_sync(payload)
        elif queue == "lead_score_queue":
            await _handle_lead_score(payload)
        elif queue == "analytics_events":
            await _handle_analytics_event(payload)
        elif queue == "urgency_alerts":
            await _handle_urgency_alert(payload)
        elif queue == "twilio_sms_queue":
            await _handle_twilio_sms(payload)
        elif queue == "voicemail_log_queue":
            await _handle_voicemail_log(payload)
        elif queue == "audit_log_queue":
            await _handle_audit_log(payload)
        elif queue == "follow_up_queue":
            await _handle_follow_up(payload)
    except Exception as exc:
        logger.error(f"DB worker: error processing {queue} item: {exc}", exc_info=True)
        # Dead-letter the item for manual inspection
        try:
            await redis.rpush(f"dlq:{queue}", raw)
        except Exception:
            pass


# ─── Handlers ─────────────────────────────────────────────────────────────────

async def _handle_db_sync(payload: dict) -> None:
    """
    Upsert call data to Supabase:
      - clients (or update if existing)
      - conversations
      - immigration_intakes
    """
    supabase = get_supabase_client()
    call_sid = payload.get("call_sid", "")
    intake = payload.get("intake", {})

    # 1. Resolve caller phone (intake fields, then Redis fallback)
    phone = intake.get("phone", "") or intake.get("caller_phone", "")
    if not phone:
        try:
            redis_client = get_redis_client()
            phone = await redis_client.hget(f"call:{call_sid}", "from") or ""
            if isinstance(phone, bytes):
                phone = phone.decode("utf-8")
        except Exception:
            pass

    # Fetch call duration — prefer Redis (set by status callback), fall back to payload
    duration_seconds = payload.get("duration_seconds") or None
    try:
        dur = await get_redis_client().hget(f"call:{call_sid}", "duration_seconds")
        if dur:
            duration_seconds = int(dur)
    except Exception:
        pass

    # Derive call outcome
    call_outcome = payload.get("call_outcome") or None
    if not call_outcome:
        if payload.get("transferred_at"):
            call_outcome = "transferred_to_staff"
        elif payload.get("scheduled_at"):
            call_outcome = "booking_made"
    # If still unknown, read the Twilio CallStatus from Redis to infer outcome
    if not call_outcome and call_sid:
        try:
            redis_client = get_redis_client()
            twilio_status = await redis_client.hget(f"call:{call_sid}", "call_status")
            if twilio_status:
                if isinstance(twilio_status, bytes):
                    twilio_status = twilio_status.decode()
                if twilio_status in ("no-answer", "busy"):
                    call_outcome = "callback_requested"
                elif twilio_status == "completed":
                    # Distinguish info-only from dropped based on turn count
                    turn_raw = await redis_client.hget(f"call:{call_sid}", "turn_count")
                    turns = int(turn_raw) if turn_raw else 0
                    call_outcome = "info_only" if turns > 2 else "dropped"
        except Exception:
            pass

    # 2. Upsert conversation record (enriched for Supabase-only dashboard)
    conv_data: dict[str, Any] = {
        "call_sid": call_sid,
        "language_detected": payload.get("language", "en"),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "channel": "phone",
    }
    # Only write score fields if this is a real payload (lead_score >= 0).
    # Skeleton/safety-net payloads use lead_score=-1 as a sentinel — they must
    # never overwrite real scores already written by _finalize_call.
    if payload.get("lead_score", -1) >= 0:
        conv_data["lead_score"] = payload["lead_score"]
        conv_data["urgency_score"] = payload.get("urgency_score", 0)
        conv_data["urgency_label"] = payload.get("urgency_label", "low")
    # Only write appointment/transfer fields when present — never overwrite with NULL
    # (skeleton and safety-net payloads don't carry these, and would clobber real data)
    if payload.get("scheduled_at"):
        conv_data["scheduled_at"] = payload["scheduled_at"]
    if payload.get("appointment_id"):
        conv_data["appointment_id"] = payload["appointment_id"]
    if payload.get("transferred_at"):
        conv_data["transferred_at"] = payload["transferred_at"]
    if phone:
        conv_data["caller_phone"] = phone
    if intake.get("full_name"):
        conv_data["caller_name"] = intake["full_name"]
    elif intake.get("first_name") or intake.get("last_name"):
        conv_data["caller_name"] = " ".join(
            filter(None, [intake.get("first_name"), intake.get("last_name")])
        ).strip() or None
    if call_outcome:
        conv_data["call_outcome"] = call_outcome
    if duration_seconds is not None:
        conv_data["duration_seconds"] = duration_seconds
        conv_data["started_at"] = (
            datetime.now(timezone.utc) - timedelta(seconds=duration_seconds)
        ).isoformat()

    try:
        supabase.table("conversations").upsert(
            conv_data, on_conflict="call_sid"
        ).execute()
        logger.debug(f"[{call_sid}] Conversation upserted to Supabase")
    except Exception as exc:
        logger.error(f"[{call_sid}] Supabase conversation upsert failed: {exc}")
        raise

    # 3. Insert immigration intake data
    if intake:
        # Resolve full_name from parts if top-level key missing
        _resolved_full_name = (
            intake.get("full_name")
            or " ".join(filter(None, [intake.get("first_name"), intake.get("last_name")])).strip()
            or None
        )
        intake_row: dict[str, Any] = {
            "call_sid": call_sid,
            "caller_phone": phone or None,
            "full_name": _resolved_full_name,
            "country_of_birth": intake.get("country_of_birth"),
            "nationality": intake.get("nationality"),
            "current_immigration_status": intake.get("current_immigration_status"),
            "case_type": intake.get("case_type"),
            "entry_date_us": _safe_date(intake.get("entry_date_us")),
            "prior_applications": _bool_str(intake.get("prior_applications")),
            "has_attorney": _bool_str(intake.get("has_attorney")),
            "urgency_reason": intake.get("urgency_reason"),
            "preferred_language": intake.get("preferred_language", payload.get("language", "en")),
            "preferred_contact_time": intake.get("preferred_contact_time"),
            "email": _safe_email(intake.get("email")),
            "prior_deportation": _bool_str(intake.get("prior_deportation")),
            # Accept both current name and legacy alias from old extraction prompt
            "criminal_history": _bool_str(
                intake.get("criminal_history") if intake.get("criminal_history") is not None
                else intake.get("has_criminal_record")
            ),
            "employer_sponsor": _bool_str(intake.get("employer_sponsor")),
            # Accept both current name and legacy alias
            "family_in_us": _bool_str(
                intake.get("family_in_us") if intake.get("family_in_us") is not None
                else intake.get("us_family_connections")
            ),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        # Remove None values to avoid overwriting existing data
        intake_row = {k: v for k, v in intake_row.items() if v is not None}
        try:
            supabase.table("immigration_intakes").upsert(
                intake_row, on_conflict="call_sid"
            ).execute()
            logger.debug(f"[{call_sid}] Immigration intake upserted to Supabase")
        except Exception as exc:
            logger.error(f"[{call_sid}] Supabase intake upsert failed: {exc}")
            raise


async def _handle_lead_score(payload: dict) -> None:
    """Persist lead score breakdown to lead_scores table."""
    supabase = get_supabase_client()
    call_sid = payload.get("call_sid", "")

    row = {
        "call_sid": call_sid,
        "total_score": payload.get("total", 0),
        "case_value_score": payload.get("case_value", 0),
        "urgency_score": payload.get("urgency", 0),
        "booking_readiness_score": payload.get("booking_readiness", 0),
        "data_completeness_score": payload.get("data_completeness", 0),
        "top_signals": payload.get("top_signals", []),
        "recommended_follow_up": payload.get("recommended_follow_up", "next_day"),
        "recommended_attorney_tier": payload.get("recommended_attorney_tier", "associate"),
        "notes": payload.get("notes", ""),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        supabase.table("lead_scores").upsert(row, on_conflict="call_sid").execute()
        logger.debug(f"[{call_sid}] Lead score persisted to Supabase")
    except Exception as exc:
        logger.error(f"[{call_sid}] Lead score persist failed: {exc}")
        raise


async def _handle_analytics_event(payload: dict) -> None:
    """Append analytics event to call_logs table."""
    supabase = get_supabase_client()
    row = {
        "call_sid": payload.get("call_sid", ""),
        "event_type": payload.get("event", "unknown"),
        "phase": payload.get("phase"),
        "latency_ms": payload.get("latency_ms"),
        "metadata": {k: v for k, v in payload.items()
                     if k not in ("call_sid", "event", "phase", "latency_ms", "ts")},
        "created_at": payload.get("ts") or datetime.now(timezone.utc).isoformat(),
    }
    try:
        supabase.table("call_logs").insert(row).execute()
    except Exception as exc:
        # Analytics failures are non-critical — warn only
        logger.warning(f"Analytics event persist failed: {exc}")


async def _handle_urgency_alert(payload: dict) -> None:
    """Persist urgency alert to urgency_alerts table."""
    supabase = get_supabase_client()
    call_sid = payload.get("call_sid", "")

    row = {
        "call_sid": call_sid,
        "urgency_score": payload.get("urgency_score", 0),
        "urgency_label": payload.get("urgency_label", "medium"),
        "factors": payload.get("factors", []),
        "recommended_action": payload.get("recommended_action", "expedite_consultation"),
        "alerted_at": datetime.now(timezone.utc).isoformat(),
        "resolved": False,
    }
    try:
        supabase.table("urgency_alerts").insert(row).execute()
        logger.info(f"[{call_sid}] Urgency alert persisted to Supabase")
    except Exception as exc:
        logger.error(f"[{call_sid}] Urgency alert persist failed: {exc}")
        raise


async def _handle_twilio_sms(payload: dict) -> None:
    """Send SMS via Twilio REST API as a fallback when GHL SMS fails."""
    contact_id = payload.get("contact_id", "")
    message = payload.get("message", "")

    if not message:
        return

    # We need the phone number — look it up from GHL
    try:
        from app.crm.ghl_client import get_ghl_client
        ghl = get_ghl_client()
        contact = await ghl.get_contact(contact_id)
        if not contact:
            logger.warning(f"Twilio SMS fallback: contact {contact_id} not found")
            return

        phone = contact.get("phone") or contact.get("phoneRaw") or ""
        if not phone:
            logger.warning(f"Twilio SMS fallback: no phone for contact {contact_id}")
            return

        from twilio.rest import Client as TwilioClient
        # Run sync Twilio client in executor
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            _send_twilio_sms_sync,
            phone,
            message,
        )
    except Exception as exc:
        logger.error(f"Twilio SMS fallback failed for contact {contact_id}: {exc}")


def _send_twilio_sms_sync(to_phone: str, message: str) -> None:
    from twilio.rest import Client as TwilioClient
    client = TwilioClient(settings.twilio_account_sid, settings.twilio_auth_token)
    client.messages.create(
        to=to_phone,
        from_=settings.twilio_phone_number,
        body=message,
    )
    logger.info(f"Twilio SMS sent to {to_phone}")


# ─── Helpers ──────────────────────────────────────────────────────────────────

async def _handle_conversation_message(payload: dict) -> None:
    """Persist one per-turn conversation message to conversation_messages table."""
    supabase = get_supabase_client()
    row = {
        "call_sid": payload.get("call_sid", ""),
        "turn_index": payload.get("turn_index", 0),
        "role": payload.get("role", "user"),
        "content": payload.get("text", "")[:4000],
        "latency_ms": payload.get("latency_ms"),
        "phase": payload.get("phase", ""),
        "intent": payload.get("intent", ""),
        "created_at": datetime.fromtimestamp(
            payload["ts"] / 1000, tz=timezone.utc
        ).isoformat() if payload.get("ts") else datetime.now(timezone.utc).isoformat(),
    }
    try:
        supabase.table("conversation_messages").insert(row).execute()
    except Exception as exc:
        logger.error(f"conversation_message persist failed: {exc}")
        raise


async def _handle_call_summary(payload: dict) -> None:
    """Update call_logs with post-call summary, sentiment and structured data."""
    supabase = get_supabase_client()
    call_sid = payload.get("call_sid", "")
    update = {
        "ai_summary": payload.get("summary", ""),
        "sentiment_score": payload.get("sentiment_score"),
        "sentiment_label": payload.get("sentiment_label", ""),
        "frustration_detected": payload.get("frustration_detected", False),
        "duration_seconds": payload.get("duration_sec"),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    # Remove None values
    update = {k: v for k, v in update.items() if v is not None}
    try:
        supabase.table("call_logs").update(update).eq("call_sid", call_sid).execute()
        logger.debug(f"[{call_sid}] Call summary updated in Supabase")
    except Exception as exc:
        logger.error(f"[{call_sid}] Call summary update failed: {exc}")
        raise

    # If structured data extracted a name, back-fill conversations.caller_name
    structured = payload.get("structured", {})
    ai_name = structured.get("full_name") if isinstance(structured, dict) else None
    if ai_name:
        try:
            supabase.table("conversations").update(
                {"caller_name": ai_name, "updated_at": datetime.now(timezone.utc).isoformat()}
            ).eq("call_sid", call_sid).is_("caller_name", "null").execute()
        except Exception as exc:
            logger.warning(f"[{call_sid}] conversations caller_name back-fill failed: {exc}")


async def _handle_voicemail_log(payload: dict) -> None:
    """Persist voicemail processing audit row to voicemails table."""
    supabase = get_supabase_client()
    row = {
        "call_sid": payload.get("call_sid", ""),
        "recording_sid": payload.get("recording_sid", ""),
        "caller_phone": payload.get("caller_number", ""),
        "transcription": payload.get("transcript", "")[:4000],
        "summary": payload.get("summary", ""),
        "ghl_task_id": payload.get("ghl_task_id", ""),
        "is_emergency": payload.get("is_emergency", False),
        "status": payload.get("status", "processed"),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        supabase.table("voicemails").insert(row).execute()
        logger.debug(f"Voicemail log persisted: {row['recording_sid']}")
    except Exception as exc:
        logger.warning(f"Voicemail log persist failed (non-critical): {exc}")


async def _handle_call_cost(payload: dict) -> None:
    """Update call_logs.cost_usd with the final per-call AI cost USD."""
    supabase = get_supabase_client()
    call_sid = payload.get("call_sid", "")
    cost_usd = payload.get("cost_usd", 0.0)
    try:
        supabase.table("call_logs").update(
            {"cost_usd": cost_usd, "updated_at": datetime.now(timezone.utc).isoformat()}
        ).eq("call_sid", call_sid).execute()
        logger.debug(f"[{call_sid}] cost_usd updated to ${cost_usd:.5f}")
    except Exception as exc:
        logger.warning(f"[{call_sid}] call_cost persist failed (non-critical): {exc}")


async def _handle_audit_log(payload: dict) -> None:
    """Persist HTTP audit log entry to audit_log table."""
    supabase = get_supabase_client()
    row = {
        "method": payload.get("method", ""),
        "path": payload.get("path", ""),
        "query": payload.get("query", ""),
        "status_code": payload.get("status_code", 0),
        "ip": payload.get("ip", ""),
        "user_agent": payload.get("user_agent", "")[:200],
        "duration_ms": payload.get("duration_ms", 0),
        "created_at": datetime.fromtimestamp(
            payload["ts"] / 1000, tz=timezone.utc
        ).isoformat() if payload.get("ts") else datetime.now(timezone.utc).isoformat(),
    }
    try:
        supabase.table("audit_log").insert(row).execute()
    except Exception as exc:
        logger.debug(f"Audit log persist failed (non-critical): {exc}")


async def _handle_follow_up(payload: dict) -> None:
    """
    Handle no-show follow-up: check appointment status in GHL, then send an SMS
    if the appointment is still in a no-show / unconfirmed state.
    """
    contact_id = payload.get("contact_id", "")
    appointment_id = payload.get("appointment_id", "")
    if not contact_id:
        logger.warning("follow_up: missing contact_id, skipping")
        return

    try:
        from app.crm.ghl_client import get_ghl_client
        ghl = get_ghl_client()

        # Fetch appointment to check current status
        status = None
        if appointment_id:
            appt = await ghl.get_appointment(appointment_id)
            status = (appt or {}).get("status", "")

        # Only follow up if appointment is still marked as no-show or unconfirmed
        if status and status.lower() not in ("noshow", "no_show", "unconfirmed", ""):
            logger.info(f"follow_up: appointment {appointment_id} status={status} — skipping SMS")
            return

        # Fetch contact for phone + name
        contact = await ghl.get_contact(contact_id)
        if not contact:
            logger.warning(f"follow_up: contact {contact_id} not found")
            return

        phone = contact.get("phone") or contact.get("phoneRaw", "")
        name = contact.get("firstName") or contact.get("name", "")
        name_part = f", {name}" if name else ""
        firm = settings.law_firm_name

        msg = (
            f"Hi{name_part}! We noticed you missed your consultation with {firm}. "
            f"We'd love to reschedule — please call us or visit {settings.booking_url} "
            f"to pick a new time. We're here to help."
        )

        if phone:
            from twilio.rest import Client as TwilioClient
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                _send_twilio_sms_sync,
                phone,
                msg,
            )
            logger.info(f"follow_up SMS sent to contact {contact_id}")

    except Exception as exc:
        logger.error(f"follow_up handler error for contact {contact_id}: {exc}", exc_info=True)
        raise


def _bool_str(value: Any) -> bool | None:
    """Convert "yes"/"no"/bool to Python bool or None."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    s = str(value).lower().strip()
    if s in ("yes", "true", "1"):
        return True
    if s in ("no", "false", "0"):
        return False
    return None


def _safe_date(value: Any) -> str | None:
    """Return ISO date string (YYYY-MM-DD) only if value matches a date pattern."""
    import re
    if not value or not isinstance(value, str):
        return None
    v = value.strip()
    # Accept YYYY-MM-DD strictly (PostgreSQL date type)
    if re.match(r'^\d{4}-\d{2}-\d{2}$', v):
        return v
    # Accept common alternate formats (MM/DD/YYYY, DD/MM/YYYY) and normalise
    m = re.match(r'^(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})$', v)
    if m:
        return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return None


def _safe_email(value: Any) -> str | None:
    """Return value only if it looks like a valid email address."""
    import re
    if not value or not isinstance(value, str):
        return None
    v = value.strip()
    if re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', v):
        return v
    return None
