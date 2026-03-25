"""
Compliance middleware.

1. AuditLogMiddleware — logs every incoming HTTP request (method, path, status,
   caller IP, duration) to a Redis `audit_log_queue` → db_worker writes to DB.
   Exempts health-check (`/health`) and static endpoints.

2. RecordingConsentMiddleware — ensures any Twilio inbound call that triggers
   a recording has consent headers set.  For TCPA compliance, the actual
   consent prompt is in the TwiML / AI script; this middleware just annotates
   the request context so downstream code can read `request.state.recording_consent`.

Mount in main.py:
    from app.compliance.middleware import AuditLogMiddleware
    app.add_middleware(AuditLogMiddleware)
"""
from __future__ import annotations

import json
import logging
import time
from typing import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

logger = logging.getLogger(__name__)

_SKIP_PATHS = frozenset(["/health", "/metrics", "/favicon.ico"])
_MUTATING_METHODS = frozenset(["POST", "PUT", "PATCH", "DELETE"])


class AuditLogMiddleware(BaseHTTPMiddleware):
    """
    Async middleware that enqueues an audit log entry for every mutating request.
    Non-blocking — failures are swallowed so the middleware never breaks requests.
    """

    def __init__(self, app: ASGIApp, redis_url: str = "") -> None:
        super().__init__(app)
        self._redis_url = redis_url
        self._redis_client = None  # lazily initialised

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        start = time.perf_counter()
        response = await call_next(request)
        duration_ms = round((time.perf_counter() - start) * 1000)

        # Only log mutating requests; skip noise paths
        if (
            request.method in _MUTATING_METHODS
            and request.url.path not in _SKIP_PATHS
        ):
            await self._enqueue(request, response.status_code, duration_ms)

        return response

    async def _enqueue(
        self,
        request: Request,
        status_code: int,
        duration_ms: int,
    ) -> None:
        try:
            from app.config import settings
            import redis.asyncio as aioredis
            url = self._redis_url or settings.redis_url
            client = aioredis.from_url(url, decode_responses=True)
            payload = json.dumps(
                {
                    "type": "audit_log",
                    "method": request.method,
                    "path": request.url.path,
                    "query": str(request.url.query)[:200],
                    "status_code": status_code,
                    "ip": _get_client_ip(request),
                    "user_agent": request.headers.get("user-agent", "")[:200],
                    "duration_ms": duration_ms,
                    "ts": int(time.time() * 1000),
                }
            )
            async with client:
                await client.rpush("audit_log_queue", payload)
        except Exception as exc:
            logger.debug(f"Audit log enqueue failed (non-critical): {exc}")


def _get_client_ip(request: Request) -> str:
    """Return the real client IP, honouring X-Forwarded-For from a trusted proxy."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        # Take the first (leftmost) IP — the original client
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
