"""
Contact manager — bridges the CRM (GHL) with call state and Supabase DB.

Responsibilities:
  1. Inbound call lookup: check Redis cache → GHL API → Supabase
  2. Return caller name + GHL contact ID so agent can personalise greeting
  3. Call-end sync: push collected intake data to GHL + Supabase DB worker queue
  4. Tag management: apply lead-score and urgency tags to GHL contact
  5. GHL contact ID is cached in Redis at `ghl:phone:{normalised_phone}` (24h TTL)

This module is called from two places:
  - websocket_handler._run_call() at call start (lookup)
  - websocket_handler._finalize_call() at call end (sync)
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.config import settings
from app.crm.ghl_client import get_ghl_client
from app.voice.conversation_state import CallState, UrgencyLabel

logger = logging.getLogger(__name__)

# Redis TTL for phone→ghl_id cache
_PHONE_CACHE_TTL = 24 * 3600


# ─── Phone normalisation ──────────────────────────────────────────────────────

def normalise_phone(phone: str) -> str:
    """Strip everything except digits and leading +. Produces E.164-ish format."""
    digits = re.sub(r"[^\d+]", "", phone)
    if digits and not digits.startswith("+"):
        # Assume US number
        if len(digits) == 10:
            digits = "+1" + digits
        elif len(digits) == 11 and digits.startswith("1"):
            digits = "+" + digits
    return digits


# ─── Inbound lookup ───────────────────────────────────────────────────────────

async def lookup_caller(phone: str, redis) -> tuple[str | None, str | None]:
    """
    Look up a caller by phone number.

    Returns (caller_name, ghl_contact_id) — either or both may be None.

    Strategy:
      1. Check Redis cache (ghl:phone:{phone})
      2. Check GHL API
      3. Cache result on hit
    """
    normalised = normalise_phone(phone)
    cache_key = f"ghl:phone:{normalised}"

    # 1. Redis cache
    try:
        cached = await redis.get(cache_key)
        if cached:
            data = json.loads(cached)
            return data.get("name"), data.get("contact_id")
    except Exception as exc:
        logger.warning(f"ContactManager Redis lookup failed for {phone}: {exc}")

    # 2. GHL API
    try:
        ghl = get_ghl_client()
        contact = await ghl.search_contact_by_phone(normalised)
        if contact:
            first = contact.get("firstName") or contact.get("first_name", "")
            last = contact.get("lastName") or contact.get("last_name", "")
            name = f"{first} {last}".strip() or None
            contact_id = contact.get("id") or contact.get("contact_id", "")

            # Cache for next call
            try:
                await redis.setex(
                    cache_key,
                    _PHONE_CACHE_TTL,
                    json.dumps({"name": name, "contact_id": contact_id}),
                )
            except Exception:
                pass  # Cache miss is non-fatal

            logger.info(f"GHL contact found for {normalised}: id={contact_id} name={name!r}")
            return name, contact_id
    except Exception as exc:
        logger.warning(f"GHL lookup failed for {phone}: {exc}")

    return None, None


# ─── Call-end sync ────────────────────────────────────────────────────────────

async def sync_call_to_crm(
    state: CallState,
    ghl_contact_id: str | None,
    lead_score: int,
    redis,
) -> str | None:
    """
    After call ends: create/update GHL contact with intake data, apply tags.
    Also queues the full sync to Supabase via DB worker.

    Returns the GHL contact ID (created or existing).
    """
    ghl = get_ghl_client()
    intake = state.intake

    # Build tags
    tags = _build_tags(state, lead_score)

    # Build call notes for GHL
    call_notes = _build_call_notes(state, lead_score)

    if ghl_contact_id:
        # Update existing contact
        updates: dict[str, Any] = {}
        if intake.get("email"):
            updates["email"] = intake["email"]
        if intake.get("full_name"):
            parts = intake["full_name"].split(None, 1)
            updates["firstName"] = parts[0]
            if len(parts) > 1:
                updates["lastName"] = parts[1]

        # Map intake fields to GHL custom fields
        custom: dict[str, str] = {}
        _intake_to_custom(intake, custom)
        if custom:
            updates["customField"] = custom

        if updates:
            await ghl.update_contact(ghl_contact_id, updates)

        # Apply tags
        await ghl.add_tags(ghl_contact_id, tags)

        # Add call note
        await ghl.add_note(ghl_contact_id, call_notes)

        logger.info(f"[{state.call_sid}] GHL contact {ghl_contact_id} updated")
        result_id = ghl_contact_id

    else:
        # Create new contact
        phone = state.intake.get("phone") or ""
        # Get phone from call state
        first = ""
        last = ""
        if intake.get("full_name"):
            parts = intake["full_name"].split(None, 1)
            first = parts[0]
            last = parts[1] if len(parts) > 1 else ""

        custom: dict[str, str] = {}
        _intake_to_custom(intake, custom)

        contact = await ghl.create_contact(
            phone=phone,
            first_name=first,
            last_name=last,
            email=intake.get("email", ""),
            tags=tags,
            custom_fields=custom,
            language=state.language,
        )
        if contact:
            result_id = contact.get("id") or contact.get("contact_id", "")
            # Cache for this phone
            await ghl.add_note(result_id, call_notes)
            logger.info(f"[{state.call_sid}] GHL contact created: {result_id}")
        else:
            logger.error(f"[{state.call_sid}] Failed to create GHL contact")
            result_id = None

    # Queue Supabase DB sync
    await _queue_db_sync(state, result_id, lead_score, redis)

    return result_id


def _build_tags(state: CallState, lead_score: int) -> list[str]:
    """Generate GHL contact tags from call state."""
    tags: list[str] = ["ivr-lead"]

    # Lead score tier
    if lead_score >= 75:
        tags.append("lead-hot")
    elif lead_score >= 50:
        tags.append("lead-warm")
    else:
        tags.append("lead-cold")

    # Urgency
    if state.urgency_label == UrgencyLabel.EMERGENCY:
        tags.append("urgency-emergency")
    elif state.urgency_label == UrgencyLabel.HIGH:
        tags.append("urgency-high")

    # Case type
    case_type = state.intake.get("case_type", "").lower()
    if case_type:
        safe_tag = re.sub(r"[^a-z0-9-]", "-", case_type)[:30]
        tags.append(f"case-{safe_tag}")

    # Language
    if state.language == "es":
        tags.append("spanish-speaker")

    return tags


def _build_call_notes(state: CallState, lead_score: int) -> str:
    """Build a human-readable call note for GHL."""
    lines = [
        f"AI Intake Call — {state.call_sid}",
        f"Language: {state.language.upper()}",
        f"Lead Score: {lead_score}/100",
        f"Urgency: {state.urgency_label.value} ({state.urgency_score}/10)",
    ]
    if state.intake:
        lines.append("\nCollected intake:")
        for k, v in state.intake.items():
            if v:
                lines.append(f"  {k}: {v}")
    if state.scheduled_at:
        lines.append(f"\nAppointment booked: {state.scheduled_at}")
    return "\n".join(lines)


def _intake_to_custom(intake: dict, out: dict) -> None:
    """Map intake fields → GHL custom field names (firm-specific keys)."""
    _FIELD_MAP = {
        "current_immigration_status": "immigration_status",
        "case_type": "case_type",
        "country_of_birth": "country_of_birth",
        "nationality": "nationality",
        "entry_date_us": "us_entry_date",
        "prior_applications": "prior_filings",
        "has_attorney": "has_current_attorney",
        "preferred_language": "preferred_language",
        "preferred_contact_time": "preferred_contact_time",
    }
    for intake_key, ghl_key in _FIELD_MAP.items():
        val = intake.get(intake_key)
        if val is not None:
            out[ghl_key] = str(val)


async def _queue_db_sync(
    state: CallState,
    ghl_contact_id: str | None,
    lead_score: int,
    redis,
) -> None:
    """Push full intake + call data to Supabase DB worker queue."""
    payload = {
        "call_sid": state.call_sid,
        "ghl_contact_id": ghl_contact_id,
        "language": state.language,
        "lead_score": lead_score,
        "urgency_score": state.urgency_score,
        "urgency_label": state.urgency_label.value,
        "intake": state.intake,
        "scheduled_at": state.scheduled_at,
        "appointment_id": state.appointment_id,
        "transferred_at": state.transferred_at,
    }
    try:
        await redis.rpush("db_sync_queue", json.dumps(payload, default=str))
    except Exception as exc:
        logger.error(f"[{state.call_sid}] Failed to queue DB sync: {exc}")
