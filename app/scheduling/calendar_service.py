"""
Calendar service — unified booking interface.

This is the public API that websocket_handler and the agent use to:
  1. Offer available time slots to the caller
  2. Book an appointment (dual-write: GHL + Google Calendar)
  3. Cancel / reschedule an appointment

Dual-write strategy:
  - GHL is the authoritative source for appointments (drives automations, reminders)
  - Google Calendar is for internal attorney visibility only
  - If GHL booking fails → no booking (surface error to agent)
  - If Google Calendar write fails → log warning only, GHL booking stands

Slot fetching:
  - Checks Redis slot cache first (1h TTL)
  - Falls back to GHL API if cache miss
  - Pre-fetches next 5 business days on first miss
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import settings
from app.crm.ghl_client import get_ghl_client, ghl_is_available
from app.scheduling.google_calendar import create_calendar_event
from app.scheduling.slot_cache import (
    cache_slots,
    get_cached_slots,
    invalidate_date,
    remove_slot,
    get_next_business_days,
)

logger = logging.getLogger(__name__)


# ─── Slot fetching ────────────────────────────────────────────────────────────

async def get_available_slots(
    days_ahead: int = 5,
    redis=None,
    force_fresh: bool = False,
) -> list[dict]:
    """
    Return a list of available appointment slots for the next `days_ahead` business days.
    Each slot: {startTime: ISO, endTime: ISO, display: "Mon Jan 6, 9:00 AM"}

    Uses cache when available (unless force_fresh=True); fetches from GHL API on miss.
    Pass force_fresh=True when presenting slots to a live caller to ensure real-time accuracy.
    """
    ghl = get_ghl_client()
    calendar_id = settings.ghl_calendar_id
    tz = settings.tz

    if not ghl_is_available():
        logger.warning("GHL unavailable — returning empty slot list.")
        return []

    business_days = get_next_business_days(n=days_ahead, tz=tz)
    all_slots: list[dict] = []

    for day in business_days:
        # Try cache first (skip on force_fresh to get real-time GHL data)
        cached = []
        if redis and not force_fresh:
            cached = await get_cached_slots(calendar_id, day, redis)

        if cached:
            # Ensure cached slots have display (older cached entries may lack it)
            for slot in cached:
                if not slot.get("display"):
                    slot["display"] = _format_slot_display(slot, tz)
            all_slots.extend(cached)
        else:
            fetched = await ghl.get_available_slots(
                start_date=day,
                end_date=day,
                timezone=settings.office_timezone,
            )
            # Annotate before caching so cached entries always carry display
            for slot in fetched:
                slot["display"] = _format_slot_display(slot, tz)
            if fetched and redis:
                await cache_slots(calendar_id, day, fetched, redis)
            all_slots.extend(fetched)

    return all_slots


def _format_slot_display(slot: dict, tz: ZoneInfo) -> str:
    """Return a human-readable string like 'Mon Jan 6, 9:00 AM'."""
    start_iso = slot.get("startTime") or slot.get("start_time", "")
    if not start_iso:
        return ""
    try:
        dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00")).astimezone(tz)
        return dt.strftime("%a %b %-d, %-I:%M %p")
    except Exception:
        return start_iso


def format_slots_for_speech(slots: list[dict], language: str = "en", max_slots: int = 3) -> str:
    """
    Format a short list of slots for the agent to read aloud.
    Returns a natural-language string, e.g.:
      "I have openings on Monday January 6th at 9 AM, Tuesday January 7th at 2 PM, ..."
    """
    if not slots:
        if language == "es":
            return "Actualmente no tenemos horarios disponibles esta semana. Puedo programarle para la siguiente semana."
        return "We don't have any openings this week. I can schedule you for next week."

    shown = slots[:max_slots]
    # Fall back to generating display from startTime if the key is missing
    displays = [
        s.get("display") or _format_slot_display(s, settings.tz)
        for s in shown
        if s.get("display") or s.get("startTime")
    ]

    if not displays:
        return ""

    if language == "es":
        if len(displays) == 1:
            return f"Tengo disponible el {displays[0]}."
        joined = ", ".join(displays[:-1]) + f" o {displays[-1]}"
        return f"Tengo disponibilidad el {joined}. ¿Cuál le funciona mejor?"
    else:
        if len(displays) == 1:
            return f"I have an opening on {displays[0]}."
        joined = ", ".join(displays[:-1]) + f", or {displays[-1]}"
        return f"I have openings on {joined}. Which works best for you?"


# ─── Booking ──────────────────────────────────────────────────────────────────

async def book_appointment(
    contact_id: str,
    slot: dict,
    caller_name: str = "",
    caller_email: str = "",
    case_type: str = "",
    language: str = "en",
    redis=None,
) -> dict | None:
    """
    Book an appointment for `contact_id` at the given `slot`.

    1. Calls GHL API to create appointment
    2. Calls Google Calendar to create mirror event
    3. Removes slot from Redis cache

    Returns the GHL appointment dict, or None on failure.
    """
    ghl = get_ghl_client()
    start_iso = slot.get("startTime") or slot.get("start_time", "")
    end_iso = slot.get("endTime") or slot.get("end_time", "")

    if not start_iso or not end_iso:
        logger.error("book_appointment: slot missing startTime/endTime")
        return None

    if not ghl_is_available():
        logger.warning(
            f"GHL unavailable — appointment for contact {contact_id} at {start_iso} "
            "could not be booked. Intake data is saved to Supabase."
        )
        return None

    # Build appointment title
    lang_label = "Spanish" if language == "es" else "English"
    case_note = f" — {case_type}" if case_type else ""
    title = f"Immigration Consultation ({lang_label}){case_note}"
    if caller_name:
        title = f"{caller_name} — {title}"

    notes = f"Booked via AI receptionist. Language: {lang_label}."
    if case_type:
        notes += f" Case type: {case_type}."

    # 1. GHL booking
    appt = await ghl.create_appointment(
        contact_id=contact_id,
        start_time=start_iso,
        end_time=end_iso,
        title=title,
        notes=notes,
        timezone=settings.office_timezone,
    )

    if not appt:
        logger.error(f"GHL appointment creation failed for contact {contact_id}")
        return None

    appt_id = appt.get("id") or appt.get("appointment_id", "")
    logger.info(f"GHL appointment booked: {appt_id} at {start_iso} for contact {contact_id}")

    # 2. Google Calendar (best effort)
    description = notes
    if caller_email:
        description += f"\nCallerEmail: {caller_email}"

    gcal_event_id = await create_calendar_event(
        summary=title,
        start_iso=start_iso,
        end_iso=end_iso,
        description=description,
        attendee_email=caller_email or "",
    )
    if gcal_event_id:
        logger.info(f"Google Calendar event created: {gcal_event_id}")
    else:
        logger.warning("Google Calendar event creation failed — GHL booking still stands")

    # 3. Invalidate the entire day's slot cache so concurrent callers get fresh data
    if redis:
        day = start_iso[:10]  # "YYYY-MM-DD"
        await invalidate_date(settings.ghl_calendar_id, day, redis)

    return appt


# ─── Cancellation ─────────────────────────────────────────────────────────────

async def cancel_appointment(appointment_id: str) -> bool:
    """Cancel an appointment in GHL. Returns True on success."""
    ghl = get_ghl_client()
    return await ghl.update_appointment_status(appointment_id, "cancelled")
