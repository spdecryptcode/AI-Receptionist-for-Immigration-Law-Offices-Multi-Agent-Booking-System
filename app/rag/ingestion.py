"""
Document ingestion pipeline for the RAG knowledge base.

Responsibilities:
  - Sentence-boundary aware parent/child chunking (tiktoken-budgeted)
  - Contextual prefix enrichment via GPT-4o-mini
  - Quality scoring via GPT-4o-mini (batch)
  - Embedding generation with Redis dedup cache (SHA-256 key, 7-day TTL)
  - Atomic versioning: delete old chunks then insert new ones in one transaction
  - Post-call transcript auto-ingestion
  - Nightly intake-pattern document aggregation
  - Startup prompt-file sync
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import tiktoken

from app.config import settings
from app.dependencies import get_asyncpg_pool, get_openai_client, get_redis_client

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_PARENT_TOKEN_LIMIT = 400      # max tokens per parent chunk
_CHILD_TOKEN_LIMIT = 100       # max tokens per child chunk (for fine-grained retrieval)
_EMBED_MODEL = "text-embedding-3-small"
_EMBED_CACHE_TTL = 60 * 60 * 24 * 7   # 7 days
_MINI_MODEL = "gpt-4o-mini"
_QUALITY_BATCH_SIZE = 20
_CASE_RE = re.compile(
    r"\b(visa|court|ice|detained|daca|deportation|i-?485|i-?130|i-?765|i-?131|"
    r"n-?400|work permit|asylum|green card|renewal|denied|petition|removal|ead|"
    r"travel document|sponsor|h-?1b|tps|parole|naturalization)\b",
    re.IGNORECASE,
)

# tiktoken encoding shared across calls
try:
    _enc = tiktoken.get_encoding("cl100k_base")
except Exception:
    _enc = None


def _token_count(text: str) -> int:
    if _enc is None:
        return len(text) // 4  # rough fallback
    return len(_enc.encode(text))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Sentence splitter
# ---------------------------------------------------------------------------
_SENT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-ZÁÉÍÓÚÜ])")


def _split_sentences(text: str) -> list[str]:
    """Split text into sentences using punctuation boundaries."""
    parts = _SENT_RE.split(text.strip())
    return [p.strip() for p in parts if p.strip()]


# ---------------------------------------------------------------------------
# DocumentIngester
# ---------------------------------------------------------------------------

class DocumentIngester:
    """
    Full ingestion pipeline: chunk → enrich → score → embed → persist.
    Requires an asyncpg pool for DB writes and vector inserts.
    """

    def __init__(self) -> None:
        self._pool = get_asyncpg_pool()
        self._openai = get_openai_client()
        self._redis = get_redis_client()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def ingest_document(
        self,
        title: str,
        source_type: str,
        language: str,
        content: str,
        metadata: Optional[dict[str, Any]] = None,
        expires_at: Optional[datetime] = None,
    ) -> Optional[str]:
        """
        Ingest a document into the knowledge base.
        Returns the document UUID or None if skipped (duplicate).
        """
        if self._pool is None:
            logger.warning("ingest_document: asyncpg pool unavailable — skipping")
            return None

        content_hash = _sha256(content)

        # Check for duplicate
        async with self._pool.acquire() as conn:
            existing = await conn.fetchval(
                "SELECT id FROM knowledge_documents WHERE content_hash = $1",
                content_hash,
            )
            if existing:
                logger.debug(f"ingest_document: duplicate hash for '{title}' — skipping")
                return str(existing)

        # Build parent/child chunk pairs
        chunks = self._chunk_document(content, language)
        if not chunks:
            logger.warning(f"ingest_document: no chunks produced for '{title}'")
            return None

        # Enrich parent chunks with contextual prefix (concurrent)
        enriched = await self._enrich_chunks(chunks, title)

        # Score quality in batch
        scores = await self._score_quality([ec["content"] for ec in enriched])

        # Embed all chunk texts (parent + children) via cached OpenAI calls
        all_texts = []
        for item in enriched:
            prefix = item.get("context_prefix", "")
            embed_text = f"{prefix}\n{item['content']}".strip() if prefix else item["content"]
            item["embed_text"] = embed_text
            all_texts.append(embed_text)
            for child in item.get("children", []):
                child["embed_text"] = embed_text  # children share parent embedding for dedup
                all_texts.append(child["content"])

        embeddings = await self._embed_texts(all_texts)

        # Persist atomically
        doc_id = await self._persist(
            title=title,
            source_type=source_type,
            language=language,
            content_hash=content_hash,
            metadata=metadata or {},
            expires_at=expires_at,
            enriched_chunks=enriched,
            scores=scores,
            embeddings=embeddings,
        )
        logger.info(f"ingest_document: '{title}' ({source_type}) → doc_id={doc_id}, chunks={len(enriched)}")
        return doc_id

    async def ingest_conversation_transcript(
        self, call_sid: str
    ) -> Optional[str]:
        """
        Index a completed conversation transcript into the knowledge base.
        Only indexes calls with meaningful intake data.
        """
        if self._pool is None:
            return None
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT role, content
                FROM conversation_messages
                WHERE call_sid = $1
                ORDER BY turn_index
                """,
                call_sid,
            )
            # Also pull caller name and outcome for a richer title
            conv_meta = await conn.fetchrow(
                "SELECT caller_name, call_outcome, started_at FROM conversations WHERE call_sid=$1",
                call_sid,
            )
        if not rows:
            return None

        turns = [
            f"{'Caller' if r['role'] == 'caller' else 'Agent'}: {r['content']}"
            for r in rows
        ]
        transcript = "\n".join(turns)

        caller = (conv_meta["caller_name"] or "") if conv_meta else ""
        outcome = (conv_meta["call_outcome"] or "") if conv_meta else ""
        date_str = str(conv_meta["started_at"])[:10] if (conv_meta and conv_meta["started_at"]) else ""
        label_parts = [p for p in [caller, date_str, outcome] if p]
        title = f"Call transcript {call_sid[:8]}" + (f" ({', '.join(label_parts)})" if label_parts else "")
        # Prepend caller name so RAG can find it by name
        header = f"Caller: {caller}\n" if caller else ""
        content_with_header = header + transcript

        return await self.ingest_document(
            title=title,
            source_type="conversation_transcript",
            language="en",
            content=content_with_header,
            metadata={"call_sid": call_sid, "caller_name": caller},
        )

    async def aggregate_intake_patterns(self) -> None:
        """
        Nightly job: aggregate common intake Q&A patterns from recent bookings
        and upsert them as FAQ documents in the knowledge base.
        """
        if self._pool is None:
            return
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT
                        ii.case_type,
                        cv.urgency_label,
                        COUNT(*) AS frequency
                    FROM immigration_intakes ii
                    JOIN conversations cv ON cv.call_sid = ii.call_sid
                    WHERE ii.created_at > NOW() - INTERVAL '30 days'
                      AND ii.case_type IS NOT NULL
                    GROUP BY ii.case_type, cv.urgency_label
                    HAVING COUNT(*) >= 2
                    ORDER BY frequency DESC
                    LIMIT 50
                    """,
                )

            if not rows:
                return

            lines = ["## Common Immigration Questions — Aggregated from Recent Calls\n"]
            for r in rows:
                urgency = r["urgency_label"] or "routine"
                lines.append(
                    f"- {r['case_type']}: urgency={urgency}, "
                    f"frequency={r['frequency']} calls in past 30 days"
                )

            content = "\n".join(lines)
            await self.ingest_document(
                title=f"Intake patterns {datetime.now(timezone.utc).strftime('%Y-%m')}",
                source_type="faq",
                language="en",
                content=content,
                metadata={"auto_generated": True},
            )
            logger.info(f"aggregate_intake_patterns: {len(rows)} patterns indexed")
        except Exception as exc:
            logger.error(f"aggregate_intake_patterns failed: {exc}")

    async def sync_prompt_files(self, prompts_dir: Path) -> None:
        """
        Startup: import all .md files from the prompts directory as firm_policy docs.
        Uses content hash to skip unchanged files.
        """
        if not prompts_dir.exists():
            return
        tasks = []
        for md_file in sorted(prompts_dir.glob("*.md")):
            content = md_file.read_text(encoding="utf-8").strip()
            if not content:
                continue
            language = "es" if "_es" in md_file.stem else "en"
            tasks.append(
                self.ingest_document(
                    title=md_file.stem.replace("_", " ").title(),
                    source_type="firm_policy",
                    language=language,
                    content=content,
                    metadata={"source_file": md_file.name},
                )
            )
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            ok = sum(1 for r in results if isinstance(r, str))
            logger.info(f"sync_prompt_files: {ok}/{len(tasks)} files imported from {prompts_dir}")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _chunk_document(
        self, text: str, language: str
    ) -> list[dict[str, Any]]:
        """
        Split text into parent chunks (≤ _PARENT_TOKEN_LIMIT tokens) each with
        child sub-chunks (≤ _CHILD_TOKEN_LIMIT tokens).
        Returns: [{"content": str, "language": str, "children": [{"content": str}]}]
        """
        sentences = _split_sentences(text)
        chunks: list[dict[str, Any]] = []
        current: list[str] = []
        current_tokens = 0

        def flush() -> None:
            if current:
                parent_text = " ".join(current)
                children = _make_children(parent_text)
                chunks.append({
                    "content": parent_text,
                    "language": language,
                    "children": children,
                })
                current.clear()

        for sent in sentences:
            t = _token_count(sent)
            if current_tokens + t > _PARENT_TOKEN_LIMIT and current:
                flush()
                current_tokens = 0
            current.append(sent)
            current_tokens += t

        flush()
        return chunks

    async def _enrich_chunks(
        self, chunks: list[dict[str, Any]], doc_title: str
    ) -> list[dict[str, Any]]:
        """
        Add a contextual prefix to each parent chunk via GPT-4o-mini.
        Concurrently enriches up to 10 chunks at once.
        """
        sem = asyncio.Semaphore(10)

        async def _enrich_one(chunk: dict[str, Any]) -> dict[str, Any]:
            async with sem:
                try:
                    resp = await self._openai.chat.completions.create(
                        model=_MINI_MODEL,
                        messages=[
                            {
                                "role": "system",
                                "content": (
                                    "You are a legal document indexer. "
                                    "Write a single SHORT sentence (max 20 words) that describes "
                                    "what the following chunk is about, for use as a search prefix. "
                                    "Context: this is from the document titled: " + doc_title
                                ),
                            },
                            {"role": "user", "content": chunk["content"][:800]},
                        ],
                        max_tokens=40,
                        temperature=0.0,
                    )
                    chunk["context_prefix"] = resp.choices[0].message.content.strip()
                except Exception as exc:
                    logger.debug(f"_enrich_one failed: {exc}")
                    chunk.setdefault("context_prefix", "")
            return chunk

        return list(await asyncio.gather(*[_enrich_one(c) for c in chunks]))

    async def _score_quality(self, texts: list[str]) -> list[float]:
        """
        Batch-score chunk quality via a single GPT-4o-mini call.
        Returns a list of floats in [0, 10].
        """
        scores: list[float] = [5.0] * len(texts)
        if not texts:
            return scores

        batches = [texts[i : i + _QUALITY_BATCH_SIZE] for i in range(0, len(texts), _QUALITY_BATCH_SIZE)]
        result_offset = 0

        for batch in batches:
            numbered = "\n".join(
                f"{i + 1}. {t[:300]}" for i, t in enumerate(batch)
            )
            try:
                resp = await self._openai.chat.completions.create(
                    model=_MINI_MODEL,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "Rate each immigration legal text chunk for usefulness as RAG context. "
                                "Score 0-10 (10=highly specific, actionable immigration guidance; "
                                "0=generic filler). Reply ONLY with a JSON array of numbers: [8, 5, ...]"
                            ),
                        },
                        {"role": "user", "content": numbered},
                    ],
                    max_tokens=len(batch) * 6,
                    temperature=0.0,
                    response_format={"type": "json_object"},
                )
                raw = resp.choices[0].message.content.strip()
                parsed = json.loads(raw)
                # Accept either {"scores": [...]} or a bare array
                if isinstance(parsed, dict):
                    parsed = next(iter(parsed.values()))
                for j, score in enumerate(parsed[: len(batch)]):
                    scores[result_offset + j] = float(score)
            except Exception as exc:
                logger.debug(f"_score_quality batch failed: {exc}")
            result_offset += len(batch)

        return scores

    async def _embed_texts(self, texts: list[str]) -> list[list[float]]:
        """
        Embed a list of texts.  Checks Redis cache (SHA-256 key) first;
        batches uncached texts into a single OpenAI call.
        """
        redis = self._redis
        results: list[list[float] | None] = [None] * len(texts)
        uncached_indices: list[int] = []
        uncached_texts: list[str] = []

        # Cache lookup
        for i, text in enumerate(texts):
            cache_key = f"emb:cache:{_sha256(text)}"
            try:
                cached = await redis.get(cache_key)
                if cached:
                    results[i] = json.loads(cached)
                    continue
            except Exception:
                pass
            uncached_indices.append(i)
            uncached_texts.append(text)

        # Batch embed uncached
        if uncached_texts:
            try:
                resp = await self._openai.embeddings.create(
                    model=_EMBED_MODEL,
                    input=uncached_texts,
                )
                for j, emb_obj in enumerate(resp.data):
                    vec = emb_obj.embedding
                    idx = uncached_indices[j]
                    results[idx] = vec
                    cache_key = f"emb:cache:{_sha256(uncached_texts[j])}"
                    try:
                        await redis.setex(
                            cache_key,
                            _EMBED_CACHE_TTL,
                            json.dumps(vec),
                        )
                    except Exception:
                        pass
            except Exception as exc:
                logger.error(f"_embed_texts OpenAI error: {exc}")
                # fill remaining with zero vectors
                for idx in uncached_indices:
                    if results[idx] is None:
                        results[idx] = [0.0] * 1536

        return [r or [0.0] * 1536 for r in results]

    async def _persist(
        self,
        title: str,
        source_type: str,
        language: str,
        content_hash: str,
        metadata: dict[str, Any],
        expires_at: Optional[datetime],
        enriched_chunks: list[dict[str, Any]],
        scores: list[float],
        embeddings: list[list[float]],
    ) -> str:
        """
        Atomically insert document + all chunks.
        Deletes any existing document with the same title + source_type first
        (handles re-ingestion / updates).
        """
        doc_id = str(uuid.uuid4())
        embed_iter = iter(embeddings)

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                # Remove previous version by title + source_type
                old_ids = await conn.fetch(
                    "SELECT id FROM knowledge_documents WHERE title = $1 AND source_type = $2",
                    title, source_type,
                )
                for row in old_ids:
                    await conn.execute(
                        "DELETE FROM knowledge_documents WHERE id = $1", row["id"]
                    )

                # Insert document
                await conn.execute(
                    """
                    INSERT INTO knowledge_documents
                        (id, title, source_type, language, content_hash, metadata, expires_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    """,
                    doc_id,
                    title,
                    source_type,
                    language,
                    content_hash,
                    json.dumps(metadata),
                    expires_at,
                )

                # Insert parent chunks, then child chunks
                for idx, chunk in enumerate(enriched_chunks):
                    parent_id = str(uuid.uuid4())
                    parent_vec = next(embed_iter, [0.0] * 1536)
                    parent_vec_str = "[" + ",".join(f"{x:.8f}" for x in parent_vec) + "]"
                    quality = scores[idx] if idx < len(scores) else 5.0

                    await conn.execute(
                        """
                        INSERT INTO knowledge_chunks
                            (id, document_id, parent_chunk_id, chunk_index, content,
                             context_prefix, language, quality_score, metadata, embedding)
                        VALUES ($1, $2, NULL, $3, $4, $5, $6, $7, $8, $9::vector)
                        """,
                        parent_id,
                        doc_id,
                        idx,
                        chunk["content"],
                        chunk.get("context_prefix", ""),
                        language,
                        quality,
                        json.dumps(chunk.get("metadata", {})),
                        parent_vec_str,
                    )

                    for c_idx, child in enumerate(chunk.get("children", [])):
                        child_vec = next(embed_iter, parent_vec)
                        child_vec_str = "[" + ",".join(f"{x:.8f}" for x in child_vec) + "]"
                        await conn.execute(
                            """
                            INSERT INTO knowledge_chunks
                                (id, document_id, parent_chunk_id, chunk_index, content,
                                 context_prefix, language, quality_score, metadata, embedding)
                            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::vector)
                            """,
                            str(uuid.uuid4()),
                            doc_id,
                            parent_id,
                            c_idx,
                            child["content"],
                            chunk.get("context_prefix", ""),
                            language,
                            quality * 0.9,  # children score slightly lower
                            json.dumps({}),
                            child_vec_str,
                        )

        return doc_id


# ---------------------------------------------------------------------------
# Child chunk helper (module-level to avoid closure issues)
# ---------------------------------------------------------------------------

def _make_children(parent_text: str) -> list[dict[str, str]]:
    """Split a parent chunk into fine-grained child chunks."""
    sentences = _split_sentences(parent_text)
    children: list[dict[str, str]] = []
    current: list[str] = []
    current_tokens = 0

    def flush_child() -> None:
        if current:
            children.append({"content": " ".join(current)})
            current.clear()

    for sent in sentences:
        t = _token_count(sent)
        if current_tokens + t > _CHILD_TOKEN_LIMIT and current:
            flush_child()
            current_tokens = 0
        current.append(sent)
        current_tokens += t

    flush_child()
    return children
