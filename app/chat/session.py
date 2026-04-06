"""
Web chat session management backed by Redis.

Session lifecycle:
  - Created via POST /chat/session
  - Stored in Redis as JSON hash, TTL = 24h, key = chat_session:{session_id}
  - Refreshed on each message
  - Rate limiting via chat_rate:{ip}:{window_minute}, TTL = 60s

Schema of session data:
  {
    "session_id": str,
    "language": "en" | "es",
    "created_at": ISO timestamp,
    "turns": [{"role": "user"|"assistant", "content": str}, ...],
    "intake": {},          # collected intake fields
    "phase": str,          # conversation phase name
    "case_type": str|null  # detected case type
  }
"""
from __future__ import annotations

import json
import secrets
import time
from typing import Any, Optional

from app.dependencies import get_redis_client

_SESSION_TTL = 60 * 60 * 24   # 24 hours
_RATE_LIMIT_MAX = 30           # messages per minute per IP
_RATE_WINDOW = 60              # seconds


def _session_key(session_id: str) -> str:
    return f"chat_session:{session_id}"


def _rate_key(ip: str) -> str:
    window = int(time.time()) // _RATE_WINDOW
    return f"chat_rate:{ip}:{window}"


async def create_session(language: str = "en") -> dict[str, Any]:
    """Create a new chat session and persist it to Redis."""
    redis = get_redis_client()
    session_id = secrets.token_urlsafe(32)
    data: dict[str, Any] = {
        "session_id": session_id,
        "language": language if language in ("en", "es") else "en",
        "created_at": time.time(),
        "turns": [],
        "intake": {},
        "phase": "GREETING",
        "case_type": None,
    }
    await redis.setex(_session_key(session_id), _SESSION_TTL, json.dumps(data))
    return data


async def get_session(session_id: str) -> Optional[dict[str, Any]]:
    """Retrieve a session from Redis. Returns None if not found or expired."""
    redis = get_redis_client()
    raw = await redis.get(_session_key(session_id))
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


async def save_session(data: dict[str, Any]) -> None:
    """Persist session data back to Redis, resetting the TTL."""
    redis = get_redis_client()
    await redis.setex(
        _session_key(data["session_id"]),
        _SESSION_TTL,
        json.dumps(data),
    )


async def append_turn(
    session_id: str, role: str, content: str
) -> Optional[dict[str, Any]]:
    """
    Append a turn to the session history.
    Keeps only the last 12 turns to stay within context window.
    Returns updated session or None if not found.
    """
    data = await get_session(session_id)
    if data is None:
        return None
    data["turns"].append({"role": role, "content": content})
    # Keep last 12 turns (6 user + 6 assistant)
    if len(data["turns"]) > 12:
        data["turns"] = data["turns"][-12:]
    await save_session(data)
    return data


async def check_rate_limit(ip: str) -> bool:
    """
    Increment the per-IP per-minute counter.
    Returns True if within limit, False if rate-limited.
    """
    redis = get_redis_client()
    key = _rate_key(ip)
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, _RATE_WINDOW)
    return count <= _RATE_LIMIT_MAX


async def delete_session(session_id: str) -> None:
    """Remove a session from Redis (called on explicit close)."""
    redis = get_redis_client()
    await redis.delete(_session_key(session_id))
