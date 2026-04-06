"""
RAG retrieval engine with all optimizations enabled.

Retrieval path selection:
  - "fast"  → single hybrid search (no HyDE, no query variants)
             used when: query < 8 tokens AND no immigration keywords
  - "full"  → HyDE + 3 query variants → multi-vector hybrid search →
             RRF fusion → cross-language fallback → rerank → confidence gate →
             MMR diversity → parent chunk resolution

Optimizations implemented:
  1. Adaptive retrieval depth (fast / full path)
  2. Hybrid semantic + keyword search (RRF fusion)
  3. Reciprocal Rank Fusion across multiple query vectors
  4. HyDE (Hypothetical Document Embeddings) for full path
  5. Query expansion: 3 variants via GPT-4o-mini
  6. Cross-language fallback (en ↔ es)
  7. Confidence gating (top rerank score < 5 → return [])
  8. MMR diversity selection (λ = 0.7)
  9. Parent chunk resolution (return richer context)
 10. Redis result cache (TTL = 300s)
 11. Speculative prefetch on phase transition
 12. Quality-score weighted retrieval
 13. Per-query observability logging
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np

from app.config import settings
from app.dependencies import get_asyncpg_pool, get_openai_client, get_redis_client

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_EMBED_MODEL = "text-embedding-3-small"
_MINI_MODEL = "gpt-4o-mini"
_RESULT_CACHE_TTL = 300        # seconds
_CONFIDENCE_THRESHOLD = 5.0    # rerank score below this → gate triggers
_MMR_LAMBDA = 0.7              # relevance weight in MMR (1-λ = diversity)
_TOP_K_SEARCH = 20             # candidate pool per search pass
_FINAL_K = 5                   # chunks returned to LLM after MMR
_RERANK_TOP_N = 10             # candidates sent to reranker

_CASE_KEYWORDS = frozenset(
    "visa court ice detained daca deportation i-485 i-130 i-765 i-131 n-400 "
    "work permit asylum green card renewal denied petition removal ead "
    "travel document sponsor h-1b h1b tps parole naturalization".split()
)

_EMBED_CACHE_TTL = 60 * 60 * 24 * 7  # 7 days


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _cache_key(query: str, language: str, phase: str, source_types: tuple[str, ...]) -> str:
    raw = f"{query}|{language}|{phase}|{'|'.join(sorted(source_types))}"
    return f"rag:cache:{_sha256(raw)}"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ChunkResult:
    id: str
    document_id: str
    parent_chunk_id: Optional[str]
    content: str
    context_prefix: str
    language: str
    source_type: str
    title: str
    quality_score: float
    embedding: list[float] = field(default_factory=list, repr=False)
    rrf_score: float = 0.0
    rerank_score: float = 0.0


# ---------------------------------------------------------------------------
# RAGRetriever
# ---------------------------------------------------------------------------

class RAGRetriever:
    """
    Retrieves relevant knowledge chunks for a given query.
    Thread-safe; a single instance is shared across all concurrent requests.
    """

    def __init__(self) -> None:
        self._pool = get_asyncpg_pool()
        self._openai = get_openai_client()
        self._redis = get_redis_client()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def retrieve(
        self,
        query: str,
        language: str,
        phase: str,
        channel: str,
        call_sid: Optional[str] = None,
        session_id: Optional[str] = None,
        source_types: Optional[tuple[str, ...]] = None,
    ) -> list[ChunkResult]:
        """
        Main retrieval entry point.
        Returns up to _FINAL_K ranked, diverse, high-quality chunks.
        Returns [] if confidence gate triggers or pool unavailable.
        """
        if self._pool is None:
            return []

        t_start = time.monotonic()
        query = query.strip()
        if not query:
            return []

        if source_types is None:
            source_types = _default_source_types(phase)

        cache_key = _cache_key(query, language, phase, source_types)

        # --- Redis cache check ---
        try:
            cached = await self._redis.get(cache_key)
            if cached:
                await self._log_query(
                    query_hash=_sha256(query),
                    channel=channel,
                    path="cache",
                    cache_hit=True,
                    chunks_returned=0,
                    confidence_gate=False,
                    cross_lang=False,
                    retrieval_ms=int((time.monotonic() - t_start) * 1000),
                    language=language,
                    call_sid=call_sid,
                    session_id=session_id,
                )
                return json.loads(cached)  # returns raw dicts; caller tolerates
        except Exception:
            pass

        path = self._classify_path(query)

        # --- Embed the query ---
        query_vectors = [await self._embed(query)]

        # --- Full path: HyDE + query variants ---
        if path == "full":
            hyde_doc = await self._hyde(query, language)
            if hyde_doc:
                query_vectors.append(await self._embed(hyde_doc))
            variants = await self._query_variants(query, language)
            for v in variants:
                query_vectors.append(await self._embed(v))

        # --- Hybrid search for each query vector ---
        all_candidates: dict[str, tuple[ChunkResult, int]] = {}  # id → (chunk, rank_sum)
        for q_vec in query_vectors:
            hits = await self._hybrid_search(q_vec, query, language, source_types)
            for rank, chunk in enumerate(hits, start=1):
                if chunk.id in all_candidates:
                    all_candidates[chunk.id] = (
                        all_candidates[chunk.id][0],
                        all_candidates[chunk.id][1] + rank,
                    )
                else:
                    all_candidates[chunk.id] = (chunk, rank)

        # --- Cross-language fallback ---
        cross_lang = False
        if len(all_candidates) < 3:
            fallback_lang = "es" if language == "en" else "en"
            fallback_hits = await self._hybrid_search(
                query_vectors[0], query, fallback_lang, source_types
            )
            for rank, chunk in enumerate(fallback_hits, start=1):
                if chunk.id not in all_candidates:
                    all_candidates[chunk.id] = (chunk, rank + 100)  # deprioritise
            cross_lang = bool(fallback_hits)

        if not all_candidates:
            await self._log_query(
                _sha256(query), channel, path, False, 0, False, cross_lang,
                int((time.monotonic() - t_start) * 1000), language, call_sid, session_id,
            )
            return []

        # --- RRF scoring ---
        ranked = _rrf_score(all_candidates)

        # --- Rerank top candidates ---
        top_for_rerank = ranked[: _RERANK_TOP_N]
        if path == "full" and top_for_rerank:
            top_for_rerank = await self._rerank(query, top_for_rerank)

        # --- Confidence gate ---
        gate_triggered = False
        if top_for_rerank and top_for_rerank[0].rerank_score < _CONFIDENCE_THRESHOLD:
            gate_triggered = True
            await self._log_query(
                _sha256(query), channel, path, True, 0, True, cross_lang,
                int((time.monotonic() - t_start) * 1000), language, call_sid, session_id,
                top_score=top_for_rerank[0].rerank_score,
            )
            return []

        # --- MMR diversity selection ---
        final_chunks = _mmr_select(top_for_rerank, k=_FINAL_K, lam=_MMR_LAMBDA)

        # --- Resolve to parent chunks for richer context ---
        final_chunks = await self._resolve_parents(final_chunks)

        # --- Cache result ---
        try:
            serialised = json.dumps([_chunk_to_dict(c) for c in final_chunks])
            await self._redis.setex(cache_key, _RESULT_CACHE_TTL, serialised)
        except Exception:
            pass

        retrieval_ms = int((time.monotonic() - t_start) * 1000)
        top_score = final_chunks[0].rerank_score if final_chunks else 0.0

        await self._log_query(
            _sha256(query), channel, path, False, len(final_chunks), gate_triggered,
            cross_lang, retrieval_ms, language, call_sid, session_id, top_score=top_score,
        )

        logger.debug(
            f"retrieve: '{query[:60]}' → {len(final_chunks)} chunks "
            f"path={path} lang={language} {retrieval_ms}ms"
        )
        return final_chunks

    async def prefetch(
        self,
        phase: str,
        language: str,
        case_type: Optional[str] = None,
    ) -> None:
        """
        Warm the Redis cache for the anticipated RAG query at the next phase.
        Called speculatively from _maybe_advance_phase() as a background task.
        """
        if self._pool is None:
            return
        query = _phase_prefetch_query(phase, language, case_type)
        if not query:
            return
        try:
            await self.retrieve(query, language, phase, channel="prefetch")
        except Exception as exc:
            logger.debug(f"prefetch failed phase={phase}: {exc}")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _classify_path(self, query: str) -> str:
        """
        "fast" if query is short AND contains no immigration keywords.
        "full" otherwise.
        """
        tokens = query.lower().split()
        if len(tokens) < 8:
            if not any(kw in query.lower() for kw in _CASE_KEYWORDS):
                return "fast"
        return "full"

    async def _embed(self, text: str) -> list[float]:
        """Embed a single text string with Redis caching."""
        cache_key = f"emb:cache:{_sha256(text)}"
        try:
            cached = await self._redis.get(cache_key)
            if cached:
                return json.loads(cached)
        except Exception:
            pass
        try:
            resp = await self._openai.embeddings.create(
                model=_EMBED_MODEL, input=[text[:8000]]
            )
            vec = resp.data[0].embedding
            try:
                await self._redis.setex(cache_key, _EMBED_CACHE_TTL, json.dumps(vec))
            except Exception:
                pass
            return vec
        except Exception as exc:
            logger.warning(f"_embed failed: {exc}")
            return [0.0] * 1536

    async def _hyde(self, query: str, language: str) -> Optional[str]:
        """
        Generate a Hypothetical Document Embedding (HyDE) passage.
        The synthetic passage is embedded to retrieve semantically similar real chunks.
        """
        lang_hint = "in Spanish" if language == "es" else ""
        try:
            resp = await self._openai.chat.completions.create(
                model=_MINI_MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            f"You are an immigration attorney. Write a short factual paragraph "
                            f"{lang_hint} that would directly answer the following question. "
                            "Be specific to US immigration law. Max 80 words."
                        ),
                    },
                    {"role": "user", "content": query},
                ],
                max_tokens=120,
                temperature=0.3,
            )
            return resp.choices[0].message.content.strip()
        except Exception as exc:
            logger.debug(f"_hyde failed: {exc}")
            return None

    async def _query_variants(self, query: str, language: str) -> list[str]:
        """
        Generate 3 semantically diverse query reformulations via GPT-4o-mini.
        """
        lang_hint = "in Spanish" if language == "es" else ""
        try:
            resp = await self._openai.chat.completions.create(
                model=_MINI_MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            f"Generate 3 alternative search queries {lang_hint} for the following "
                            "immigration question. Make each query semantically different. "
                            'Return JSON: {"variants": ["...", "...", "..."]}'
                        ),
                    },
                    {"role": "user", "content": query},
                ],
                max_tokens=100,
                temperature=0.6,
                response_format={"type": "json_object"},
            )
            parsed = json.loads(resp.choices[0].message.content)
            return parsed.get("variants", [])[:3]
        except Exception as exc:
            logger.debug(f"_query_variants failed: {exc}")
            return []

    async def _hybrid_search(
        self,
        query_vector: list[float],
        query_text: str,
        language: str,
        source_types: tuple[str, ...],
    ) -> list[ChunkResult]:
        """
        Run a hybrid vector + full-text search with RRF fusion.
        Only child chunks are searched (finer granularity); parent chunks are
        resolved later for richer context.
        """
        vec_str = "[" + ",".join(f"{x:.8f}" for x in query_vector) + "]"
        lang_dict = "spanish" if language == "es" else "english"
        st_placeholders = ", ".join(f"${i+4}" for i in range(len(source_types)))

        sql = f"""
        WITH vector_ranked AS (
            SELECT
                kc.id,
                kc.document_id,
                kc.parent_chunk_id,
                kc.content,
                kc.context_prefix,
                kc.language,
                kc.quality_score,
                kd.source_type,
                kd.title,
                kc.embedding::text AS embedding_text,
                ROW_NUMBER() OVER (
                    ORDER BY kc.embedding <=> $1::vector
                ) AS vec_rank
            FROM knowledge_chunks kc
            JOIN knowledge_documents kd ON kd.id = kc.document_id
            WHERE kc.embedding IS NOT NULL
              AND kc.language = $2
              AND kd.source_type IN ({st_placeholders})
              AND (kd.expires_at IS NULL OR kd.expires_at > NOW())
              AND kc.quality_score >= 3
            ORDER BY kc.embedding <=> $1::vector
            LIMIT {_TOP_K_SEARCH}
        ),
        text_ranked AS (
            SELECT
                kc.id,
                ROW_NUMBER() OVER (
                    ORDER BY ts_rank(
                        kc.tsvector_col,
                        plainto_tsquery('{lang_dict}', $3)
                    ) DESC
                ) AS txt_rank
            FROM knowledge_chunks kc
            JOIN knowledge_documents kd ON kd.id = kc.document_id
            WHERE kc.tsvector_col @@ plainto_tsquery('{lang_dict}', $3)
              AND kc.language = $2
              AND kd.source_type IN ({st_placeholders})
              AND (kd.expires_at IS NULL OR kd.expires_at > NOW())
            LIMIT {_TOP_K_SEARCH}
        )
        SELECT
            vr.id,
            vr.document_id,
            vr.parent_chunk_id,
            vr.content,
            vr.context_prefix,
            vr.language,
            vr.quality_score,
            vr.source_type,
            vr.title,
            vr.embedding_text,
            (1.0 / (60 + vr.vec_rank)) AS vec_rrf,
            COALESCE(1.0 / (60 + tr.txt_rank), 0) AS txt_rrf
        FROM vector_ranked vr
        LEFT JOIN text_ranked tr ON tr.id = vr.id
        ORDER BY (1.0 / (60 + vr.vec_rank)) + COALESCE(1.0 / (60 + tr.txt_rank), 0) DESC
        LIMIT {_TOP_K_SEARCH}
        """

        params = [vec_str, language, query_text] + list(source_types)

        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(sql, *params)
        except Exception as exc:
            logger.warning(f"_hybrid_search DB error: {exc}")
            return []

        results: list[ChunkResult] = []
        for r in rows:
            emb_text = r["embedding_text"] or ""
            try:
                # Parse pgvector text format "[0.1,0.2,...]"
                raw_vec = emb_text.strip("[]").split(",")
                embedding = [float(x) for x in raw_vec]
            except Exception:
                embedding = []
            chunk = ChunkResult(
                id=str(r["id"]),
                document_id=str(r["document_id"]),
                parent_chunk_id=str(r["parent_chunk_id"]) if r["parent_chunk_id"] else None,
                content=r["content"],
                context_prefix=r["context_prefix"] or "",
                language=r["language"],
                source_type=r["source_type"],
                title=r["title"],
                quality_score=float(r["quality_score"]),
                embedding=embedding,
                rrf_score=float(r["vec_rrf"]) + float(r["txt_rrf"]),
            )
            results.append(chunk)

        return results

    async def _rerank(
        self, query: str, chunks: list[ChunkResult]
    ) -> list[ChunkResult]:
        """
        Rerank candidates using GPT-4o-mini pairwise scoring.
        Assigns ChunkResult.rerank_score in [0, 10].
        Returns sorted list (highest score first).
        """
        if not chunks:
            return chunks

        numbered = "\n\n".join(
            f"[{i+1}] {c.context_prefix}\n{c.content[:400]}"
            for i, c in enumerate(chunks)
        )
        try:
            resp = await self._openai.chat.completions.create(
                model=_MINI_MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a reranker for an immigration legal assistant. "
                            f"Query: \"{query}\"\n\n"
                            "Score each of the following chunks 0-10 for relevance to the query. "
                            "10=directly answers the query, 0=completely irrelevant. "
                            'Return ONLY JSON: {"scores": [8, 5, ...]}'
                        ),
                    },
                    {"role": "user", "content": numbered},
                ],
                max_tokens=len(chunks) * 8,
                temperature=0.0,
                response_format={"type": "json_object"},
            )
            raw = json.loads(resp.choices[0].message.content)
            scores = raw.get("scores", [])
            for i, chunk in enumerate(chunks):
                chunk.rerank_score = float(scores[i]) if i < len(scores) else chunk.rrf_score * 10
        except Exception as exc:
            logger.debug(f"_rerank failed: {exc}")
            # Fallback: use rrf_score scaled to [0, 10]
            max_rrf = max((c.rrf_score for c in chunks), default=1) or 1
            for chunk in chunks:
                chunk.rerank_score = (chunk.rrf_score / max_rrf) * 10

        return sorted(chunks, key=lambda c: c.rerank_score, reverse=True)

    async def _resolve_parents(self, chunks: list[ChunkResult]) -> list[ChunkResult]:
        """
        Replace child chunks with their parent (if they have one) for richer context.
        Deduplicates by parent_chunk_id.
        """
        if not chunks:
            return chunks

        parent_ids = [c.parent_chunk_id for c in chunks if c.parent_chunk_id]
        if not parent_ids:
            return chunks

        seen: set[str] = set()
        resolved: list[ChunkResult] = []

        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT
                        kc.id, kc.document_id, kc.content, kc.context_prefix,
                        kc.language, kc.quality_score, kd.source_type, kd.title
                    FROM knowledge_chunks kc
                    JOIN knowledge_documents kd ON kd.id = kc.document_id
                    WHERE kc.id = ANY($1::uuid[])
                    """,
                    parent_ids,
                )
            parent_map = {str(r["id"]): r for r in rows}
        except Exception as exc:
            logger.debug(f"_resolve_parents DB error: {exc}")
            return chunks

        for chunk in chunks:
            if chunk.parent_chunk_id and chunk.parent_chunk_id in parent_map:
                p = parent_map[chunk.parent_chunk_id]
                key = chunk.parent_chunk_id
                if key not in seen:
                    seen.add(key)
                    resolved.append(ChunkResult(
                        id=str(p["id"]),
                        document_id=str(p["document_id"]),
                        parent_chunk_id=None,
                        content=p["content"],
                        context_prefix=p["context_prefix"] or "",
                        language=p["language"],
                        source_type=p["source_type"],
                        title=p["title"],
                        quality_score=float(p["quality_score"]),
                        rerank_score=chunk.rerank_score,
                        rrf_score=chunk.rrf_score,
                    ))
            else:
                if chunk.id not in seen:
                    seen.add(chunk.id)
                    resolved.append(chunk)

        return resolved

    async def _log_query(
        self,
        query_hash: str,
        channel: str,
        path: str,
        cache_hit: bool,
        chunks_returned: int,
        confidence_gate: bool,
        cross_lang: bool,
        retrieval_ms: int,
        language: str,
        call_sid: Optional[str],
        session_id: Optional[str],
        top_score: float = 0.0,
    ) -> None:
        """Fire-and-forget: write a rag_query_logs row."""
        if self._pool is None:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO rag_query_logs
                        (query_hash, channel, path, top_score, chunks_returned,
                         confidence_gate_triggered, cross_language_fallback,
                         cache_hit, retrieval_ms, language, call_sid, session_id)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
                    """,
                    query_hash, channel, path, top_score, chunks_returned,
                    confidence_gate, cross_lang, cache_hit, retrieval_ms,
                    language, call_sid, session_id,
                )
        except Exception as exc:
            logger.debug(f"_log_query failed: {exc}")


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _rrf_score(
    candidates: dict[str, tuple[ChunkResult, int]]
) -> list[ChunkResult]:
    """Sort candidates by RRF score descending."""
    scored: list[ChunkResult] = []
    for chunk, rank_sum in candidates.values():
        chunk.rrf_score = 1.0 / (60 + rank_sum)
        scored.append(chunk)
    return sorted(scored, key=lambda c: c.rrf_score, reverse=True)


def _mmr_select(
    chunks: list[ChunkResult], k: int, lam: float
) -> list[ChunkResult]:
    """
    Maximal Marginal Relevance selection.
    Uses rerank_score as relevance and cosine similarity between embeddings
    to enforce diversity (λ controls relevance vs diversity trade-off).
    """
    if not chunks:
        return []
    if len(chunks) <= k:
        return chunks

    # Filter chunks with no embedding to avoid numpy errors
    scored = [c for c in chunks if c.embedding]
    no_emb = [c for c in chunks if not c.embedding]

    if not scored:
        return chunks[:k]

    embeddings = np.array([c.embedding for c in scored], dtype=np.float32)
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    norms = np.where(norms == 0, 1, norms)
    embeddings = embeddings / norms

    selected_indices: list[int] = []
    remaining = list(range(len(scored)))

    # First chunk is always the top-ranked
    best = max(remaining, key=lambda i: scored[i].rerank_score)
    selected_indices.append(best)
    remaining.remove(best)

    while len(selected_indices) < min(k, len(scored)):
        selected_embs = embeddings[selected_indices]
        scores_mmr: list[float] = []
        for i in remaining:
            relevance = scored[i].rerank_score / 10.0  # normalised to [0,1]
            sim = float(np.max(embeddings[i] @ selected_embs.T))
            mmr = lam * relevance - (1 - lam) * sim
            scores_mmr.append(mmr)
        chosen = remaining[int(np.argmax(scores_mmr))]
        selected_indices.append(chosen)
        remaining.remove(chosen)

    result = [scored[i] for i in selected_indices]
    # Append no-embedding chunks if budget permits
    budget = k - len(result)
    result.extend(no_emb[:budget])
    return result


def _default_source_types(phase: str) -> tuple[str, ...]:
    """Map conversation phase to the most relevant source types."""
    mapping: dict[str, tuple[str, ...]] = {
        "URGENCY_TRIAGE": ("faq", "case_guide", "firm_policy"),
        "INTAKE": ("case_guide", "uscis_form", "faq"),
        "CONSULTATION_PITCH": ("firm_policy", "faq", "case_guide"),
        "BOOKING": ("firm_policy", "faq"),
        "CONFIRMATION": ("firm_policy",),
    }
    return mapping.get(phase, ("faq", "case_guide", "firm_policy", "uscis_form"))


def _phase_prefetch_query(
    phase: str, language: str, case_type: Optional[str]
) -> Optional[str]:
    """
    Return a representative query to pre-warm the cache for a given phase.
    """
    templates: dict[str, str] = {
        "URGENCY_TRIAGE": "emergency immigration detention court date",
        "INTAKE": f"{case_type or 'immigration'} case requirements documents process",
        "CONSULTATION_PITCH": f"immigration attorney consultation {case_type or 'services'} fees",
        "BOOKING": "schedule appointment immigration attorney",
        "CONFIRMATION": "appointment confirmation immigration law firm",
    }
    query = templates.get(phase)
    if not query:
        return None
    if language == "es":
        query_map: dict[str, str] = {
            "URGENCY_TRIAGE": "detención inmigración fecha de corte emergencia",
            "INTAKE": f"proceso documentos caso de inmigración {case_type or ''}",
            "CONSULTATION_PITCH": f"consulta abogado inmigración {case_type or 'servicios'}",
            "BOOKING": "programar cita abogado inmigración",
            "CONFIRMATION": "confirmación cita firma de abogados",
        }
        query = query_map.get(phase, query)
    return query


def _chunk_to_dict(chunk: ChunkResult) -> dict[str, Any]:
    return {
        "id": chunk.id,
        "document_id": chunk.document_id,
        "parent_chunk_id": chunk.parent_chunk_id,
        "content": chunk.content,
        "context_prefix": chunk.context_prefix,
        "language": chunk.language,
        "source_type": chunk.source_type,
        "title": chunk.title,
        "quality_score": chunk.quality_score,
        "rerank_score": chunk.rerank_score,
        "rrf_score": chunk.rrf_score,
    }
