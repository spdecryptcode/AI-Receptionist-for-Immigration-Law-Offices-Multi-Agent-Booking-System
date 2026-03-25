"""
Google Calendar service account client.

Authentication uses a service account JSON key file (path from config).
The service account must be granted "Make changes to events" permission
on the immigration firm's Google Calendar.

We write appointments to Google Calendar as a secondary confirmation
alongside GHL (GHL is the source-of-truth for the CRM; Google Calendar
is for attorney visibility in G Suite / Gmail).

Library: google-api-python-client + google-auth (async via run_in_executor)
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

# Lazy import — google libraries are optional; if not installed, calendar write degrades gracefully
_google_available = False
_service = None

try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    _google_available = True
except ImportError:
    logger.warning(
        "google-api-python-client not installed — Google Calendar sync disabled. "
        "Add google-api-python-client google-auth to requirements.txt to enable."
    )


_SCOPES = ["https://www.googleapis.com/auth/calendar"]


def _build_service():
    """Build and return a Google Calendar service object (sync, called in executor)."""
    key_path = settings.google_service_account_key
    if not os.path.exists(key_path):
        logger.warning(f"Google service account key not found at {key_path}")
        return None

    creds = service_account.Credentials.from_service_account_file(
        key_path, scopes=_SCOPES
    )
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _get_service():
    global _service
    if _service is None and _google_available:
        _service = _build_service()
    return _service


# ─── Async wrappers ───────────────────────────────────────────────────────────

async def create_calendar_event(
    summary: str,
    start_iso: str,      # ISO 8601 with timezone offset
    end_iso: str,
    description: str = "",
    attendee_email: str = "",
    location: str = "",
) -> str | None:
    """
    Create a Google Calendar event. Returns event ID or None on failure.
    Runs in a thread executor to avoid blocking the event loop.
    """
    if not _google_available:
        return None

    loop = asyncio.get_event_loop()
    try:
        event_id = await loop.run_in_executor(
            None,
            _create_event_sync,
            summary,
            start_iso,
            end_iso,
            description,
            attendee_email,
            location,
        )
        return event_id
    except Exception as exc:
        logger.error(f"Google Calendar create_event failed: {exc}")
        return None


def _create_event_sync(
    summary: str,
    start_iso: str,
    end_iso: str,
    description: str,
    attendee_email: str,
    location: str,
) -> str | None:
    service = _get_service()
    if not service:
        return None

    event: dict[str, Any] = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": start_iso, "timeZone": settings.office_timezone},
        "end": {"dateTime": end_iso, "timeZone": settings.office_timezone},
    }
    if location:
        event["location"] = location
    if attendee_email:
        event["attendees"] = [{"email": attendee_email}]

    try:
        created = (
            service.events()
            .insert(calendarId=settings.google_calendar_id, body=event)
            .execute()
        )
        return created.get("id")
    except Exception as exc:
        logger.error(f"Google Calendar insert failed: {exc}")
        return None


async def cancel_calendar_event(event_id: str) -> bool:
    """Delete/cancel a Google Calendar event by ID."""
    if not _google_available:
        return False
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, _cancel_event_sync, event_id)
        return True
    except Exception as exc:
        logger.error(f"Google Calendar cancel_event ({event_id}) failed: {exc}")
        return False


def _cancel_event_sync(event_id: str) -> None:
    service = _get_service()
    if service:
        try:
            service.events().delete(
                calendarId=settings.google_calendar_id, eventId=event_id
            ).execute()
        except Exception as exc:
            logger.error(f"Google Calendar delete failed: {exc}")
