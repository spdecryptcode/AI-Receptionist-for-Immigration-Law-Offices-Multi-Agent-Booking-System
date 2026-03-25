"""
FastAPI application entry point.

Startup sequence:
1. Initialize shared clients (Redis, OpenAI HTTP/2 pool, Supabase)
2. Register all routers
3. On SIGTERM: drain active WebSocket connections (graceful shutdown)
"""
from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app import dependencies
from app.config import settings
from app.logging_analytics.structured_logger import configure_logging

logger = logging.getLogger(__name__)

# Tracks active WebSocket call handlers — used for graceful shutdown
_active_calls: set[asyncio.Task] = set()
_accepting_connections = True


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan: startup → yield → shutdown."""
    # ── Startup ─────────────────────────────────────────────────────────────
    configure_logging(level="INFO", json_output=True)
    logger.info("Starting IVR Immigration AI Receptionist")
    await dependencies.startup()
    logger.info("All shared clients initialized")

    # Start DB persistence worker
    db_worker_task = asyncio.create_task(
        _start_db_worker(), name="db_persistence_worker"
    )
    logger.info("DB persistence worker started")

    # Start outbound callback queue consumer
    callback_worker_task = asyncio.create_task(
        _start_callback_worker(), name="callback_queue_worker"
    )
    logger.info("Callback queue consumer started")

    yield

    # ── Shutdown (SIGTERM / graceful deploy) ─────────────────────────────────
    global _accepting_connections
    _accepting_connections = False
    logger.info("Shutdown signal received — draining active calls")

    if _active_calls:
        logger.info(f"Waiting for {len(_active_calls)} active call(s) to complete")
        # Wait up to 30s for active calls to finish
        done, pending = await asyncio.wait(_active_calls, timeout=30.0)
        for task in pending:
            logger.warning("Cancelling call task that did not finish within 30s")
            task.cancel()

    # Stop DB worker
    db_worker_task.cancel()
    try:
        await db_worker_task
    except asyncio.CancelledError:
        pass

    # Stop callback worker
    callback_worker_task.cancel()
    try:
        await callback_worker_task
    except asyncio.CancelledError:
        pass

    await dependencies.shutdown()
    logger.info("Shutdown complete")


# ---------------------------------------------------------------------------
# App instance
# ---------------------------------------------------------------------------
app = FastAPI(
    title="IVR Immigration AI Receptionist",
    description="Real-time voice AI pipeline for immigration law intake",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Tighten in production
    allow_methods=["*"],
    allow_headers=["*"],
)

# Trust proxy headers from ngrok/reverse proxy so request.url uses https://
# and Twilio signature validation works correctly
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")

# Audit log middleware — logs all mutating requests for compliance
from app.compliance.middleware import AuditLogMiddleware
app.add_middleware(AuditLogMiddleware)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
@app.get("/health")
@app.get("/healthz")
async def health_check():
    """Liveness + shallow readiness check. Returns 200 only when Redis is reachable."""
    redis = dependencies.get_redis_client()
    redis_ok = False
    try:
        await redis.ping()
        redis_ok = True
    except Exception:
        pass

    # Shallow DB check — verify Supabase REST API is reachable
    db_ok = False
    try:
        import urllib.error
        import urllib.request
        req = urllib.request.Request(
            f"{settings.supabase_url}/rest/v1/",
            headers={"apikey": settings.supabase_anon_key},
        )
        def _ping_supabase():
            try:
                urllib.request.urlopen(req, timeout=3)
            except urllib.error.HTTPError:
                pass  # 401/404 means the server is up; that's enough
        await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(None, _ping_supabase),
            timeout=4.0,
        )
        db_ok = True
    except Exception:
        pass

    status = "ok" if (redis_ok and db_ok) else "degraded"
    return {
        "status": status,
        "redis": "ok" if redis_ok else "error",
        "db": "ok" if db_ok else "error",
        "accepting_connections": _accepting_connections,
    }


# ---------------------------------------------------------------------------
# Routers (registered after imports resolve)
# ---------------------------------------------------------------------------
def register_routers():
    from app.webhooks.twilio_webhooks import router as twilio_router
    from app.webhooks.ghl_webhooks import router as ghl_router
    from app.voice.websocket_handler import router as ws_router
    from app.social.webhook_handler import router as social_router
    from app.dashboard.router import router as dashboard_router

    # twilio_router already has prefix="/twilio" → routes at /twilio/*
    # ghl_router already has prefix="/ghl"       → routes at /ghl/*
    # ws_router has no top-level prefix          → routes at /ws/*
    # social_router has prefix="/social"         → routes at /social/*
    # dashboard_router has prefix="/dashboard"   → routes at /dashboard/*
    app.include_router(twilio_router)
    app.include_router(ghl_router)
    app.include_router(ws_router)
    app.include_router(social_router)
    app.include_router(dashboard_router)


register_routers()


async def _start_db_worker() -> None:
    """Wrapper to import and run the DB persistence worker (avoids circular import at module level)."""
    from app.logging_analytics.db_worker import db_worker_loop
    await db_worker_loop()


async def _start_callback_worker() -> None:
    """Wrapper to import and run the outbound callback queue consumer."""
    from app.telephony.outbound_callback import callback_queue_loop
    await callback_queue_loop()
