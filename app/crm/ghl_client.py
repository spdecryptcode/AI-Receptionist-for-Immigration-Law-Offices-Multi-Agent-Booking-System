"""
GoHighLevel (GHL) REST API client — v2 (Private Integration Token).

Rate limits:
  - GHL API v2: 100 requests/minute per location
  - This client implements a token-bucket rate limiter (100 req/min = 1.67/sec)

All requests use shared HTTP/2 client from dependencies.
Responses are parsed to dicts — no Pydantic models to keep the layer thin.

API base: https://services.leadconnectorhq.com  (GHL v2, requires pit- token)
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import time
from typing import Any

import httpx

from app.config import settings
from app.dependencies import get_http2_client

logger = logging.getLogger(__name__)

_GHL_BASE = "https://services.leadconnectorhq.com"

# Default appointment duration — used to compute endTime from the v2 free-slots
# response (which only provides start times).  Override via GHL calendar settings.
_DEFAULT_SLOT_DURATION_MINUTES = 60

# ─── Credential circuit breaker ──────────────────────────────────────────────
# After a 401/403 response the circuit opens and all subsequent GHL calls are
# short-circuited immediately (returning None / False / []).  The circuit
# re-closes automatically after _CRED_RETRY_AFTER seconds so the system
# self-heals if credentials are rotated without a restart.
_CRED_RETRY_AFTER = 300  # seconds (5 min)

_creds_ok: bool = True                  # False once a 401/403 is seen
_creds_failed_at: float | None = None   # monotonic timestamp of first failure


def _mark_creds_failed() -> None:
    """Open the circuit breaker after an auth error."""
    global _creds_ok, _creds_failed_at
    if _creds_ok:
        _creds_failed_at = time.monotonic()
        _creds_ok = False
        logger.error(
            "GHL credentials rejected (401/403). All GHL calls will be "
            "short-circuited for %d s. Check GHL_API_KEY / GHL_LOCATION_ID.",
            _CRED_RETRY_AFTER,
        )


def _creds_available() -> bool:
    """Return True when GHL calls should proceed."""
    global _creds_ok, _creds_failed_at
    if _creds_ok:
        return True
    # Auto-reset after the retry window so the system self-heals
    if _creds_failed_at is not None and (time.monotonic() - _creds_failed_at) >= _CRED_RETRY_AFTER:
        _creds_ok = True
        _creds_failed_at = None
        logger.info("GHL credential circuit breaker reset — retrying.")
        return True
    return False


def ghl_is_available() -> bool:
    """Public accessor used by health-check and calendar service."""
    return _creds_available()


# ─── Rate limiter: 100 req/min ────────────────────────────────────────────────
_RATE_LIMIT = 100          # requests per window
_RATE_WINDOW = 60.0        # seconds
_MIN_INTERVAL = _RATE_WINDOW / _RATE_LIMIT   # 0.6 s between requests


class _TokenBucket:
    """Simple async token bucket — allows burst up to _RATE_LIMIT then throttles."""

    def __init__(self, rate: float, capacity: float):
        self._rate = rate         # tokens added per second
        self._capacity = capacity
        self._tokens = capacity
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
            self._last_refill = now
            if self._tokens < 1:
                sleep_for = (1 - self._tokens) / self._rate
                await asyncio.sleep(sleep_for)
                self._tokens = 0
            else:
                self._tokens -= 1


_rate_bucket = _TokenBucket(rate=_RATE_LIMIT / _RATE_WINDOW, capacity=_RATE_LIMIT)


# ─── Client class ─────────────────────────────────────────────────────────────

class GHLClient:
    """
    Async GoHighLevel API client.
    Instantiate once and reuse (uses shared httpx session).
    """

    def __init__(self) -> None:
        self._api_key = settings.ghl_api_key
        self._location_id = settings.ghl_location_id
        self._calendar_id = settings.ghl_calendar_id
        self._http: httpx.AsyncClient = get_http2_client()

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Version": "2021-07-28",
        }

    async def _get(self, path: str, params: dict | None = None) -> dict[str, Any]:
        if not _creds_available():
            raise httpx.HTTPStatusError(
                "GHL credentials unavailable (circuit open)",
                request=None,  # type: ignore[arg-type]
                response=None,  # type: ignore[arg-type]
            )
        await _rate_bucket.acquire()
        url = f"{_GHL_BASE}{path}"
        resp = await self._http.get(url, headers=self._headers(), params=params or {})
        if resp.status_code in (401, 403):
            _mark_creds_failed()
        resp.raise_for_status()
        return resp.json()

    async def _post(self, path: str, payload: dict) -> dict[str, Any]:
        if not _creds_available():
            raise httpx.HTTPStatusError(
                "GHL credentials unavailable (circuit open)",
                request=None,  # type: ignore[arg-type]
                response=None,  # type: ignore[arg-type]
            )
        await _rate_bucket.acquire()
        url = f"{_GHL_BASE}{path}"
        resp = await self._http.post(url, headers=self._headers(), json=payload)
        if resp.status_code in (401, 403):
            _mark_creds_failed()
        resp.raise_for_status()
        return resp.json()

    async def _put(self, path: str, payload: dict) -> dict[str, Any]:
        if not _creds_available():
            raise httpx.HTTPStatusError(
                "GHL credentials unavailable (circuit open)",
                request=None,  # type: ignore[arg-type]
                response=None,  # type: ignore[arg-type]
            )
        await _rate_bucket.acquire()
        url = f"{_GHL_BASE}{path}"
        resp = await self._http.put(url, headers=self._headers(), json=payload)
        if resp.status_code in (401, 403):
            _mark_creds_failed()
        resp.raise_for_status()
        return resp.json()

    # ─── Contact endpoints ────────────────────────────────────────────────────

    async def search_contact_by_phone(self, phone: str) -> dict | None:
        """
        Search for an existing GHL contact by phone number.
        Returns the first match or None.
        """
        # Normalise phone: GHL expects E.164 without the + sign in search
        normalised = phone.lstrip("+").replace(" ", "").replace("-", "")
        try:
            data = await self._get("/contacts/", params={"locationId": self._location_id, "query": phone})
            contacts = data.get("contacts", [])
            if contacts:
                return contacts[0]
            # Retry with normalised form if original failed
            if normalised != phone:
                data2 = await self._get("/contacts/", params={"locationId": self._location_id, "query": normalised})
                contacts2 = data2.get("contacts", [])
                if contacts2:
                    return contacts2[0]
        except httpx.HTTPStatusError as exc:
            logger.warning(f"GHL search_contact_by_phone ({phone}): HTTP {exc.response.status_code}")
        return None

    async def get_contact(self, contact_id: str) -> dict | None:
        """Fetch a contact by ID."""
        try:
            data = await self._get(f"/contacts/{contact_id}")
            return data.get("contact") or data
        except httpx.HTTPStatusError as exc:
            logger.warning(f"GHL get_contact ({contact_id}): HTTP {exc.response.status_code}")
            return None

    async def create_contact(
        self,
        phone: str,
        first_name: str = "",
        last_name: str = "",
        email: str = "",
        tags: list[str] | None = None,
        custom_fields: dict | None = None,
        language: str = "en",
    ) -> dict | None:
        """
        Create a new GHL contact. Returns the created contact dict or None on failure.
        """
        payload: dict[str, Any] = {
            "locationId": self._location_id,
            "phone": phone,
            "firstName": first_name,
            "lastName": last_name,
        }
        if email:
            payload["email"] = email
        if tags:
            payload["tags"] = tags
        if language:
            payload["customField"] = custom_fields or {}
            payload["customField"]["preferred_language"] = language

        try:
            data = await self._post("/contacts/", payload)
            return data.get("contact") or data
        except httpx.HTTPStatusError as exc:
            logger.error(f"GHL create_contact: HTTP {exc.response.status_code} — {exc.response.text[:200]}")
            return None

    async def update_contact(
        self,
        contact_id: str,
        updates: dict[str, Any],
    ) -> dict | None:
        """
        Update an existing GHL contact with arbitrary field updates.
        `updates` keys should match GHL contact field names.
        """
        try:
            data = await self._put(f"/contacts/{contact_id}", updates)
            return data.get("contact") or data
        except httpx.HTTPStatusError as exc:
            logger.error(
                f"GHL update_contact ({contact_id}): HTTP {exc.response.status_code} — "
                f"{exc.response.text[:200]}"
            )
            return None

    async def add_tags(self, contact_id: str, tags: list[str]) -> bool:
        """Append tags to a contact without overwriting existing tags."""
        contact = await self.get_contact(contact_id)
        if not contact:
            return False
        existing = contact.get("tags", []) or []
        merged = list(set(existing + tags))
        result = await self.update_contact(contact_id, {"tags": merged})
        return result is not None

    async def add_note(self, contact_id: str, body: str) -> dict | None:
        """Create a note on a contact."""
        try:
            data = await self._post(f"/contacts/{contact_id}/notes", {"body": body})
            return data
        except httpx.HTTPStatusError as exc:
            logger.warning(f"GHL add_note ({contact_id}): HTTP {exc.response.status_code}")
            return None

    # ─── Appointment endpoints ─────────────────────────────────────────────────

    async def get_available_slots(
        self,
        start_date: str,  # "YYYY-MM-DD"
        end_date: str,    # "YYYY-MM-DD"
        timezone: str | None = None,
    ) -> list[dict]:
        """
        Fetch available calendar slots using the GHL v2 free-slots endpoint.

        GHL v2 requires epoch-millisecond timestamps and returns:
          {"YYYY-MM-DD": {"slots": ["ISO_start", ...]}, ...}

        Returns list of {startTime: ISO, endTime: ISO} dicts.
        """
        tz_str = timezone or settings.office_timezone
        # Convert YYYY-MM-DD → epoch ms (midnight UTC so all timezones are covered)
        s_day = _dt.date.fromisoformat(start_date)
        e_day = _dt.date.fromisoformat(end_date)
        start_ms = int(_dt.datetime(s_day.year, s_day.month, s_day.day,
                                    tzinfo=_dt.timezone.utc).timestamp() * 1000)
        end_ms = int(_dt.datetime(e_day.year, e_day.month, e_day.day, 23, 59, 59,
                                  tzinfo=_dt.timezone.utc).timestamp() * 1000)
        params = {
            "startDate": start_ms,
            "endDate": end_ms,
            "timezone": tz_str,
        }
        try:
            data = await self._get(f"/calendars/{self._calendar_id}/free-slots", params=params)
            # Parse v2 response into {startTime, endTime} dicts
            slots: list[dict] = []
            for _date_key, day_data in data.items():
                if not isinstance(day_data, dict):
                    continue
                for start_iso in day_data.get("slots", []):
                    try:
                        start_dt = _dt.datetime.fromisoformat(
                            start_iso.replace("Z", "+00:00")
                        )
                        end_dt = start_dt + _dt.timedelta(minutes=_DEFAULT_SLOT_DURATION_MINUTES)
                        slots.append({
                            "startTime": start_dt.isoformat(),
                            "endTime": end_dt.isoformat(),
                        })
                    except Exception:
                        slots.append({"startTime": start_iso, "endTime": ""})
            return slots
        except httpx.HTTPStatusError as exc:
            logger.error(
                f"GHL get_available_slots: HTTP {exc.response.status_code} — {exc.response.text[:200]}"
            )
            return []

    async def create_appointment(
        self,
        contact_id: str,
        start_time: str,   # ISO 8601
        end_time: str,     # ISO 8601
        title: str = "Immigration Consultation",
        notes: str = "",
        timezone: str | None = None,
    ) -> dict | None:
        """
        Book an appointment for a contact.
        Returns the created appointment dict or None on failure.
        """
        appt_tz = timezone or settings.office_timezone
        payload: dict[str, Any] = {
            "calendarId": self._calendar_id,
            "locationId": self._location_id,
            "contactId": contact_id,
            "startTime": start_time,
            "endTime": end_time,
            "title": title,
            "appoinmentStatus": "confirmed",
            "timezone": appt_tz,
        }
        if notes:
            payload["notes"] = notes
        try:
            data = await self._post("/calendars/events/appointments", payload)
            # v2 returns {"event": {...}} or the event dict directly
            return data.get("event") or data.get("appointment") or data
        except httpx.HTTPStatusError as exc:
            logger.error(
                f"GHL create_appointment: HTTP {exc.response.status_code} — "
                f"{exc.response.text[:300]}"
            )
            return None

    async def update_appointment_status(
        self, appointment_id: str, status: str
    ) -> bool:
        """Update appointment status: confirmed | cancelled | showed | noShow."""
        try:
            await self._put(f"/calendars/events/appointments/{appointment_id}", {"appoinmentStatus": status})
            return True
        except httpx.HTTPStatusError as exc:
            logger.warning(f"GHL update_appointment_status: HTTP {exc.response.status_code}")
            return False

    async def get_appointment(self, appointment_id: str) -> dict | None:
        """Fetch a single appointment by ID. Returns the appointment dict or None."""
        try:
            data = await self._get(f"/calendars/events/appointments/{appointment_id}")
            return data.get("event") or data.get("appointment") or data or None
        except httpx.HTTPStatusError as exc:
            logger.warning(f"GHL get_appointment {appointment_id}: HTTP {exc.response.status_code}")
            return None

    # ─── Opportunity endpoints ─────────────────────────────────────────────────

    async def create_opportunity(
        self,
        contact_id: str,
        name: str,
        pipeline_id: str,
        stage_id: str,
        monetary_value: float = 0.0,
        status: str = "open",
    ) -> dict | None:
        """Create a pipeline opportunity for a contact."""
        payload = {
            "pipelineId": pipeline_id,
            "locationId": self._location_id,
            "name": name,
            "pipelineStageId": stage_id,
            "status": status,
            "contactId": contact_id,
            "monetaryValue": monetary_value,
        }
        try:
            data = await self._post("/opportunities/", payload)
            return data.get("opportunity") or data
        except httpx.HTTPStatusError as exc:
            logger.error(f"GHL create_opportunity: HTTP {exc.response.status_code}")
            return None

    # ─── SMS / messaging ──────────────────────────────────────────────────────

    async def send_sms(
        self,
        contact_id: str,
        message: str,
        from_number: str | None = None,
    ) -> bool:
        """
        Send an SMS to a contact via GHL messaging.
        Falls back to Twilio SMS if GHL send fails.
        """
        payload: dict[str, Any] = {
            "type": "SMS",
            "message": message,
            "contactId": contact_id,
        }
        if from_number:
            payload["fromNumber"] = from_number
        else:
            payload["fromNumber"] = settings.twilio_phone_number

        try:
            await self._post("/conversations/messages", payload)
            return True
        except httpx.HTTPStatusError as exc:
            logger.warning(f"GHL send_sms ({contact_id}): HTTP {exc.response.status_code} — falling back")
            return False


# ─── Singleton accessor ───────────────────────────────────────────────────────

_ghl_client: GHLClient | None = None


def get_ghl_client() -> GHLClient:
    global _ghl_client
    if _ghl_client is None:
        _ghl_client = GHLClient()
    return _ghl_client
