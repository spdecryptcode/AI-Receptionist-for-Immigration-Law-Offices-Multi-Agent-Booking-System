"""
Resilience layer — circuit breaker + retry wrapper for external service calls.

Pattern:
  - Circuit breaker: 3 failures / 60s window → 30s trip (OPEN state)
  - During OPEN: fast-fail with a filler audio response instead of hanging
  - After 30s trip: probe one request (HALF_OPEN state)
  - On probe success: close circuit; on probe failure: re-open
  - Retry wrapper: exponential backoff for transient HTTP errors, max 2 retries

Filler audio:
  - Short silence + "one moment please" phrase for TTS
  - Pre-generated mulaw files stored in assets/fillers/ for zero-latency playback
  - Falls back to synthesised audio if files not present

Services protected:
  - Deepgram STT connection
  - ElevenLabs TTS synthesis
  - OpenAI API calls
  - GHL API calls (via ghl_client)
"""
from __future__ import annotations

import asyncio
import functools
import logging
import time
from enum import Enum
from typing import Any, Callable, TypeVar, Awaitable
from pathlib import Path

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Awaitable[Any]])

# ─── Filler audio ─────────────────────────────────────────────────────────────

_FILLER_DIR = Path(__file__).parent.parent.parent / "assets" / "fillers"

_FILLER_FILES = {
    "en": "one_moment_en.ulaw",
    "es": "un_momento_es.ulaw",
}

_filler_cache: dict[str, bytes] = {}


def get_filler_audio(language: str = "en") -> bytes | None:
    """
    Return pre-recorded filler audio bytes (mulaw 8kHz).
    Returns None if file not present (caller should synthesize on the fly).
    """
    if language in _filler_cache:
        return _filler_cache[language]

    filename = _FILLER_FILES.get(language) or _FILLER_FILES["en"]
    path = _FILLER_DIR / filename

    if path.exists():
        data = path.read_bytes()
        _filler_cache[language] = data
        return data

    return None


# ─── Circuit breaker state ────────────────────────────────────────────────────

class CBState(Enum):
    CLOSED = "closed"       # Normal operation
    OPEN = "open"           # Fast-failing
    HALF_OPEN = "half_open" # Probing


class CircuitBreaker:
    """
    Simple per-service circuit breaker.

    Thread-safe for asyncio (single-threaded event loop).
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 3,
        window_seconds: float = 60.0,
        trip_seconds: float = 30.0,
    ):
        self.name = name
        self._failure_threshold = failure_threshold
        self._window_seconds = window_seconds
        self._trip_seconds = trip_seconds

        self._state = CBState.CLOSED
        self._failure_count = 0
        self._window_start = time.monotonic()
        self._tripped_at: float = 0.0

    @property
    def state(self) -> CBState:
        self._maybe_reset()
        return self._state

    def _maybe_reset(self) -> None:
        now = time.monotonic()
        if self._state == CBState.OPEN:
            if now - self._tripped_at >= self._trip_seconds:
                logger.info(f"Circuit breaker [{self.name}] → HALF_OPEN (probe)")
                self._state = CBState.HALF_OPEN
        elif self._state == CBState.CLOSED:
            if now - self._window_start >= self._window_seconds:
                # Reset window
                self._failure_count = 0
                self._window_start = now

    def record_success(self) -> None:
        if self._state == CBState.HALF_OPEN:
            logger.info(f"Circuit breaker [{self.name}] → CLOSED (probe succeeded)")
            self._state = CBState.CLOSED
            self._failure_count = 0

    def record_failure(self) -> None:
        self._maybe_reset()
        if self._state == CBState.HALF_OPEN:
            # Probe failed — re-open
            self._trip(reason="probe failed")
            return

        self._failure_count += 1
        if self._failure_count >= self._failure_threshold:
            self._trip(reason=f"{self._failure_count} failures in window")

    def _trip(self, reason: str) -> None:
        self._state = CBState.OPEN
        self._tripped_at = time.monotonic()
        logger.warning(
            f"Circuit breaker [{self.name}] TRIPPED ({reason}) — "
            f"fast-failing for {self._trip_seconds}s"
        )

    def is_open(self) -> bool:
        return self.state == CBState.OPEN


class CircuitBreakerOpen(Exception):
    """Raised when a call is attempted while the circuit is OPEN."""


# ─── Global circuit breaker registry ─────────────────────────────────────────

_breakers: dict[str, CircuitBreaker] = {
    "deepgram": CircuitBreaker("deepgram"),
    "elevenlabs": CircuitBreaker("elevenlabs"),
    "openai": CircuitBreaker("openai"),
    "ghl": CircuitBreaker("ghl"),
}


def get_breaker(service: str) -> CircuitBreaker:
    if service not in _breakers:
        _breakers[service] = CircuitBreaker(service)
    return _breakers[service]


# ─── Retry wrapper ────────────────────────────────────────────────────────────

async def retry_async(
    coro_fn: Callable[..., Awaitable[Any]],
    *args,
    retries: int = 2,
    base_delay: float = 0.5,
    service: str = "",
    **kwargs,
) -> Any:
    """
    Call an async function with exponential-backoff retry (jitter-free).
    Optionally integrates with a circuit breaker via `service` parameter.

    Raises the last exception if all retries are exhausted.
    """
    breaker = get_breaker(service) if service else None

    if breaker and breaker.is_open():
        raise CircuitBreakerOpen(f"Circuit breaker for {service} is OPEN")

    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            result = await coro_fn(*args, **kwargs)
            if breaker:
                breaker.record_success()
            return result
        except CircuitBreakerOpen:
            raise
        except Exception as exc:
            last_exc = exc
            if breaker:
                breaker.record_failure()
            if attempt < retries:
                delay = base_delay * (2 ** attempt)
                logger.warning(
                    f"retry_async [{service or 'unknown'}] attempt {attempt + 1}/{retries + 1} "
                    f"failed: {exc} — retrying in {delay:.1f}s"
                )
                await asyncio.sleep(delay)
            else:
                logger.error(
                    f"retry_async [{service or 'unknown'}] all {retries + 1} attempts failed: {exc}"
                )

    raise last_exc


# ─── Decorator ───────────────────────────────────────────────────────────────

def with_circuit_breaker(service: str, retries: int = 2, base_delay: float = 0.5):
    """
    Decorator that wraps an async function with circuit breaker + retry.

    Usage:
        @with_circuit_breaker("elevenlabs")
        async def synthesize_speech(text: str) -> bytes:
            ...
    """
    def decorator(func: F) -> F:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            return await retry_async(
                func,
                *args,
                retries=retries,
                base_delay=base_delay,
                service=service,
                **kwargs,
            )
        return wrapper  # type: ignore[return-value]
    return decorator
