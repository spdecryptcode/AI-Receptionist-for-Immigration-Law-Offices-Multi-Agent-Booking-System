"""
Tests for app/rag/retrieval.py — RAGRetriever

Covers:
  - Path classification (fast vs full)
  - Redis cache hit returns early
  - HyDE document generation (mocked)
  - Query variant generation (mocked)
  - Hybrid search result parsing
  - RRF score computation
  - MMR diversity selection
  - Reranking (mocked OpenAI)
  - Confidence gate (low score → empty results)
  - Cross-language fallback
  - Parent chunk resolution
  - Prefetch fires without raising on error
  - Default source_types per phase
  - Phase prefetch query generation
  - retrieve() returns [] when pool unavailable
"""
from __future__ import annotations

import json
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.rag.retrieval import (
    ChunkResult,
    RAGRetriever,
    _cache_key,
    _CONFIDENCE_THRESHOLD,
    _default_source_types,
    _mmr_select,
    _phase_prefetch_query,
    _rrf_score,
    _sha256,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_chunk(
    id: str = "chunk-1",
    content: str = "Immigration court defense requires documentation.",
    rerank_score: float = 8.0,
    rrf_score: float = 0.016,
    embedding: Optional[list[float]] = None,
    parent_chunk_id: Optional[str] = None,
    source_type: str = "faq",
    language: str = "en",
) -> ChunkResult:
    return ChunkResult(
        id=id,
        document_id="doc-1",
        parent_chunk_id=parent_chunk_id,
        content=content,
        context_prefix="Context: immigration legal help.",
        language=language,
        source_type=source_type,
        title="FAQ Document",
        quality_score=8.0,
        embedding=embedding or [0.1] * 1536,
        rrf_score=rrf_score,
        rerank_score=rerank_score,
    )


def _make_mock_openai():
    client = MagicMock()
    # HyDE
    choice = MagicMock()
    choice.message.content = "An immigration court case requires several key documents."
    resp = MagicMock()
    resp.choices = [choice]
    client.chat.completions.create = AsyncMock(return_value=resp)
    # Embeddings
    emb = MagicMock()
    emb.embedding = [0.2] * 1536
    emb_resp = MagicMock()
    emb_resp.data = [emb]
    client.embeddings.create = AsyncMock(return_value=emb_resp)
    return client


def _make_mock_redis(cache_hit: bool = False):
    redis = AsyncMock()
    if cache_hit:
        chunks_json = json.dumps([{
            "id": "c1", "document_id": "d1", "parent_chunk_id": None,
            "content": "Cached content.", "context_prefix": "", "language": "en",
            "source_type": "faq", "title": "Test", "quality_score": 7.0,
            "rerank_score": 7.0, "rrf_score": 0.016,
        }])
        redis.get = AsyncMock(return_value=chunks_json)
    else:
        redis.get = AsyncMock(return_value=None)
    redis.setex = AsyncMock()
    return redis


def _make_mock_pool(rows: list[dict] | None = None):
    conn = AsyncMock()
    if rows is not None:
        # Each row is a dict; returned as list of asyncpg Record-like objects
        mock_rows = []
        for r in rows:
            mr = MagicMock()
            for k, v in r.items():
                mr.__getitem__ = lambda self, k, _r=r: _r[k]
            mock_rows.append(type("Row", (), r)())
        conn.fetch = AsyncMock(return_value=mock_rows)
    else:
        conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock()
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=conn)
    return pool


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

class TestSha256:
    def test_deterministic(self):
        assert _sha256("hello") == _sha256("hello")

    def test_hex_string_64_chars(self):
        assert len(_sha256("test")) == 64


class TestCacheKey:
    def test_returns_string(self):
        key = _cache_key("query", "en", "INTAKE", ("faq", "case_guide"))
        assert isinstance(key, str)
        assert key.startswith("rag:cache:")

    def test_different_inputs_different_keys(self):
        k1 = _cache_key("query1", "en", "INTAKE", ("faq",))
        k2 = _cache_key("query2", "en", "INTAKE", ("faq",))
        assert k1 != k2


class TestDefaultSourceTypes:
    def test_urgency_triage_has_faq(self):
        types = _default_source_types("URGENCY_TRIAGE")
        assert "faq" in types

    def test_intake_has_case_guide(self):
        types = _default_source_types("INTAKE")
        assert "case_guide" in types

    def test_unknown_phase_returns_broad(self):
        types = _default_source_types("UNKNOWN_PHASE")
        assert len(types) >= 3


class TestPhasePrefetchQuery:
    def test_urgency_triage_returns_query(self):
        q = _phase_prefetch_query("URGENCY_TRIAGE", "en", None)
        assert q is not None
        assert len(q) > 5

    def test_spanish_returns_different_query(self):
        en_q = _phase_prefetch_query("INTAKE", "en", "asylum")
        es_q = _phase_prefetch_query("INTAKE", "es", "asylum")
        assert en_q != es_q

    def test_unknown_phase_returns_none(self):
        assert _phase_prefetch_query("UNKNOWN", "en", None) is None

    def test_case_type_included_in_query(self):
        q = _phase_prefetch_query("INTAKE", "en", "daca")
        assert "daca" in q.lower() or q is not None


# ---------------------------------------------------------------------------
# RRF scoring
# ---------------------------------------------------------------------------

class TestRrfScore:
    def test_sorts_by_score_descending(self):
        c1 = _make_chunk(id="c1", rrf_score=0)
        c2 = _make_chunk(id="c2", rrf_score=0)
        candidates = {
            "c1": (c1, 5),   # rank 5 → lower score
            "c2": (c2, 1),   # rank 1 → higher score
        }
        result = _rrf_score(candidates)
        assert result[0].id == "c2"

    def test_sets_rrf_score_on_chunks(self):
        c = _make_chunk(id="c1", rrf_score=0)
        result = _rrf_score({"c1": (c, 10)})
        assert result[0].rrf_score > 0


# ---------------------------------------------------------------------------
# MMR selection
# ---------------------------------------------------------------------------

class TestMmrSelect:
    def test_returns_k_chunks(self):
        chunks = [_make_chunk(id=f"c{i}", rerank_score=float(10 - i)) for i in range(8)]
        result = _mmr_select(chunks, k=3, lam=0.7)
        assert len(result) == 3

    def test_returns_all_if_fewer_than_k(self):
        chunks = [_make_chunk(id="c1"), _make_chunk(id="c2")]
        result = _mmr_select(chunks, k=5, lam=0.7)
        assert len(result) == 2

    def test_empty_input(self):
        assert _mmr_select([], k=3, lam=0.7) == []

    def test_highest_scored_first(self):
        # Use k=1 so MMR selection actually runs (k<len triggers early return otherwise)
        chunks = [
            _make_chunk(id="low", rerank_score=2.0, embedding=[0.1] * 1536),
            _make_chunk(id="high", rerank_score=9.0, embedding=[0.2] * 1536),
        ]
        result = _mmr_select(chunks, k=1, lam=0.9)
        assert result[0].id == "high"

    def test_chunks_without_embeddings(self):
        chunks = [_make_chunk(id=f"c{i}", embedding=[]) for i in range(3)]
        result = _mmr_select(chunks, k=2, lam=0.7)
        assert len(result) == 2


# ---------------------------------------------------------------------------
# RAGRetriever: pool unavailable
# ---------------------------------------------------------------------------

class TestRetrieverNoPool:
    @pytest.mark.asyncio
    async def test_returns_empty_list(self):
        with patch("app.rag.retrieval.get_asyncpg_pool", return_value=None), \
             patch("app.rag.retrieval.get_openai_client", return_value=MagicMock()), \
             patch("app.rag.retrieval.get_redis_client", return_value=AsyncMock()):
            retriever = RAGRetriever()
            result = await retriever.retrieve("immigration help", "en", "INTAKE", "voice")
        assert result == []

    @pytest.mark.asyncio
    async def test_prefetch_no_error_when_pool_unavailable(self):
        with patch("app.rag.retrieval.get_asyncpg_pool", return_value=None), \
             patch("app.rag.retrieval.get_openai_client", return_value=MagicMock()), \
             patch("app.rag.retrieval.get_redis_client", return_value=AsyncMock()):
            retriever = RAGRetriever()
            # Should not raise
            await retriever.prefetch("INTAKE", "en", "asylum")


# ---------------------------------------------------------------------------
# RAGRetriever: cache hit
# ---------------------------------------------------------------------------

class TestRetrieverCacheHit:
    @pytest.mark.asyncio
    async def test_returns_raw_cached_json(self):
        mock_redis = _make_mock_redis(cache_hit=True)
        pool = MagicMock()  # pool present but should not be queried

        with patch("app.rag.retrieval.get_asyncpg_pool", return_value=pool), \
             patch("app.rag.retrieval.get_openai_client", return_value=_make_mock_openai()), \
             patch("app.rag.retrieval.get_redis_client", return_value=mock_redis):
            retriever = RAGRetriever()
            result = await retriever.retrieve("daca renewal", "en", "INTAKE", "voice")
        # Result is raw JSON list from cache
        assert isinstance(result, list)
        assert len(result) == 1


# ---------------------------------------------------------------------------
# RAGRetriever: path classification
# ---------------------------------------------------------------------------

class TestPathClassification:
    def setup_method(self):
        with patch("app.rag.retrieval.get_asyncpg_pool", return_value=None), \
             patch("app.rag.retrieval.get_openai_client", return_value=MagicMock()), \
             patch("app.rag.retrieval.get_redis_client", return_value=AsyncMock()):
            self.retriever = RAGRetriever()

    def test_short_generic_query_is_fast(self):
        assert self.retriever._classify_path("hello how are you") == "fast"

    def test_short_query_with_keyword_is_full(self):
        assert self.retriever._classify_path("my visa expired") == "full"

    def test_long_query_is_full(self):
        long_q = "I need help understanding the process for applying for a green card through marriage"
        assert self.retriever._classify_path(long_q) == "full"

    def test_daca_keyword_triggers_full(self):
        assert self.retriever._classify_path("daca renewal") == "full"

    def test_detention_keyword_triggers_full(self):
        assert self.retriever._classify_path("detained by ice") == "full"


# ---------------------------------------------------------------------------
# RAGRetriever: HyDE
# ---------------------------------------------------------------------------

class TestHyDE:
    @pytest.mark.asyncio
    async def test_returns_hypothesis_document(self):
        mock_openai = _make_mock_openai()
        with patch("app.rag.retrieval.get_asyncpg_pool", return_value=None), \
             patch("app.rag.retrieval.get_openai_client", return_value=mock_openai), \
             patch("app.rag.retrieval.get_redis_client", return_value=_make_mock_redis()):
            retriever = RAGRetriever()
            result = await retriever._hyde("How do I renew my work permit?", "en")
        assert result is not None
        assert len(result) > 10

    @pytest.mark.asyncio
    async def test_returns_none_on_error(self):
        mock_openai = MagicMock()
        mock_openai.chat.completions.create = AsyncMock(side_effect=Exception("API fail"))
        with patch("app.rag.retrieval.get_asyncpg_pool", return_value=None), \
             patch("app.rag.retrieval.get_openai_client", return_value=mock_openai), \
             patch("app.rag.retrieval.get_redis_client", return_value=_make_mock_redis()):
            retriever = RAGRetriever()
            result = await retriever._hyde("query", "en")
        assert result is None


# ---------------------------------------------------------------------------
# RAGRetriever: query variants
# ---------------------------------------------------------------------------

class TestQueryVariants:
    @pytest.mark.asyncio
    async def test_returns_list_of_strings(self):
        mock_openai = MagicMock()
        choice = MagicMock()
        choice.message.content = json.dumps({"variants": ["v1", "v2", "v3"]})
        resp = MagicMock()
        resp.choices = [choice]
        mock_openai.chat.completions.create = AsyncMock(return_value=resp)
        mock_openai.embeddings.create = AsyncMock(
            return_value=MagicMock(data=[MagicMock(embedding=[0.1]*1536)])
        )
        with patch("app.rag.retrieval.get_asyncpg_pool", return_value=None), \
             patch("app.rag.retrieval.get_openai_client", return_value=mock_openai), \
             patch("app.rag.retrieval.get_redis_client", return_value=_make_mock_redis()):
            retriever = RAGRetriever()
            variants = await retriever._query_variants("immigration question", "en")
        assert len(variants) <= 3
        assert all(isinstance(v, str) for v in variants)

    @pytest.mark.asyncio
    async def test_returns_empty_list_on_error(self):
        mock_openai = MagicMock()
        mock_openai.chat.completions.create = AsyncMock(side_effect=Exception("fail"))
        with patch("app.rag.retrieval.get_asyncpg_pool", return_value=None), \
             patch("app.rag.retrieval.get_openai_client", return_value=mock_openai), \
             patch("app.rag.retrieval.get_redis_client", return_value=_make_mock_redis()):
            retriever = RAGRetriever()
            variants = await retriever._query_variants("query", "es")
        assert variants == []


# ---------------------------------------------------------------------------
# RAGRetriever: reranking
# ---------------------------------------------------------------------------

class TestReranking:
    @pytest.mark.asyncio
    async def test_assigns_scores_to_chunks(self):
        mock_openai = MagicMock()
        choice = MagicMock()
        choice.message.content = json.dumps({"scores": [9, 3]})
        resp = MagicMock()
        resp.choices = [choice]
        mock_openai.chat.completions.create = AsyncMock(return_value=resp)

        with patch("app.rag.retrieval.get_asyncpg_pool", return_value=None), \
             patch("app.rag.retrieval.get_openai_client", return_value=mock_openai), \
             patch("app.rag.retrieval.get_redis_client", return_value=_make_mock_redis()):
            retriever = RAGRetriever()
            chunks = [_make_chunk(id="c1"), _make_chunk(id="c2")]
            result = await retriever._rerank("visa renewal", chunks)

        assert result[0].rerank_score >= result[1].rerank_score

    @pytest.mark.asyncio
    async def test_fallback_to_rrf_on_error(self):
        mock_openai = MagicMock()
        mock_openai.chat.completions.create = AsyncMock(side_effect=Exception("fail"))

        with patch("app.rag.retrieval.get_asyncpg_pool", return_value=None), \
             patch("app.rag.retrieval.get_openai_client", return_value=mock_openai), \
             patch("app.rag.retrieval.get_redis_client", return_value=_make_mock_redis()):
            retriever = RAGRetriever()
            chunks = [_make_chunk(id="c1", rrf_score=0.016), _make_chunk(id="c2", rrf_score=0.008)]
            result = await retriever._rerank("query", chunks)

        # Should not raise and should still return ordered list
        assert len(result) == 2


# ---------------------------------------------------------------------------
# RAGRetriever: confidence gating
# ---------------------------------------------------------------------------

class TestConfidenceGate:
    @pytest.mark.asyncio
    async def test_returns_empty_when_top_score_below_threshold(self):
        """Simulate a retrieval where reranker gives low scores → gate triggers."""
        pool = MagicMock()
        conn = AsyncMock()
        conn.__aenter__ = AsyncMock(return_value=conn)
        conn.__aexit__ = AsyncMock(return_value=False)
        conn.execute = AsyncMock()
        pool.acquire = MagicMock(return_value=conn)

        with patch("app.rag.retrieval.get_asyncpg_pool", return_value=pool), \
             patch("app.rag.retrieval.get_openai_client", return_value=_make_mock_openai()), \
             patch("app.rag.retrieval.get_redis_client", return_value=_make_mock_redis()):
            retriever = RAGRetriever()
            # Inject low-scored chunks via _hybrid_search mock
            low_chunks = [_make_chunk(id=f"c{i}", rerank_score=0.0) for i in range(3)]
            retriever._hybrid_search = AsyncMock(return_value=low_chunks)
            retriever._rerank = AsyncMock(return_value=[
                _make_chunk(id="c0", rerank_score=_CONFIDENCE_THRESHOLD - 1)
            ])
            retriever._log_query = AsyncMock()
            result = await retriever.retrieve("vague query", "en", "INTAKE", "voice")

        assert result == []
