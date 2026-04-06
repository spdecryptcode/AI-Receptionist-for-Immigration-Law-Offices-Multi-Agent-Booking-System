"""
RAG admin API — protected endpoints for knowledge base management and analytics.

Auth: reuses the dashboard session cookie (same Redis key pattern).
"""
from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.dependencies import get_asyncpg_pool, get_redis_client, get_rag_retriever
from app.rag.ingestion import DocumentIngester

router = APIRouter(prefix="/rag", tags=["rag-admin"])

_SESSION_KEY = "dash:session:"


# ---------------------------------------------------------------------------
# Auth guard (reuses dashboard session cookie)
# ---------------------------------------------------------------------------

async def _require_session(
    dashboard_session: Optional[str] = Cookie(default=None),
) -> None:
    if not dashboard_session:
        raise HTTPException(status_code=401, detail="Not authenticated")
    redis = get_redis_client()
    valid = await redis.get(f"{_SESSION_KEY}{dashboard_session}")
    if valid != "1":
        raise HTTPException(status_code=401, detail="Session expired")


# ---------------------------------------------------------------------------
# Ingest a new document
# ---------------------------------------------------------------------------

class _IngestRequest(BaseModel):
    title: str
    source_type: str  # one of KnowledgeSourceType values
    language: str = "en"
    content: str
    metadata: Optional[dict] = None


@router.post("/ingest", summary="Ingest a knowledge document")
async def ingest_document(
    body: _IngestRequest,
    _: None = Depends(_require_session),
) -> JSONResponse:
    ingester = DocumentIngester()
    doc_id = await ingester.ingest_document(
        title=body.title,
        source_type=body.source_type,
        language=body.language,
        content=body.content,
        metadata=body.metadata,
    )
    if doc_id is None:
        return JSONResponse({"ok": False, "detail": "Pool unavailable or duplicate"}, status_code=400)
    return JSONResponse({"ok": True, "doc_id": doc_id})


# ---------------------------------------------------------------------------
# List documents
# ---------------------------------------------------------------------------

@router.get("/documents", summary="List knowledge documents")
async def list_documents(
    source_type: Optional[str] = Query(default=None),
    language: Optional[str] = Query(default=None),
    limit: int = Query(default=50, le=200),
    _: None = Depends(_require_session),
) -> JSONResponse:
    pool = get_asyncpg_pool()
    if pool is None:
        raise HTTPException(status_code=503, detail="Database pool unavailable")

    conditions = ["TRUE"]
    params: list = []
    if source_type:
        params.append(source_type)
        conditions.append(f"source_type = ${len(params)}")
    if language:
        params.append(language)
        conditions.append(f"language = ${len(params)}")
    params.append(limit)
    where = " AND ".join(conditions)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT id, title, source_type, language, created_at,
                   (SELECT COUNT(*) FROM knowledge_chunks kc WHERE kc.document_id = kd.id) AS chunk_count
            FROM knowledge_documents kd
            WHERE {where}
            ORDER BY created_at DESC
            LIMIT ${len(params)}
            """,
            *params,
        )

    return JSONResponse(
        [
            {
                "id": str(r["id"]),
                "title": r["title"],
                "source_type": r["source_type"],
                "language": r["language"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "chunk_count": r["chunk_count"],
            }
            for r in rows
        ]
    )


# ---------------------------------------------------------------------------
# Delete a document
# ---------------------------------------------------------------------------

@router.delete("/documents/{doc_id}", summary="Delete a knowledge document and its chunks")
async def delete_document(
    doc_id: str,
    _: None = Depends(_require_session),
) -> JSONResponse:
    pool = get_asyncpg_pool()
    if pool is None:
        raise HTTPException(status_code=503, detail="Database pool unavailable")

    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM knowledge_documents WHERE id = $1", doc_id
        )

    deleted = int(result.split()[-1])
    return JSONResponse({"ok": True, "deleted": deleted})


# ---------------------------------------------------------------------------
# Query analytics
# ---------------------------------------------------------------------------

@router.get("/analytics", summary="RAG query analytics (last 1000 queries)")
async def query_analytics(
    channel: Optional[str] = Query(default=None),
    _: None = Depends(_require_session),
) -> JSONResponse:
    pool = get_asyncpg_pool()
    if pool is None:
        raise HTTPException(status_code=503, detail="Database pool unavailable")

    params: list = []
    where = "TRUE"
    if channel:
        params.append(channel)
        where = "channel = $1"

    async with pool.acquire() as conn:
        summary = await conn.fetchrow(
            f"""
            SELECT
                COUNT(*) AS total,
                AVG(retrieval_ms) AS avg_ms,
                SUM(CASE WHEN cache_hit THEN 1 ELSE 0 END) AS cache_hits,
                SUM(CASE WHEN confidence_gate_triggered THEN 1 ELSE 0 END) AS gate_triggers,
                SUM(CASE WHEN cross_language_fallback THEN 1 ELSE 0 END) AS cross_lang,
                AVG(chunks_returned) AS avg_chunks,
                AVG(top_score) AS avg_top_score
            FROM rag_query_logs
            WHERE {where} AND created_at > NOW() - INTERVAL '24 hours'
            """,
            *params,
        )

    return JSONResponse(
        {
            "total_queries_24h": summary["total"],
            "avg_retrieval_ms": round(float(summary["avg_ms"] or 0), 1),
            "cache_hit_rate": round(
                float(summary["cache_hits"] or 0) / max(float(summary["total"] or 1), 1), 3
            ),
            "confidence_gate_rate": round(
                float(summary["gate_triggers"] or 0) / max(float(summary["total"] or 1), 1), 3
            ),
            "cross_language_fallback_rate": round(
                float(summary["cross_lang"] or 0) / max(float(summary["total"] or 1), 1), 3
            ),
            "avg_chunks_returned": round(float(summary["avg_chunks"] or 0), 2),
            "avg_top_score": round(float(summary["avg_top_score"] or 0), 2),
        }
    )


# ---------------------------------------------------------------------------
# Test retrieval endpoint
# ---------------------------------------------------------------------------

class _TestRetrieveRequest(BaseModel):
    query: str
    language: str = "en"
    phase: str = "INTAKE"
    channel: str = "web"


@router.post("/retrieve/test", summary="Test RAG retrieval (admin only)")
async def test_retrieve(
    body: _TestRetrieveRequest,
    _: None = Depends(_require_session),
) -> JSONResponse:
    retriever = get_rag_retriever()
    if retriever is None:
        raise HTTPException(status_code=503, detail="RAG retriever not initialised")

    chunks = await retriever.retrieve(
        query=body.query,
        language=body.language,
        phase=body.phase,
        channel=body.channel,
    )
    return JSONResponse(
        [
            {
                "id": c.id,
                "title": c.title,
                "source_type": c.source_type,
                "content": c.content[:300],
                "rerank_score": c.rerank_score,
                "rrf_score": round(c.rrf_score, 6),
            }
            for c in chunks
        ]
    )
