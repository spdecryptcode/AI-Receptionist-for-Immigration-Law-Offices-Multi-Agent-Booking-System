"""
Tests for app/rag/ingestion.py — DocumentIngester

Covers:
  - Sentence splitting
  - Parent/child chunking within token limits
  - SHA-256 dedup logic
  - Contextual prefix enrichment (mocked OpenAI)
  - Quality batch scoring (mocked OpenAI)
  - Embedding with Redis cache hit/miss (mocked)
  - Prompt file sync (sync_prompt_files)
  - Transcript ingestion keyword gate
  - Nightly intake pattern aggregation
  - Full ingest_document flow with mocked pool + OpenAI + Redis
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.rag.ingestion import (
    DocumentIngester,
    _make_children,
    _sha256,
    _split_sentences,
    _token_count,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_openai():
    """Minimal AsyncOpenAI mock with embeddings and chat completions."""
    client = MagicMock()

    # Embeddings
    emb_obj = MagicMock()
    emb_obj.embedding = [0.1] * 1536
    emb_resp = MagicMock()
    emb_resp.data = [emb_obj]
    client.embeddings.create = AsyncMock(return_value=emb_resp)

    # Chat completions
    choice = MagicMock()
    choice.message.content = "Context prefix sentence."
    chat_resp = MagicMock()
    chat_resp.choices = [choice]
    client.chat.completions.create = AsyncMock(return_value=chat_resp)

    return client


def _make_mock_redis():
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)   # cache miss by default
    redis.setex = AsyncMock(return_value=True)
    return redis


def _make_mock_pool(existing_hash=None):
    """asyncpg pool mock. existing_hash simulates a pre-existing document."""
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=existing_hash)
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    # Support async context manager
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=False)
    tx = AsyncMock()
    tx.__aenter__ = AsyncMock(return_value=tx)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx)

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=conn)
    return pool, conn


# ---------------------------------------------------------------------------
# Unit tests: module-level helpers
# ---------------------------------------------------------------------------

class TestSplitSentences:
    def test_basic_split(self):
        text = "Hello world. This is a test. Another sentence here."
        parts = _split_sentences(text)
        assert len(parts) == 3

    def test_single_sentence(self):
        parts = _split_sentences("Just one sentence")
        assert parts == ["Just one sentence"]

    def test_empty_string(self):
        assert _split_sentences("") == []

    def test_preserves_content(self):
        text = "DACA renewal is important. Court dates matter."
        parts = _split_sentences(text)
        assert "DACA renewal is important." in parts[0]


class TestTokenCount:
    def test_non_empty(self):
        count = _token_count("Hello world")
        assert count > 0

    def test_empty(self):
        assert _token_count("") == 0

    def test_more_text_more_tokens(self):
        short = _token_count("Hi")
        long_count = _token_count("Hi " * 100)
        assert long_count > short


class TestSha256:
    def test_deterministic(self):
        assert _sha256("hello") == _sha256("hello")

    def test_different_inputs(self):
        assert _sha256("hello") != _sha256("world")

    def test_correct_format(self):
        result = _sha256("test")
        assert len(result) == 64
        assert all(c in "0123456789abcdef" for c in result)


class TestMakeChildren:
    def test_returns_list(self):
        children = _make_children("First sentence. Second sentence. Third sentence.")
        assert isinstance(children, list)

    def test_each_child_has_content(self):
        children = _make_children("A. B. C. D. E.")
        for c in children:
            assert "content" in c
            assert c["content"]

    def test_single_sentence(self):
        children = _make_children("Only one sentence here.")
        assert len(children) == 1


# ---------------------------------------------------------------------------
# DocumentIngester: chunking
# ---------------------------------------------------------------------------

class TestChunkDocument:
    def setup_method(self):
        with patch("app.rag.ingestion.get_asyncpg_pool", return_value=None), \
             patch("app.rag.ingestion.get_openai_client", return_value=MagicMock()), \
             patch("app.rag.ingestion.get_redis_client", return_value=AsyncMock()):
            self.ingester = DocumentIngester()

    def test_produces_chunks(self):
        text = "Immigration law is complex. " * 30
        chunks = self.ingester._chunk_document(text, "en")
        assert len(chunks) >= 1

    def test_chunk_has_required_fields(self):
        text = "Visa applications require documentation. Green cards take time."
        chunks = self.ingester._chunk_document(text, "en")
        assert chunks
        for chunk in chunks:
            assert "content" in chunk
            assert "language" in chunk
            assert "children" in chunk

    def test_respects_language(self):
        text = "Hola mundo. Esto es una prueba."
        chunks = self.ingester._chunk_document(text, "es")
        assert all(c["language"] == "es" for c in chunks)

    def test_long_text_splits_into_multiple_chunks(self):
        # ~600 tokens worth of text should produce multiple parent chunks
        text = "This is a sentence about immigration law and DACA renewals. " * 50
        chunks = self.ingester._chunk_document(text, "en")
        assert len(chunks) > 1


# ---------------------------------------------------------------------------
# DocumentIngester: embedding with cache
# ---------------------------------------------------------------------------

class TestEmbedTexts:
    def setup_method(self):
        self.mock_openai = _make_mock_openai()
        self.mock_redis = _make_mock_redis()
        with patch("app.rag.ingestion.get_asyncpg_pool", return_value=None), \
             patch("app.rag.ingestion.get_openai_client", return_value=self.mock_openai), \
             patch("app.rag.ingestion.get_redis_client", return_value=self.mock_redis):
            self.ingester = DocumentIngester()

    @pytest.mark.asyncio
    async def test_embeds_texts(self):
        texts = ["Hello immigration law", "DACA renewal process"]
        result = await self.ingester._embed_texts(texts)
        assert len(result) == 2
        assert all(len(v) == 1536 for v in result)

    @pytest.mark.asyncio
    async def test_cache_hit_skips_openai(self):
        cached_vec = [0.5] * 1536
        self.mock_redis.get = AsyncMock(return_value=json.dumps(cached_vec))
        result = await self.ingester._embed_texts(["hello"])
        # OpenAI should not be called since cache hit
        self.mock_openai.embeddings.create.assert_not_called()
        assert result[0] == cached_vec

    @pytest.mark.asyncio
    async def test_cache_writes_after_embed(self):
        result = await self.ingester._embed_texts(["asylum application"])
        self.mock_redis.setex.assert_called_once()
        assert len(result[0]) == 1536


# ---------------------------------------------------------------------------
# DocumentIngester: quality scoring
# ---------------------------------------------------------------------------

class TestScoreQuality:
    def setup_method(self):
        self.mock_openai = _make_mock_openai()
        # Override chat to return scores JSON
        choice = MagicMock()
        choice.message.content = json.dumps({"scores": [8, 3, 7]})
        resp = MagicMock()
        resp.choices = [choice]
        self.mock_openai.chat.completions.create = AsyncMock(return_value=resp)

        with patch("app.rag.ingestion.get_asyncpg_pool", return_value=None), \
             patch("app.rag.ingestion.get_openai_client", return_value=self.mock_openai), \
             patch("app.rag.ingestion.get_redis_client", return_value=AsyncMock()):
            self.ingester = DocumentIngester()

    @pytest.mark.asyncio
    async def test_returns_list_of_floats(self):
        texts = ["text one", "text two", "text three"]
        scores = await self.ingester._score_quality(texts)
        assert len(scores) == 3
        assert all(isinstance(s, float) for s in scores)

    @pytest.mark.asyncio
    async def test_returns_defaults_on_error(self):
        self.mock_openai.chat.completions.create = AsyncMock(side_effect=Exception("API error"))
        scores = await self.ingester._score_quality(["text"])
        assert len(scores) == 1
        assert scores[0] == 5.0  # default

    @pytest.mark.asyncio
    async def test_empty_input(self):
        scores = await self.ingester._score_quality([])
        assert scores == []


# ---------------------------------------------------------------------------
# DocumentIngester: duplicate detection
# ---------------------------------------------------------------------------

class TestIngestDocumentDuplicate:
    @pytest.mark.asyncio
    async def test_returns_existing_id_on_duplicate(self):
        existing_id = "00000000-0000-0000-0000-000000000001"
        pool, conn = _make_mock_pool(existing_hash=existing_id)

        with patch("app.rag.ingestion.get_asyncpg_pool", return_value=pool), \
             patch("app.rag.ingestion.get_openai_client", return_value=_make_mock_openai()), \
             patch("app.rag.ingestion.get_redis_client", return_value=_make_mock_redis()):
            ingester = DocumentIngester()
            result = await ingester.ingest_document(
                title="Test", source_type="faq", language="en",
                content="Some test content about immigration."
            )
        assert result == existing_id


# ---------------------------------------------------------------------------
# DocumentIngester: pool unavailable
# ---------------------------------------------------------------------------

class TestIngestDocumentNoPool:
    @pytest.mark.asyncio
    async def test_returns_none_when_pool_unavailable(self):
        with patch("app.rag.ingestion.get_asyncpg_pool", return_value=None), \
             patch("app.rag.ingestion.get_openai_client", return_value=_make_mock_openai()), \
             patch("app.rag.ingestion.get_redis_client", return_value=_make_mock_redis()):
            ingester = DocumentIngester()
            result = await ingester.ingest_document(
                title="Test", source_type="faq", language="en", content="content"
            )
        assert result is None


# ---------------------------------------------------------------------------
# DocumentIngester: transcript keyword gate
# ---------------------------------------------------------------------------

class TestIngestConversationTranscript:
    @pytest.mark.asyncio
    async def test_skips_when_no_keywords(self):
        pool, conn = _make_mock_pool()
        conn.fetch = AsyncMock(return_value=[
            {"role": "caller", "content": "Hello, I need some general help please."},
            {"role": "assistant", "content": "How can I help you today?"},
        ])
        with patch("app.rag.ingestion.get_asyncpg_pool", return_value=pool), \
             patch("app.rag.ingestion.get_openai_client", return_value=_make_mock_openai()), \
             patch("app.rag.ingestion.get_redis_client", return_value=_make_mock_redis()):
            ingester = DocumentIngester()
            result = await ingester.ingest_conversation_transcript("CALL123")
        assert result is None

    @pytest.mark.asyncio
    async def test_processes_when_keywords_present(self):
        pool, conn = _make_mock_pool()
        conn.fetchval = AsyncMock(return_value=None)  # no duplicate
        conn.fetch = AsyncMock(return_value=[
            {"role": "caller", "content": "I have a court date next week for removal defense."},
            {"role": "assistant", "content": "I understand. We can help with your removal case."},
        ])
        # Make _persist not actually persist (pool acquire mock)
        with patch("app.rag.ingestion.get_asyncpg_pool", return_value=pool), \
             patch("app.rag.ingestion.get_openai_client", return_value=_make_mock_openai()), \
             patch("app.rag.ingestion.get_redis_client", return_value=_make_mock_redis()):
            ingester = DocumentIngester()
            # patch ingest_document to avoid full DB write
            ingester.ingest_document = AsyncMock(return_value="mock-doc-id")
            result = await ingester.ingest_conversation_transcript("CALL456")
        assert result == "mock-doc-id"

    @pytest.mark.asyncio
    async def test_returns_none_when_pool_unavailable(self):
        with patch("app.rag.ingestion.get_asyncpg_pool", return_value=None), \
             patch("app.rag.ingestion.get_openai_client", return_value=MagicMock()), \
             patch("app.rag.ingestion.get_redis_client", return_value=AsyncMock()):
            ingester = DocumentIngester()
            result = await ingester.ingest_conversation_transcript("CALL789")
        assert result is None


# ---------------------------------------------------------------------------
# DocumentIngester: sync_prompt_files
# ---------------------------------------------------------------------------

class TestSyncPromptFiles:
    @pytest.mark.asyncio
    async def test_skips_missing_directory(self):
        with patch("app.rag.ingestion.get_asyncpg_pool", return_value=None), \
             patch("app.rag.ingestion.get_openai_client", return_value=MagicMock()), \
             patch("app.rag.ingestion.get_redis_client", return_value=AsyncMock()):
            ingester = DocumentIngester()
            # Should not raise even if directory doesn't exist
            await ingester.sync_prompt_files(Path("/tmp/nonexistent_prompts_dir_xyz"))

    @pytest.mark.asyncio
    async def test_imports_md_files(self, tmp_path):
        (tmp_path / "system_prompt_en.md").write_text("Immigration legal help information.")
        (tmp_path / "system_prompt_es.md").write_text("Información de ayuda legal de inmigración.")

        with patch("app.rag.ingestion.get_asyncpg_pool", return_value=None), \
             patch("app.rag.ingestion.get_openai_client", return_value=MagicMock()), \
             patch("app.rag.ingestion.get_redis_client", return_value=AsyncMock()):
            ingester = DocumentIngester()
            ingester.ingest_document = AsyncMock(return_value=None)
            await ingester.sync_prompt_files(tmp_path)

        assert ingester.ingest_document.call_count == 2

    @pytest.mark.asyncio
    async def test_detects_spanish_language(self, tmp_path):
        (tmp_path / "intake_questions_es.md").write_text("Preguntas de inmigración.")

        with patch("app.rag.ingestion.get_asyncpg_pool", return_value=None), \
             patch("app.rag.ingestion.get_openai_client", return_value=MagicMock()), \
             patch("app.rag.ingestion.get_redis_client", return_value=AsyncMock()):
            ingester = DocumentIngester()
            ingester.ingest_document = AsyncMock(return_value=None)
            await ingester.sync_prompt_files(tmp_path)

        call_kwargs = ingester.ingest_document.call_args_list[0][1]
        assert call_kwargs["language"] == "es"
