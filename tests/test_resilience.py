"""
Unit tests for app/voice/resilience.py — VERIFICATION.md Test 24.

Covers:
  CircuitBreaker:
    - starts CLOSED
    - 3 failures within window → OPEN (trip)
    - is_open() True when OPEN, False when CLOSED
    - HALF_OPEN after trip_seconds elapses
    - probe success in HALF_OPEN → CLOSED (record_success)
    - probe failure in HALF_OPEN → re-OPEN
    - window expiry resets failure count
    - fewer than threshold failures does NOT trip
    - trip re-sets _tripped_at timestamp

  retry_async:
    - success on first attempt → result returned, no retry
    - retries up to `retries` times on exception, then raises
    - exponential backoff: base_delay * 2^attempt delay between retries
    - with service=...: circuit breaker records success/failure
    - raises CircuitBreakerOpen immediately when breaker is OPEN (no attempt)
    - re-raises the last exception after exhausting retries

  with_circuit_breaker decorator:
    - wraps async function transparently (return value preserved)
    - raises CircuitBreakerOpen when breaker is OPEN

  get_breaker:
    - returns known registered breakers by name
    - creates new breaker for unknown service name
    - same instance returned on second call

  get_filler_audio:
    - returns None when filler directory/file absent
    - returns bytes when file present (mocked Path.exists + read_bytes)
    - caches on second call
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
os.environ.setdefault("BASE_URL", "test.example.com")
os.environ.setdefault("GHL_API_KEY", "ghl-test")
os.environ.setdefault("GHL_LOCATION_ID", "loc-test")
os.environ.setdefault("ELEVENLABS_VOICE_ID_EN", "voice-en")
os.environ.setdefault("ELEVENLABS_VOICE_ID_ES", "voice-es")
os.environ.setdefault("GHL_CALENDAR_ID", "cal-test")
os.environ.setdefault("GOOGLE_CALENDAR_ID", "gcal-test")

import asyncio
import time
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from app.voice.resilience import (
    CBState,
    CircuitBreaker,
    CircuitBreakerOpen,
    get_breaker,
    get_filler_audio,
    retry_async,
    with_circuit_breaker,
    _filler_cache,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fresh_cb(**kwargs) -> CircuitBreaker:
    """A new CircuitBreaker with a short window for easy testing."""
    defaults = {"name": "test", "failure_threshold": 3,
                "window_seconds": 60.0, "trip_seconds": 30.0}
    defaults.update(kwargs)
    return CircuitBreaker(**defaults)


# ---------------------------------------------------------------------------
# CircuitBreaker — initial state
# ---------------------------------------------------------------------------

class TestCircuitBreakerInit:
    def test_starts_closed(self):
        cb = _fresh_cb()
        assert cb.state == CBState.CLOSED

    def test_is_open_false_when_closed(self):
        cb = _fresh_cb()
        assert cb.is_open() is False

    def test_failure_count_zero(self):
        cb = _fresh_cb()
        assert cb._failure_count == 0


# ---------------------------------------------------------------------------
# CircuitBreaker — trip behaviour
# ---------------------------------------------------------------------------

class TestCircuitBreakerTrip:
    def test_three_failures_trips_to_open(self):
        cb = _fresh_cb(failure_threshold=3)
        for _ in range(3):
            cb.record_failure()
        assert cb.state == CBState.OPEN

    def test_is_open_true_after_trip(self):
        cb = _fresh_cb(failure_threshold=3)
        for _ in range(3):
            cb.record_failure()
        assert cb.is_open() is True

    def test_fewer_than_threshold_does_not_trip(self):
        cb = _fresh_cb(failure_threshold=3)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CBState.CLOSED

    def test_trip_records_tripped_at_timestamp(self):
        cb = _fresh_cb(failure_threshold=1)
        before = time.monotonic()
        cb.record_failure()
        assert cb._tripped_at >= before

    def test_custom_threshold_respected(self):
        cb = _fresh_cb(failure_threshold=5)
        for _ in range(4):
            cb.record_failure()
        assert cb.state == CBState.CLOSED
        cb.record_failure()
        assert cb.state == CBState.OPEN


# ---------------------------------------------------------------------------
# CircuitBreaker — HALF_OPEN and recovery
# ---------------------------------------------------------------------------

class TestCircuitBreakerHalfOpen:
    def _tripped_cb(self, trip_seconds=30.0) -> CircuitBreaker:
        cb = _fresh_cb(failure_threshold=1, trip_seconds=trip_seconds)
        cb.record_failure()
        assert cb._state == CBState.OPEN
        return cb

    def test_open_transitions_to_half_open_after_trip_seconds(self):
        cb = self._tripped_cb(trip_seconds=1.0)
        # Manually push _tripped_at back
        cb._tripped_at -= 2.0
        assert cb.state == CBState.HALF_OPEN

    def test_half_open_success_closes_circuit(self):
        cb = self._tripped_cb()
        cb._state = CBState.HALF_OPEN
        cb.record_success()
        assert cb.state == CBState.CLOSED

    def test_half_open_success_resets_failure_count(self):
        cb = self._tripped_cb()
        cb._state = CBState.HALF_OPEN
        cb.record_success()
        assert cb._failure_count == 0

    def test_half_open_failure_reopens_circuit(self):
        cb = self._tripped_cb()
        cb._state = CBState.HALF_OPEN
        cb.record_failure()
        assert cb.state == CBState.OPEN

    def test_record_success_noop_when_closed(self):
        cb = _fresh_cb()
        cb.record_success()  # must not raise
        assert cb.state == CBState.CLOSED


# ---------------------------------------------------------------------------
# CircuitBreaker — window expiry resets failure count
# ---------------------------------------------------------------------------

class TestCircuitBreakerWindowReset:
    def test_window_expiry_resets_count(self):
        cb = _fresh_cb(failure_threshold=3, window_seconds=1.0)
        cb.record_failure()
        cb.record_failure()
        assert cb._failure_count == 2
        # Push window_start back so it expires
        cb._window_start -= 2.0
        # Trigger _maybe_reset via state property
        _ = cb.state
        assert cb._failure_count == 0


# ---------------------------------------------------------------------------
# retry_async
# ---------------------------------------------------------------------------

class TestRetryAsync:
    async def test_success_on_first_attempt(self):
        async def fn():
            return 42

        result = await retry_async(fn, retries=2, base_delay=0.0)
        assert result == 42

    async def test_reraises_after_all_retries(self):
        call_count = [0]

        async def always_fails():
            call_count[0] += 1
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            await retry_async(always_fails, retries=2, base_delay=0.0)

        assert call_count[0] == 3  # 1 initial + 2 retries

    async def test_success_on_second_attempt(self):
        attempts = [0]

        async def fn():
            attempts[0] += 1
            if attempts[0] < 2:
                raise ConnectionError("transient")
            return "ok"

        result = await retry_async(fn, retries=2, base_delay=0.0)
        assert result == "ok"
        assert attempts[0] == 2

    async def test_exponential_backoff_delays(self):
        sleep_calls = []

        async def failing():
            raise RuntimeError("err")

        async def fake_sleep(d):
            sleep_calls.append(d)

        with patch("app.voice.resilience.asyncio.sleep", side_effect=fake_sleep):
            with pytest.raises(RuntimeError):
                await retry_async(failing, retries=2, base_delay=1.0)

        # Delays: base=1.0*2^0=1.0, base=1.0*2^1=2.0
        assert sleep_calls == [1.0, 2.0]

    async def test_circuit_breaker_records_success(self):
        from app.voice.resilience import _breakers
        cb = CircuitBreaker("test_cb_suc", failure_threshold=3)
        _breakers["test_cb_suc"] = cb

        async def fn():
            return "done"

        await retry_async(fn, retries=0, base_delay=0.0, service="test_cb_suc")
        assert cb.state == CBState.CLOSED

    async def test_circuit_breaker_records_failure(self):
        from app.voice.resilience import _breakers
        cb = CircuitBreaker("test_cb_fail", failure_threshold=1)
        _breakers["test_cb_fail"] = cb

        async def fn():
            raise IOError("svc down")

        with pytest.raises(IOError):
            await retry_async(fn, retries=0, base_delay=0.0, service="test_cb_fail")

        assert cb.state == CBState.OPEN

    async def test_open_circuit_raises_immediately_without_calling_fn(self):
        from app.voice.resilience import _breakers
        cb = CircuitBreaker("test_cb_open", failure_threshold=1)
        cb.record_failure()  # trip it
        _breakers["test_cb_open"] = cb

        called = [False]

        async def fn():
            called[0] = True
            return "should not reach"

        with pytest.raises(CircuitBreakerOpen):
            await retry_async(fn, retries=2, base_delay=0.0, service="test_cb_open")

        assert called[0] is False

    async def test_no_service_no_circuit_breaker(self):
        """When service="" no circuit breaker should be consulted."""
        async def fn():
            return "plain"

        result = await retry_async(fn, retries=0, base_delay=0.0)
        assert result == "plain"


# ---------------------------------------------------------------------------
# with_circuit_breaker decorator
# ---------------------------------------------------------------------------

class TestWithCircuitBreakerDecorator:
    async def test_decorated_fn_returns_value(self):
        from app.voice.resilience import _breakers
        _breakers["dec_ok"] = CircuitBreaker("dec_ok", failure_threshold=3)

        @with_circuit_breaker("dec_ok", retries=0, base_delay=0.0)
        async def my_fn(x: int) -> int:
            return x * 2

        assert await my_fn(5) == 10

    async def test_decorated_fn_raises_cb_open_when_tripped(self):
        from app.voice.resilience import _breakers
        cb = CircuitBreaker("dec_open", failure_threshold=1)
        cb.record_failure()  # trip
        _breakers["dec_open"] = cb

        @with_circuit_breaker("dec_open", retries=0, base_delay=0.0)
        async def my_fn():
            return "nope"

        with pytest.raises(CircuitBreakerOpen):
            await my_fn()

    async def test_decorator_preserves_function_name(self):
        @with_circuit_breaker("any_svc")
        async def uniquely_named_function():
            return 1

        assert uniquely_named_function.__name__ == "uniquely_named_function"


# ---------------------------------------------------------------------------
# get_breaker
# ---------------------------------------------------------------------------

class TestGetBreaker:
    def test_known_services_registered(self):
        for svc in ("deepgram", "elevenlabs", "openai", "ghl"):
            cb = get_breaker(svc)
            assert isinstance(cb, CircuitBreaker)
            assert cb.name == svc

    def test_unknown_service_creates_new_breaker(self):
        cb = get_breaker("my_new_service_xyz")
        assert isinstance(cb, CircuitBreaker)
        assert cb.name == "my_new_service_xyz"

    def test_same_instance_returned_twice(self):
        cb1 = get_breaker("deepgram")
        cb2 = get_breaker("deepgram")
        assert cb1 is cb2


# ---------------------------------------------------------------------------
# get_filler_audio
# ---------------------------------------------------------------------------

class TestGetFillerAudio:
    def setup_method(self):
        _filler_cache.clear()

    def test_returns_none_when_file_absent(self):
        with patch("app.voice.resilience.Path.exists", return_value=False):
            assert get_filler_audio("en") is None

    def test_returns_bytes_when_file_present(self):
        fake_audio = b"\xff\xfe" * 100
        with patch("app.voice.resilience.Path.exists", return_value=True), \
             patch("app.voice.resilience.Path.read_bytes", return_value=fake_audio):
            result = get_filler_audio("en")
        assert result == fake_audio

    def test_caches_on_second_call(self):
        fake_audio = b"\x80" * 50
        with patch("app.voice.resilience.Path.exists", return_value=True), \
             patch("app.voice.resilience.Path.read_bytes",
                   return_value=fake_audio) as mock_rb:
            get_filler_audio("en")
            get_filler_audio("en")  # second call
        # read_bytes called only once — cache hit on second call
        mock_rb.assert_called_once()

    def test_unknown_language_falls_back_to_en(self):
        _filler_cache.clear()
        with patch("app.voice.resilience.Path.exists", return_value=False):
            # Should not raise even for unsupported language
            result = get_filler_audio("fr")
        assert result is None
