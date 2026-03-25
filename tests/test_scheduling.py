"""
Unit tests for app/scheduling/slot_cache.py and app/scheduling/calendar_service.py.

Covers:
  slot_cache:
    _slot_key:
      - format is "slots:{calendar_id}:{day}"
      - constants _SLOT_KEY_PREFIX / _SLOT_TTL
    cache_slots:
      - pipeline.delete called with correct key
      - pipeline.zadd called once per valid slot with epoch score
      - pipeline.expire called with _SLOT_TTL (3600)
      - pipeline.execute awaited
      - slot missing startTime is skipped
      - slot with bad ISO is skipped (ValueError)
      - empty list → only delete + expire, no zadd
    get_cached_slots:
      - calls zrangebyscore(key, now, "+inf")
      - JSON-deserialises members
      - uses time.time() when now_epoch not given
      - Redis exception → returns []
    remove_slot:
      - zrange(key, 0, -1) fetched; matching slot (startTime) → zrem called
      - start_time alias also matched
      - no match → zrem NOT called
      - Redis exception swallowed
    invalidate_date:
      - redis.delete called with correct key
      - Redis exception swallowed
    get_next_business_days:
      - returns exactly n items
      - all returned days are weekdays (Mon–Fri)
      - no weekends included
      - n=0 returns []

  calendar_service:
    _format_slot_display:
      - correct strftime output for a known UTC ISO string
      - empty string on missing startTime
      - uses start_time alias
      - bad ISO returns original string
    format_slots_for_speech:
      - empty list EN → "We don't have any openings..."
      - empty list ES → "Actualmente no tenemos..."
      - single slot EN → "I have an opening on X."
      - single slot ES → "Tengo disponible el X."
      - two slots EN → joined with "or"
      - three slots EN → joined with commas + "or"
      - three slots ES → joined with "o"
      - max_slots=2 truncates to 2 display slots
      - slot with no display key silently skipped
    get_available_slots:
      - cache HIT: GHL API NOT called; returns cached slots annotated with display
      - cache MISS (no redis): GHL API called; slots annotated with display
      - cache MISS (redis present): GHL called, cache_slots called to store result
      - empty redis=None path works
    book_appointment:
      - returns None when startTime missing
      - returns None when endTime missing
      - calls ghl.create_appointment with correct args
      - returns None when ghl.create_appointment returns falsy
      - calls create_calendar_event (non-fatal on failure)
      - calls remove_slot on successful booking when redis present
      - does NOT call remove_slot when redis=None
      - returns appointment dict on success
      - title includes caller_name, language, case_type
      - Spanish language → lang_label "Spanish"
      - no case_type → title has no trailing "—"
"""
from __future__ import annotations

import json
import os

os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "testtoken")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+10000000000")
os.environ.setdefault("OPENAI_API_KEY", "sk-test")
os.environ.setdefault("DEEPGRAM_API_KEY", "dg-test")
os.environ.setdefault("ELEVENLABS_API_KEY", "el-test")
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_ANON_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")
os.environ.setdefault("BASE_URL", "test.example.com")
os.environ.setdefault("GHL_API_KEY", "ghl-test")
os.environ.setdefault("GHL_LOCATION_ID", "loc-test")
os.environ.setdefault("ELEVENLABS_VOICE_ID_EN", "voice-en")
os.environ.setdefault("ELEVENLABS_VOICE_ID_ES", "voice-es")
os.environ.setdefault("GHL_CALENDAR_ID", "cal-test")
os.environ.setdefault("GOOGLE_CALENDAR_ID", "gcal-test")

from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.scheduling.slot_cache import (
    _SLOT_KEY_PREFIX,
    _SLOT_TTL,
    _slot_key,
    cache_slots,
    get_cached_slots,
    get_next_business_days,
    invalidate_date,
    remove_slot,
)
from app.scheduling.calendar_service import (
    _format_slot_display,
    book_appointment,
    format_slots_for_speech,
    get_available_slots,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CAL_ID = "cal-test"
_DAY = "2025-01-06"  # a Monday


def _make_pipe():
    """Return a mock Redis pipeline with all expected chained methods."""
    pipe = MagicMock()
    pipe.delete = MagicMock(return_value=pipe)
    pipe.zadd = MagicMock(return_value=pipe)
    pipe.expire = MagicMock(return_value=pipe)
    pipe.execute = AsyncMock(return_value=[])
    return pipe


def _make_redis(zrangebyscore_result=None, zrange_result=None):
    """Return a (redis_mock, pipe_mock) pair."""
    redis = MagicMock()
    pipe = _make_pipe()
    redis.pipeline = MagicMock(return_value=pipe)
    redis.zrangebyscore = AsyncMock(return_value=zrangebyscore_result or [])
    redis.zrange = AsyncMock(return_value=zrange_result or [])
    redis.zrem = AsyncMock()
    redis.delete = AsyncMock()
    return redis, pipe


def _slot(start: str = "2025-01-06T09:00:00Z", end: str = "2025-01-06T09:30:00Z") -> dict:
    return {"startTime": start, "endTime": end}


# ---------------------------------------------------------------------------
# TestSlotKey
# ---------------------------------------------------------------------------

class TestSlotKey:
    def test_format(self):
        assert _slot_key("cal-abc", "2025-01-06") == "slots:cal-abc:2025-01-06"

    def test_prefix_constant(self):
        assert _SLOT_KEY_PREFIX == "slots"

    def test_ttl_constant(self):
        assert _SLOT_TTL == 3600

    def test_key_contains_calendar_id(self):
        key = _slot_key("my-calendar", "2025-03-15")
        assert "my-calendar" in key

    def test_key_contains_day(self):
        key = _slot_key("cal", "2025-03-15")
        assert "2025-03-15" in key


# ---------------------------------------------------------------------------
# TestCacheSlots
# ---------------------------------------------------------------------------

class TestCacheSlots:
    async def test_pipeline_created(self):
        redis, pipe = _make_redis()
        await cache_slots(_CAL_ID, _DAY, [_slot()], redis)
        redis.pipeline.assert_called_once()

    async def test_pipe_delete_called_with_key(self):
        redis, pipe = _make_redis()
        await cache_slots(_CAL_ID, _DAY, [_slot()], redis)
        pipe.delete.assert_called_once_with(_slot_key(_CAL_ID, _DAY))

    async def test_pipe_zadd_called_for_valid_slot(self):
        redis, pipe = _make_redis()
        slot = _slot("2025-01-06T14:00:00Z")
        await cache_slots(_CAL_ID, _DAY, [slot], redis)
        assert pipe.zadd.call_count == 1
        call_args = pipe.zadd.call_args
        key = call_args[0][0]
        scores = call_args[0][1]
        assert key == _slot_key(_CAL_ID, _DAY)
        # The score must be a positive epoch timestamp
        score_value = list(scores.values())[0]
        assert score_value > 1_700_000_000

    async def test_slot_json_stored_as_member(self):
        redis, pipe = _make_redis()
        slot = _slot("2025-01-06T14:00:00Z")
        await cache_slots(_CAL_ID, _DAY, [slot], redis)
        call_args = pipe.zadd.call_args[0][1]
        member = list(call_args.keys())[0]
        assert json.loads(member) == slot

    async def test_pipe_expire_called_with_ttl(self):
        redis, pipe = _make_redis()
        await cache_slots(_CAL_ID, _DAY, [_slot()], redis)
        pipe.expire.assert_called_once_with(_slot_key(_CAL_ID, _DAY), _SLOT_TTL)

    async def test_pipe_execute_awaited(self):
        redis, pipe = _make_redis()
        await cache_slots(_CAL_ID, _DAY, [_slot()], redis)
        pipe.execute.assert_awaited_once()

    async def test_slot_missing_start_time_skipped(self):
        redis, pipe = _make_redis()
        bad_slot = {"endTime": "2025-01-06T09:30:00Z"}  # no startTime
        await cache_slots(_CAL_ID, _DAY, [bad_slot], redis)
        pipe.zadd.assert_not_called()

    async def test_slot_bad_iso_skipped(self):
        redis, pipe = _make_redis()
        bad_slot = {"startTime": "not-a-date", "endTime": "2025-01-06T09:30:00Z"}
        await cache_slots(_CAL_ID, _DAY, [bad_slot], redis)
        pipe.zadd.assert_not_called()

    async def test_empty_list_no_zadd(self):
        redis, pipe = _make_redis()
        await cache_slots(_CAL_ID, _DAY, [], redis)
        pipe.zadd.assert_not_called()
        # but delete and expire are still called
        pipe.delete.assert_called_once()
        pipe.expire.assert_called_once()

    async def test_multiple_slots_all_added(self):
        redis, pipe = _make_redis()
        slots = [
            _slot("2025-01-06T09:00:00Z"),
            _slot("2025-01-06T10:00:00Z"),
            _slot("2025-01-06T11:00:00Z"),
        ]
        await cache_slots(_CAL_ID, _DAY, slots, redis)
        assert pipe.zadd.call_count == 3

    async def test_start_time_alias_used_when_no_startTime(self):
        """slot_cache also reads start_time (alias) from GHL."""
        redis, pipe = _make_redis()
        slot = {"start_time": "2025-01-06T09:00:00Z", "endTime": "2025-01-06T09:30:00Z"}
        await cache_slots(_CAL_ID, _DAY, [slot], redis)
        assert pipe.zadd.call_count == 1


# ---------------------------------------------------------------------------
# TestGetCachedSlots
# ---------------------------------------------------------------------------

class TestGetCachedSlots:
    async def test_returns_parsed_slots(self):
        slot = _slot()
        redis, _ = _make_redis(zrangebyscore_result=[json.dumps(slot).encode()])
        result = await get_cached_slots(_CAL_ID, _DAY, redis, now_epoch=0)
        assert result == [slot]

    async def test_zrangebyscore_args(self):
        redis, _ = _make_redis(zrangebyscore_result=[])
        now = 1_700_000_000.0
        await get_cached_slots(_CAL_ID, _DAY, redis, now_epoch=now)
        redis.zrangebyscore.assert_awaited_once_with(
            _slot_key(_CAL_ID, _DAY), now, "+inf"
        )

    async def test_redis_exception_returns_empty(self):
        redis = MagicMock()
        redis.zrangebyscore = AsyncMock(side_effect=Exception("redis down"))
        result = await get_cached_slots(_CAL_ID, _DAY, redis)
        assert result == []

    async def test_empty_cache_returns_empty_list(self):
        redis, _ = _make_redis(zrangebyscore_result=[])
        result = await get_cached_slots(_CAL_ID, _DAY, redis, now_epoch=0)
        assert result == []

    async def test_multiple_members_all_returned(self):
        slots = [_slot("2025-01-06T09:00:00Z"), _slot("2025-01-06T10:00:00Z")]
        members = [json.dumps(s).encode() for s in slots]
        redis, _ = _make_redis(zrangebyscore_result=members)
        result = await get_cached_slots(_CAL_ID, _DAY, redis, now_epoch=0)
        assert len(result) == 2

    async def test_uses_time_time_when_now_epoch_none(self):
        """When now_epoch is None, the function uses time.time() internally."""
        redis, _ = _make_redis(zrangebyscore_result=[])
        # Should not raise; score must be > 0
        result = await get_cached_slots(_CAL_ID, _DAY, redis)
        redis.zrangebyscore.assert_awaited_once()
        call_now_arg = redis.zrangebyscore.call_args[0][1]
        assert call_now_arg > 1_700_000_000  # sensible epoch


# ---------------------------------------------------------------------------
# TestRemoveSlot
# ---------------------------------------------------------------------------

class TestRemoveSlot:
    async def test_removes_matching_startTime(self):
        start_iso = "2025-01-06T09:00:00Z"
        slot = _slot(start_iso)
        member = json.dumps(slot).encode()
        redis, _ = _make_redis(zrange_result=[member])
        await remove_slot(_CAL_ID, _DAY, start_iso, redis)
        redis.zrem.assert_awaited_once_with(_slot_key(_CAL_ID, _DAY), member)

    async def test_removes_matching_start_time_alias(self):
        start_iso = "2025-01-06T10:00:00Z"
        slot = {"start_time": start_iso, "endTime": "2025-01-06T10:30:00Z"}
        member = json.dumps(slot).encode()
        redis, _ = _make_redis(zrange_result=[member])
        await remove_slot(_CAL_ID, _DAY, start_iso, redis)
        redis.zrem.assert_awaited_once()

    async def test_no_match_zrem_not_called(self):
        slot = _slot("2025-01-06T09:00:00Z")
        member = json.dumps(slot).encode()
        redis, _ = _make_redis(zrange_result=[member])
        await remove_slot(_CAL_ID, _DAY, "2025-01-06T11:00:00Z", redis)
        redis.zrem.assert_not_called()

    async def test_redis_exception_swallowed(self):
        redis = MagicMock()
        redis.zrange = AsyncMock(side_effect=Exception("redis down"))
        # Must not raise
        await remove_slot(_CAL_ID, _DAY, "2025-01-06T09:00:00Z", redis)

    async def test_zrange_called_with_full_range(self):
        redis, _ = _make_redis(zrange_result=[])
        await remove_slot(_CAL_ID, _DAY, "2025-01-06T09:00:00Z", redis)
        redis.zrange.assert_awaited_once_with(_slot_key(_CAL_ID, _DAY), 0, -1)


# ---------------------------------------------------------------------------
# TestInvalidateDate
# ---------------------------------------------------------------------------

class TestInvalidateDate:
    async def test_delete_called_with_correct_key(self):
        redis, _ = _make_redis()
        await invalidate_date(_CAL_ID, _DAY, redis)
        redis.delete.assert_awaited_once_with(_slot_key(_CAL_ID, _DAY))

    async def test_redis_exception_swallowed(self):
        redis = MagicMock()
        redis.delete = AsyncMock(side_effect=Exception("redis down"))
        await invalidate_date(_CAL_ID, _DAY, redis)  # must not raise


# ---------------------------------------------------------------------------
# TestGetNextBusinessDays
# ---------------------------------------------------------------------------

class TestGetNextBusinessDays:
    def test_returns_n_days(self):
        result = get_next_business_days(5)
        assert len(result) == 5

    def test_all_weekdays(self):
        result = get_next_business_days(10)
        for day_str in result:
            d = date.fromisoformat(day_str)
            assert d.weekday() < 5, f"{day_str} is a weekend"

    def test_no_weekend_days(self):
        result = get_next_business_days(14)
        for day_str in result:
            d = date.fromisoformat(day_str)
            assert d.weekday() not in (5, 6), f"{day_str} is Saturday or Sunday"

    def test_n_zero_returns_empty(self):
        assert get_next_business_days(0) == []

    def test_iso_format(self):
        result = get_next_business_days(1)
        # Verify ISO format parseable
        date.fromisoformat(result[0])

    def test_days_are_in_future(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/Chicago")
        today_str = datetime.now(tz).date().isoformat()
        result = get_next_business_days(5, tz=tz)
        for day_str in result:
            assert day_str > today_str

    def test_accepts_custom_tz(self):
        tz = ZoneInfo("America/New_York")
        result = get_next_business_days(3, tz=tz)
        assert len(result) == 3


# ---------------------------------------------------------------------------
# TestFormatSlotDisplay
# ---------------------------------------------------------------------------

class TestFormatSlotDisplay:
    def test_known_utc_iso(self):
        """2025-01-06T14:00:00Z in America/Chicago is 8:00 AM CST (UTC-6)."""
        tz = ZoneInfo("America/Chicago")
        slot = {"startTime": "2025-01-06T14:00:00Z"}
        result = _format_slot_display(slot, tz)
        # Mon Jan 6, 8:00 AM  (%-d may render as "6" on Linux/macOS)
        assert "Jan" in result
        assert "8:00 AM" in result

    def test_empty_string_on_missing_startTime(self):
        tz = ZoneInfo("America/Chicago")
        result = _format_slot_display({}, tz)
        assert result == ""

    def test_start_time_alias(self):
        tz = ZoneInfo("America/Chicago")
        slot = {"start_time": "2025-01-06T14:00:00Z"}
        result = _format_slot_display(slot, tz)
        assert result != ""

    def test_bad_iso_returns_original(self):
        tz = ZoneInfo("America/Chicago")
        slot = {"startTime": "not-a-date"}
        result = _format_slot_display(slot, tz)
        # Falls back to original string rather than raising
        assert result == "not-a-date"

    def test_z_suffix_handled(self):
        tz = ZoneInfo("UTC")
        slot = {"startTime": "2025-06-15T10:30:00Z"}
        result = _format_slot_display(slot, tz)
        assert "10:30 AM" in result


# ---------------------------------------------------------------------------
# TestFormatSlotsForSpeech
# ---------------------------------------------------------------------------

def _display_slot(display: str) -> dict:
    return {"startTime": "2025-01-06T09:00:00Z", "display": display}


class TestFormatSlotsForSpeech:
    def test_empty_en(self):
        result = format_slots_for_speech([], language="en")
        assert "We don't have any openings" in result

    def test_empty_es(self):
        result = format_slots_for_speech([], language="es")
        assert "Actualmente" in result or "no tenemos" in result

    def test_single_slot_en(self):
        result = format_slots_for_speech([_display_slot("Mon Jan 6, 9:00 AM")], language="en")
        assert result == "I have an opening on Mon Jan 6, 9:00 AM."

    def test_single_slot_es(self):
        result = format_slots_for_speech([_display_slot("lun ene 6, 9:00 AM")], language="es")
        assert result.startswith("Tengo disponible el")

    def test_two_slots_en_uses_or(self):
        slots = [_display_slot("Mon Jan 6, 9:00 AM"), _display_slot("Mon Jan 6, 10:00 AM")]
        result = format_slots_for_speech(slots, language="en")
        assert ", or " in result
        assert "Which works best for you?" in result

    def test_three_slots_en(self):
        slots = [
            _display_slot("Mon Jan 6, 9:00 AM"),
            _display_slot("Mon Jan 6, 10:00 AM"),
            _display_slot("Tue Jan 7, 9:00 AM"),
        ]
        result = format_slots_for_speech(slots, language="en")
        assert "Mon Jan 6, 9:00 AM" in result
        assert "Tue Jan 7, 9:00 AM" in result
        assert ", or " in result

    def test_three_slots_es(self):
        slots = [
            _display_slot("lun ene 6, 9:00"),
            _display_slot("lun ene 6, 10:00"),
            _display_slot("mar ene 7, 9:00"),
        ]
        result = format_slots_for_speech(slots, language="es")
        assert " o " in result
        assert "¿Cuál le funciona mejor?" in result

    def test_max_slots_truncates(self):
        slots = [_display_slot(f"slot {i}") for i in range(5)]
        result = format_slots_for_speech(slots, language="en", max_slots=2)
        # Only 2 slots shown; "slot 2" through "slot 4" absent
        assert "slot 2" not in result
        assert "slot 3" not in result

    def test_slot_without_display_field_skipped(self):
        slots = [{"startTime": "2025-01-06T09:00:00Z"}]  # no display key
        result = format_slots_for_speech(slots, language="en")
        # All displays empty → returns ""
        assert result == ""

    def test_default_language_is_en(self):
        result = format_slots_for_speech([_display_slot("Mon Jan 6, 9:00 AM")])
        assert "I have an opening on" in result


# ---------------------------------------------------------------------------
# TestGetAvailableSlots
# ---------------------------------------------------------------------------

class TestGetAvailableSlots:
    async def test_cache_hit_skips_ghl(self):
        """If Redis has cached slots, GHL API must not be called."""
        cached_slot = _slot("2025-01-06T09:00:00Z")
        cached_slot["display"] = "Mon Jan 6, 9:00 AM"

        redis, _ = _make_redis(
            zrangebyscore_result=[json.dumps(cached_slot).encode()]
        )

        mock_ghl = MagicMock()
        mock_ghl.get_available_slots = AsyncMock(return_value=[])

        with patch("app.scheduling.calendar_service.get_ghl_client", return_value=mock_ghl), \
             patch("app.scheduling.calendar_service.get_next_business_days", return_value=["2025-01-06"]):
            result = await get_available_slots(days_ahead=1, redis=redis)

        mock_ghl.get_available_slots.assert_not_awaited()
        assert len(result) >= 1

    async def test_cache_miss_calls_ghl(self):
        """On cache miss the GHL API is called."""
        fetched = [_slot("2025-01-06T09:00:00Z")]
        redis, _ = _make_redis(zrangebyscore_result=[])  # empty cache

        mock_ghl = MagicMock()
        mock_ghl.get_available_slots = AsyncMock(return_value=fetched)

        with patch("app.scheduling.calendar_service.get_ghl_client", return_value=mock_ghl), \
             patch("app.scheduling.calendar_service.get_next_business_days", return_value=["2025-01-06"]):
            result = await get_available_slots(days_ahead=1, redis=redis)

        mock_ghl.get_available_slots.assert_awaited_once()
        assert len(result) == 1

    async def test_cache_miss_stores_in_redis(self):
        """After a GHL fetch the slots are stored in Redis."""
        fetched = [_slot("2025-01-06T09:00:00Z")]
        redis, pipe = _make_redis(zrangebyscore_result=[])

        mock_ghl = MagicMock()
        mock_ghl.get_available_slots = AsyncMock(return_value=fetched)

        with patch("app.scheduling.calendar_service.get_ghl_client", return_value=mock_ghl), \
             patch("app.scheduling.calendar_service.get_next_business_days", return_value=["2025-01-06"]):
            await get_available_slots(days_ahead=1, redis=redis)

        # pipeline.execute should have been awaited (cache_slots uses pipeline)
        pipe.execute.assert_awaited()

    async def test_no_redis_calls_ghl(self):
        """When redis=None the GHL API is always called."""
        fetched = [_slot("2025-01-06T09:00:00Z")]
        mock_ghl = MagicMock()
        mock_ghl.get_available_slots = AsyncMock(return_value=fetched)

        with patch("app.scheduling.calendar_service.get_ghl_client", return_value=mock_ghl), \
             patch("app.scheduling.calendar_service.get_next_business_days", return_value=["2025-01-06"]):
            result = await get_available_slots(days_ahead=1, redis=None)

        mock_ghl.get_available_slots.assert_awaited_once()
        # display string injected
        assert "display" in result[0]

    async def test_slots_annotated_with_display(self):
        fetched = [_slot("2025-01-06T14:00:00Z")]
        mock_ghl = MagicMock()
        mock_ghl.get_available_slots = AsyncMock(return_value=fetched)

        with patch("app.scheduling.calendar_service.get_ghl_client", return_value=mock_ghl), \
             patch("app.scheduling.calendar_service.get_next_business_days", return_value=["2025-01-06"]):
            result = await get_available_slots(days_ahead=1, redis=None)

        assert "display" in result[0]
        assert result[0]["display"] != ""


# ---------------------------------------------------------------------------
# TestBookAppointment
# ---------------------------------------------------------------------------

class TestBookAppointment:
    def _mock_ghl(self, appt_return=None):
        ghl = MagicMock()
        ghl.create_appointment = AsyncMock(
            return_value=appt_return if appt_return is not None else {"id": "appt-123"}
        )
        return ghl

    async def test_returns_none_when_startTime_missing(self):
        slot = {"endTime": "2025-01-06T09:30:00Z"}
        result = await book_appointment("contact-1", slot)
        assert result is None

    async def test_returns_none_when_endTime_missing(self):
        slot = {"startTime": "2025-01-06T09:00:00Z"}
        result = await book_appointment("contact-1", slot)
        assert result is None

    async def test_calls_ghl_create_appointment(self):
        mock_ghl = self._mock_ghl()
        slot = _slot()
        with patch("app.scheduling.calendar_service.get_ghl_client", return_value=mock_ghl), \
             patch("app.scheduling.calendar_service.create_calendar_event", new_callable=AsyncMock, return_value="evt-1"):
            await book_appointment("contact-1", slot)
        mock_ghl.create_appointment.assert_awaited_once()

    async def test_returns_none_when_ghl_fails(self):
        mock_ghl = MagicMock()
        mock_ghl.create_appointment = AsyncMock(return_value=None)
        slot = _slot()
        with patch("app.scheduling.calendar_service.get_ghl_client", return_value=mock_ghl), \
             patch("app.scheduling.calendar_service.create_calendar_event", new_callable=AsyncMock, return_value=None):
            result = await book_appointment("contact-1", slot)
        assert result is None

    async def test_returns_appointment_dict_on_success(self):
        appt = {"id": "appt-xyz", "startTime": "2025-01-06T09:00:00Z"}
        mock_ghl = self._mock_ghl(appt_return=appt)
        slot = _slot()
        with patch("app.scheduling.calendar_service.get_ghl_client", return_value=mock_ghl), \
             patch("app.scheduling.calendar_service.create_calendar_event", new_callable=AsyncMock, return_value="evt-1"):
            result = await book_appointment("contact-1", slot)
        assert result == appt

    async def test_calls_create_calendar_event(self):
        mock_ghl = self._mock_ghl()
        mock_gcal = AsyncMock(return_value="evt-1")
        slot = _slot()
        with patch("app.scheduling.calendar_service.get_ghl_client", return_value=mock_ghl), \
             patch("app.scheduling.calendar_service.create_calendar_event", mock_gcal):
            await book_appointment("contact-1", slot)
        mock_gcal.assert_awaited_once()

    async def test_gcal_failure_nonfatal(self):
        """Google Calendar failure must not prevent returning the appointment."""
        mock_ghl = self._mock_ghl()
        slot = _slot()
        with patch("app.scheduling.calendar_service.get_ghl_client", return_value=mock_ghl), \
             patch("app.scheduling.calendar_service.create_calendar_event",
                   new_callable=AsyncMock, return_value=None):
            result = await book_appointment("contact-1", slot)
        assert result is not None

    async def test_remove_slot_called_when_redis_present(self):
        mock_ghl = self._mock_ghl()
        slot = _slot("2025-01-06T09:00:00Z")
        redis, _ = _make_redis(zrange_result=[json.dumps(slot).encode()])

        with patch("app.scheduling.calendar_service.get_ghl_client", return_value=mock_ghl), \
             patch("app.scheduling.calendar_service.create_calendar_event",
                   new_callable=AsyncMock, return_value="evt-1"), \
             patch("app.scheduling.calendar_service.remove_slot", new_callable=AsyncMock) as mock_remove:
            await book_appointment("contact-1", slot, redis=redis)

        mock_remove.assert_awaited_once()

    async def test_remove_slot_not_called_without_redis(self):
        mock_ghl = self._mock_ghl()
        slot = _slot()
        with patch("app.scheduling.calendar_service.get_ghl_client", return_value=mock_ghl), \
             patch("app.scheduling.calendar_service.create_calendar_event",
                   new_callable=AsyncMock, return_value="evt-1"), \
             patch("app.scheduling.calendar_service.remove_slot", new_callable=AsyncMock) as mock_remove:
            await book_appointment("contact-1", slot, redis=None)
        mock_remove.assert_not_called()

    async def test_title_includes_caller_name(self):
        mock_ghl = self._mock_ghl()
        slot = _slot()
        with patch("app.scheduling.calendar_service.get_ghl_client", return_value=mock_ghl), \
             patch("app.scheduling.calendar_service.create_calendar_event",
                   new_callable=AsyncMock, return_value=None):
            await book_appointment("contact-1", slot, caller_name="Jane Doe")

        call_kwargs = mock_ghl.create_appointment.call_args[1]
        assert "Jane Doe" in call_kwargs["title"]

    async def test_title_spanish_language(self):
        mock_ghl = self._mock_ghl()
        slot = _slot()
        with patch("app.scheduling.calendar_service.get_ghl_client", return_value=mock_ghl), \
             patch("app.scheduling.calendar_service.create_calendar_event",
                   new_callable=AsyncMock, return_value=None):
            await book_appointment("contact-1", slot, language="es")

        call_kwargs = mock_ghl.create_appointment.call_args[1]
        assert "Spanish" in call_kwargs["title"]

    async def test_title_includes_case_type(self):
        mock_ghl = self._mock_ghl()
        slot = _slot()
        with patch("app.scheduling.calendar_service.get_ghl_client", return_value=mock_ghl), \
             patch("app.scheduling.calendar_service.create_calendar_event",
                   new_callable=AsyncMock, return_value=None):
            await book_appointment("contact-1", slot, case_type="asylum")

        call_kwargs = mock_ghl.create_appointment.call_args[1]
        assert "asylum" in call_kwargs["title"]

    async def test_title_no_trailing_dash_without_case_type(self):
        mock_ghl = self._mock_ghl()
        slot = _slot()
        with patch("app.scheduling.calendar_service.get_ghl_client", return_value=mock_ghl), \
             patch("app.scheduling.calendar_service.create_calendar_event",
                   new_callable=AsyncMock, return_value=None):
            await book_appointment("contact-1", slot, case_type="")

        call_kwargs = mock_ghl.create_appointment.call_args[1]
        title = call_kwargs["title"]
        # No trailing em-dash segment when case_type is empty
        assert title.endswith(")") or not title.endswith("—")

    async def test_start_time_alias_in_slot(self):
        """slot using start_time / end_time aliases should still work."""
        slot = {"start_time": "2025-01-06T09:00:00Z", "end_time": "2025-01-06T09:30:00Z"}
        mock_ghl = self._mock_ghl()
        with patch("app.scheduling.calendar_service.get_ghl_client", return_value=mock_ghl), \
             patch("app.scheduling.calendar_service.create_calendar_event",
                   new_callable=AsyncMock, return_value=None):
            result = await book_appointment("contact-1", slot)
        assert result is not None
