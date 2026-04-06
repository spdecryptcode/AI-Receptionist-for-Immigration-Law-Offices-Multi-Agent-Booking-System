"""
Shared singleton clients, initialized once at startup and
injected via FastAPI dependency injection where needed.
"""
from __future__ import annotations

import logging

import asyncpg
import httpx
import openai
import redis.asyncio as aioredis
from supabase import create_client, Client as SupabaseClient

from app.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# HTTP/2 AsyncClient — shared across all OpenAI requests
# Avoids TCP/TLS handshake cost per request (~50-100ms savings per call turn)
# ---------------------------------------------------------------------------
_http2_client: httpx.AsyncClient | None = None


def get_http2_client() -> httpx.AsyncClient:
    global _http2_client
    if _http2_client is None or _http2_client.is_closed:
        _http2_client = httpx.AsyncClient(
            http2=True,
            limits=httpx.Limits(
                max_connections=20,
                max_keepalive_connections=5,
                keepalive_expiry=60,
            ),
            timeout=httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0),
        )
    return _http2_client


# ---------------------------------------------------------------------------
# OpenAI async client — uses shared HTTP/2 pool
# ---------------------------------------------------------------------------
_openai_client: openai.AsyncOpenAI | None = None


def get_openai_client() -> openai.AsyncOpenAI:
    global _openai_client
    if _openai_client is None:
        _openai_client = openai.AsyncOpenAI(
            api_key=settings.openai_api_key,
            http_client=get_http2_client(),
        )
    return _openai_client


# ---------------------------------------------------------------------------
# Redis async client
# ---------------------------------------------------------------------------
_redis_client: aioredis.Redis | None = None


def get_redis_client() -> aioredis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = aioredis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
        )
    return _redis_client


# ---------------------------------------------------------------------------
# Supabase client
# ---------------------------------------------------------------------------
_supabase_client: SupabaseClient | None = None


def get_supabase_client() -> SupabaseClient:
    global _supabase_client
    if _supabase_client is None:
        _supabase_client = create_client(
            settings.supabase_url,
            settings.supabase_anon_key,
        )
    return _supabase_client


# ---------------------------------------------------------------------------
# asyncpg connection pool — required for RAG vector queries (pgvector)
# Falls back gracefully if database_url is not configured.
# ---------------------------------------------------------------------------
_asyncpg_pool: asyncpg.Pool | None = None


async def _init_asyncpg_pool() -> None:
    global _asyncpg_pool
    db_url = settings.database_url
    if not db_url:
        logger.warning(
            "database_url not configured — RAG vector search will be unavailable. "
            "Set DATABASE_URL env var to enable full RAG."
        )
        return
    # Strip SQLAlchemy driver prefix so asyncpg can use the URL directly
    db_url = (
        db_url.replace("postgresql+asyncpg://", "postgresql://")
              .replace("postgresql+psycopg2://", "postgresql://")
    )
    try:
        _asyncpg_pool = await asyncpg.create_pool(
            db_url,
            min_size=2,
            max_size=10,
            command_timeout=30,
        )
        logger.info("asyncpg pool for RAG initialised (min=2 max=10)")
    except Exception as exc:
        logger.warning(f"asyncpg pool creation failed: {exc} — RAG vector search will be unavailable")


def get_asyncpg_pool() -> asyncpg.Pool | None:
    """Return the asyncpg pool, or None if not configured/available."""
    return _asyncpg_pool


# ---------------------------------------------------------------------------
# RAG retriever singleton — set during app startup after pool is ready
# ---------------------------------------------------------------------------
_rag_retriever = None  # type: ignore[assignment]


def set_rag_retriever(retriever) -> None:
    global _rag_retriever
    _rag_retriever = retriever


def get_rag_retriever():
    """Return the RAGRetriever singleton, or None if not initialised."""
    return _rag_retriever


# ---------------------------------------------------------------------------
# Lifecycle: called from FastAPI lifespan
# ---------------------------------------------------------------------------
async def startup() -> None:
    """Pre-initialize all shared clients at startup."""
    get_http2_client()
    get_openai_client()
    get_redis_client()
    get_supabase_client()
    await _init_asyncpg_pool()


async def shutdown() -> None:
    """Gracefully close all shared clients."""
    global _http2_client, _redis_client, _asyncpg_pool
    if _http2_client and not _http2_client.is_closed:
        await _http2_client.aclose()
    if _redis_client:
        await _redis_client.aclose()
    if _asyncpg_pool:
        await _asyncpg_pool.close()
