"""
Unit tests for is_office_open() in call_router.py.
Uses mocked datetime to test all routing scenarios without clock dependency.
"""
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
os.environ.setdefault("BASE_URL", "https://test.example.com")
os.environ.setdefault("GHL_API_KEY", "ghl-test")
os.environ.setdefault("GHL_LOCATION_ID", "loc-test")
os.environ.setdefault("ELEVENLABS_VOICE_ID_EN", "voice-en-test")
os.environ.setdefault("ELEVENLABS_VOICE_ID_ES", "voice-es-test")
os.environ.setdefault("GHL_CALENDAR_ID", "cal-test")
os.environ.setdefault("GOOGLE_CALENDAR_ID", "gcal-test")

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.telephony.call_router import is_office_open, OFFICE_TZ

TZ = ZoneInfo(OFFICE_TZ)


def _dt(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=TZ)


class TestIsOfficeOpen:
    # ── Business hours (weekday, non-holiday) ─────────────────────────────

    def test_open_during_business_hours(self):
        # Tuesday 10:00 AM
        now = _dt(2025, 3, 4, 10, 0)
        assert is_office_open(now) is True

    def test_open_at_exact_opening_time(self):
        # Monday 9:00 AM — boundary: open_time <= now
        now = _dt(2025, 3, 3, 9, 0)
        assert is_office_open(now) is True

    def test_closed_before_opening(self):
        # Wednesday 8:59 AM
        now = _dt(2025, 3, 5, 8, 59)
        assert is_office_open(now) is False

    def test_closed_at_closing_time(self):
        # Thursday 18:00 — boundary: now < close_time (exclusive)
        now = _dt(2025, 3, 6, 18, 0)
        assert is_office_open(now) is False

    def test_closed_after_closing(self):
        # Friday 19:00 PM
        now = _dt(2025, 3, 7, 19, 0)
        assert is_office_open(now) is False

    # ── Weekends ──────────────────────────────────────────────────────────

    def test_saturday_closed(self):
        now = _dt(2025, 3, 8, 10, 0)  # Saturday
        assert is_office_open(now) is False

    def test_sunday_closed(self):
        now = _dt(2025, 3, 9, 10, 0)  # Sunday
        assert is_office_open(now) is False

    # ── Federal holidays ─────────────────────────────────────────────────

    def test_new_years_day_closed(self):
        # January 1 (Wednesday in 2025)
        now = _dt(2025, 1, 1, 10, 0)
        assert is_office_open(now) is False

    def test_independence_day_closed(self):
        now = _dt(2025, 7, 4, 10, 0)
        assert is_office_open(now) is False

    def test_christmas_closed(self):
        now = _dt(2025, 12, 25, 10, 0)
        assert is_office_open(now) is False

    def test_veterans_day_closed(self):
        now = _dt(2025, 11, 11, 10, 0)
        assert is_office_open(now) is False

    # ── Non-holidays in the same months ──────────────────────────────────

    def test_non_holiday_jan_2_open(self):
        # Jan 2 is a Thursday — not a holiday
        now = _dt(2025, 1, 2, 10, 0)
        assert is_office_open(now) is True

    def test_non_holiday_dec_26_open(self):
        # Dec 26 is a Friday
        now = _dt(2025, 12, 26, 10, 0)
        assert is_office_open(now) is True


class TestOfficeConstants:
    def test_open_hour_value(self):
        from app.telephony.call_router import OFFICE_OPEN_HOUR
        assert OFFICE_OPEN_HOUR == 9

    def test_close_hour_value(self):
        from app.telephony.call_router import OFFICE_CLOSE_HOUR
        assert OFFICE_CLOSE_HOUR == 18

    def test_tz_string(self):
        assert OFFICE_TZ == "America/New_York"
