"""
Structured JSON logging for the IVR pipeline.

Every log entry emits a JSON object with:
  - timestamp (ISO 8601 UTC)
  - level
  - logger (module name)
  - message
  - call_sid (if available in context)
  - phase (ConversationPhase if available)
  - latency_ms (for timed events)
  - error (exception class + message for ERROR level)

Usage:
    from app.logging_analytics.structured_logger import get_logger, log_event

    logger = get_logger(__name__)
    logger.info("Greeting sent")

    # For performance events:
    log_event("tts_latency", call_sid=sid, latency_ms=142.5)
    log_event("deepgram_transcript", call_sid=sid, text="hello", confidence=0.97)

Call-level analytics events are also pushed to the `analytics_events` Redis list
for the background worker to persist to the `call_analytics` / `call_logs` tables.
"""
from __future__ import annotations

import json
import logging
import sys
import time
import traceback
from datetime import datetime, timezone
from typing import Any


# ─── JSON formatter ───────────────────────────────────────────────────────────

class JSONFormatter(logging.Formatter):
    """Emit log records as newline-delimited JSON."""

    LEVEL_MAP = {
        logging.DEBUG: "debug",
        logging.INFO: "info",
        logging.WARNING: "warning",
        logging.ERROR: "error",
        logging.CRITICAL: "critical",
    }

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        entry: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": self.LEVEL_MAP.get(record.levelno, "info"),
            "logger": record.name,
            "msg": record.getMessage(),
        }

        # Bubble up extra fields injected via logger.info("...", extra={...})
        for key in ("call_sid", "phase", "latency_ms", "event", "lang"):
            val = getattr(record, key, None)
            if val is not None:
                entry[key] = val

        if record.exc_info:
            entry["error"] = {
                "type": record.exc_info[0].__name__ if record.exc_info[0] else "unknown",
                "message": str(record.exc_info[1]) if record.exc_info[1] else "",
                "traceback": traceback.format_exception(*record.exc_info)[-3:],
            }

        return json.dumps(entry, ensure_ascii=False)


# ─── Setup logging ───────────────────────────────────────────────────────────

def configure_logging(level: str = "INFO", json_output: bool = True) -> None:
    """
    Configure root logger. Call once from main.py at startup.

    Args:
        level: Root log level string ("DEBUG", "INFO", "WARNING", "ERROR").
        json_output: If True, use JSONFormatter. If False, use standard text formatter.
    """
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Remove any existing handlers (avoids duplicate logs in reload scenarios)
    for h in root.handlers[:]:
        root.removeHandler(h)

    handler = logging.StreamHandler(sys.stdout)
    if json_output:
        handler.setFormatter(JSONFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("[%(asctime)s] %(levelname)-8s %(name)s: %(message)s")
        )
    root.addHandler(handler)

    # Quiet noisy third-party loggers
    for noisy in ("httpx", "httpcore", "websockets", "deepgram", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Return a logger with the given name. Thin wrapper for consistency."""
    return logging.getLogger(name)


# ─── Analytics event emitter ─────────────────────────────────────────────────

_analytics_redis = None
_analytics_queue_key = "analytics_events"


def _get_redis():
    global _analytics_redis
    if _analytics_redis is None:
        from app.dependencies import get_redis_client
        _analytics_redis = get_redis_client()
    return _analytics_redis


async def log_event(
    event_type: str,
    call_sid: str = "",
    phase: str = "",
    latency_ms: float | None = None,
    **kwargs: Any,
) -> None:
    """
    Emit a structured analytics event to Redis list `analytics_events`.
    The DB persistence worker consumes this list and writes to call_logs table.

    Examples:
        await log_event("tts_latency", call_sid=sid, latency_ms=145.2)
        await log_event("transcript_received", call_sid=sid, text="help me", confidence=0.98)
        await log_event("phase_transition", call_sid=sid, phase="intake", from_phase="greeting")
        await log_event("appointment_booked", call_sid=sid, appointment_id="abc123")
    """
    payload: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "event": event_type,
    }
    if call_sid:
        payload["call_sid"] = call_sid
    if phase:
        payload["phase"] = phase
    if latency_ms is not None:
        payload["latency_ms"] = round(latency_ms, 2)
    payload.update(kwargs)

    try:
        redis = _get_redis()
        await redis.rpush(_analytics_queue_key, json.dumps(payload, default=str))
    except Exception:
        # Analytics failure should never crash the call pipeline
        pass


# ─── Timing context manager ───────────────────────────────────────────────────

class TimedOperation:
    """
    Async context manager that logs operation latency as an analytics event.

    Usage:
        async with TimedOperation("deepgram_connect", call_sid=sid):
            await stt.connect()
    """

    def __init__(self, event_type: str, **kwargs: Any):
        self._event_type = event_type
        self._kwargs = kwargs
        self._start: float = 0.0

    async def __aenter__(self) -> "TimedOperation":
        self._start = time.monotonic()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        elapsed_ms = (time.monotonic() - self._start) * 1000
        await log_event(self._event_type, latency_ms=elapsed_ms, **self._kwargs)
        # Don't suppress exceptions
        return None
