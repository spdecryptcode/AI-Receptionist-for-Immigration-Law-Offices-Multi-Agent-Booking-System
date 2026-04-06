# RAG Database Schema

Three new tables added to the existing PostgreSQL 16 schema. Requires `pgvector` and `uuid-ossp` extensions (both already enabled). No new extensions needed.

---

## Design Principles

- **Parent-child chunk model**: parent rows (~400 tokens) hold content returned to the LLM; child rows (~100 tokens) hold the embeddings matched against queries. `parent_chunk_id` self-FK connects them.
- **Quality gating at ingest**: `quality_score` set by GPT-4o-mini at ingest time; chunks < 5.0 are never inserted.
- **Staleness via `expires_at`**: NULL means never expires; `policy_news` documents default to 30 days. Every retrieval query filters `expires_at IS NULL OR expires_at > NOW()`.
- **SHA-256 dedup**: `content_hash` on `knowledge_documents` prevents re-embedding unchanged content. On update, atomic transaction swaps old chunks for new.
- **Full-text + vector hybrid**: `tsvector_col` carries a GIN-indexed tsvector alongside the HNSW-indexed `embedding` — both used in every search query with weighted scoring (`0.7 × cosine + 0.3 × ts_rank`).
- **Observability built-in**: `rag_query_logs` captures per-query metrics (latency, cache hit, confidence gate, top score) for offline KB gap analysis.

---

## Tables (3 new)

### 1. `knowledge_documents`

Master document registry. One row per source document.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | `gen_random_uuid()` |
| `title` | TEXT NOT NULL | Human-readable document title |
| `source_type` | ENUM NOT NULL | See `KnowledgeSourceType` below |
| `language` | ENUM NOT NULL | `en`, `es`, `both` |
| `content_hash` | TEXT UNIQUE NOT NULL | SHA-256 of raw content — dedup key |
| `metadata` | JSONB | Arbitrary extra fields (e.g. `case_type`, `form_number`, `url`) |
| `expires_at` | TIMESTAMPTZ NULL | NULL = never expires. `policy_news` defaults to `NOW() + 30 days` |
| `created_at` | TIMESTAMPTZ | `NOW()` |
| `updated_at` | TIMESTAMPTZ | Updated on re-ingest |

**`KnowledgeSourceType` enum values:**

| Value | Description |
|---|---|
| `faq` | Immigration FAQ content |
| `case_guide` | DACA, TPS, deportation defense, employment visa guides |
| `firm_policy` | Services, pricing, attorneys, intake scripts (`prompts/*.md`) |
| `uscis_form` | I-130, I-485, I-765, N-400 instructions |
| `conversation_transcript` | Past call transcripts (quality-gated, booking/transfer outcomes only) |
| `policy_news` | USCIS processing times, travel bans, policy changes (30-day autexpiry) |

**Indexes:**
```sql
CREATE UNIQUE INDEX idx_knowledge_documents_content_hash
  ON knowledge_documents (content_hash);

CREATE INDEX idx_knowledge_documents_expires_at
  ON knowledge_documents (expires_at)
  WHERE expires_at IS NOT NULL;

CREATE INDEX idx_knowledge_documents_source_type_language
  ON knowledge_documents (source_type, language);
```

---

### 2. `knowledge_chunks`

One row per chunk derived from a document. Both parent and child rows live in this table, distinguished by whether `parent_chunk_id` is NULL.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | `gen_random_uuid()` |
| `document_id` | UUID FK → `knowledge_documents.id` | CASCADE DELETE |
| `chunk_index` | INT NOT NULL | Ordering within document |
| `content` | TEXT NOT NULL | Raw chunk text (returned to LLM) |
| `context_prefix` | TEXT | GPT-4o-mini generated context sentence — prepended before embedding |
| `parent_chunk_id` | UUID FK → `knowledge_chunks.id` NULL | NULL = this is a parent chunk. Non-NULL = child chunk pointing to its parent |
| `embedding` | VECTOR(1536) | `text-embedding-3-small` on `context_prefix + content` |
| `tsvector_col` | TSVECTOR | Full-text search vector — updated via trigger |
| `language` | TEXT NOT NULL | `en` or `es` (inherits from document) |
| `quality_score` | FLOAT NOT NULL | GPT-4o-mini score 0–10. Chunks < 5.0 are not inserted. |
| `metadata` | JSONB | Inherits from parent document; may include page number, section header |

**Indexes:**
```sql
-- HNSW for approximate nearest neighbour search
CREATE INDEX idx_knowledge_chunks_embedding_hnsw
  ON knowledge_chunks
  USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);

-- GIN for full-text search
CREATE INDEX idx_knowledge_chunks_tsvector_gin
  ON knowledge_chunks USING gin (tsvector_col);

-- B-tree indexes for filtering
CREATE INDEX idx_knowledge_chunks_document_id
  ON knowledge_chunks (document_id);

CREATE INDEX idx_knowledge_chunks_parent_chunk_id
  ON knowledge_chunks (parent_chunk_id)
  WHERE parent_chunk_id IS NOT NULL;

CREATE INDEX idx_knowledge_chunks_language_quality
  ON knowledge_chunks (language, quality_score);
```

**Tsvector trigger:**
```sql
CREATE FUNCTION update_chunk_tsvector() RETURNS trigger AS $$
BEGIN
  NEW.tsvector_col := to_tsvector('english', COALESCE(NEW.content, ''));
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_knowledge_chunks_tsvector
  BEFORE INSERT OR UPDATE OF content
  ON knowledge_chunks
  FOR EACH ROW EXECUTE FUNCTION update_chunk_tsvector();
```

**Chunk sizing:**
| Level | Target size | Overlap | Purpose |
|---|---|---|---|
| Parent | ~400 tokens | 100 tokens | Content returned to LLM |
| Child | ~100 tokens | 25 tokens | Embedding matched against query |

---

### 3. `rag_query_logs`

Per-query observability record. Written asynchronously via `asyncio.create_task()` — never in the hot retrieval path.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | `gen_random_uuid()` |
| `query_hash` | TEXT NOT NULL | SHA-256 of raw query text |
| `channel` | TEXT NOT NULL | `voice`, `web_chat` |
| `path` | TEXT NOT NULL | `fast` or `full` (adaptive retrieval depth) |
| `top_score` | FLOAT | Highest reranker score returned (NULL if cache hit) |
| `chunks_returned` | INT NOT NULL | 0 if confidence-gated |
| `confidence_gate_triggered` | BOOLEAN NOT NULL | TRUE = top_score < 5, no RAG injected |
| `cross_language_fallback` | BOOLEAN NOT NULL | TRUE = ES query returned < 2 results, EN fallback used |
| `cache_hit` | BOOLEAN NOT NULL | TRUE = result served from `rag:cache:*` |
| `retrieval_ms` | INT | Total retrieval wall-clock time in milliseconds |
| `language` | TEXT NOT NULL | `en` or `es` |
| `call_sid` | TEXT NULL | Twilio call SID (voice channel only) |
| `session_id` | TEXT NULL | Web chat session ID |
| `created_at` | TIMESTAMPTZ | `NOW()` |

**Indexes:**
```sql
CREATE INDEX idx_rag_query_logs_created_at
  ON rag_query_logs (created_at DESC);

CREATE INDEX idx_rag_query_logs_channel_gate
  ON rag_query_logs (channel, confidence_gate_triggered, created_at DESC);
```

**Analytics queries (used by `GET /rag/analytics`):**
```sql
-- KB content gaps: queries that consistently get gated out
SELECT query_hash, COUNT(*) AS occurrences
FROM rag_query_logs
WHERE confidence_gate_triggered = TRUE
  AND created_at > NOW() - INTERVAL '7 days'
GROUP BY query_hash
ORDER BY occurrences DESC
LIMIT 20;

-- Cache performance
SELECT
  ROUND(100.0 * SUM(cache_hit::int) / COUNT(*), 1) AS cache_hit_pct,
  PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY retrieval_ms) AS p50_ms,
  PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY retrieval_ms) AS p95_ms
FROM rag_query_logs
WHERE created_at > NOW() - INTERVAL '24 hours';

-- Language breakdown
SELECT language, COUNT(*) FROM rag_query_logs
WHERE created_at > NOW() - INTERVAL '7 days'
GROUP BY language;
```

---

## Migration

File: `app/database/migrations/XXXX_add_rag_knowledge_base.py`

Run order:
1. `CREATE TYPE knowledgesourcetype AS ENUM (...)`
2. `CREATE TABLE knowledge_documents (...)`
3. `CREATE TABLE knowledge_chunks (...)`  — depends on 2
4. `ALTER TABLE knowledge_chunks ADD CONSTRAINT fk_parent_chunk ...`  — self-FK added after table exists
5. `CREATE TABLE rag_query_logs (...)`
6. All indexes
7. tsvector trigger + function

```
alembic upgrade head
```

Verify with:
```sql
SELECT tablename FROM pg_tables WHERE schemaname = 'public'
  AND tablename IN ('knowledge_documents', 'knowledge_chunks', 'rag_query_logs');

SELECT indexname FROM pg_indexes
  WHERE tablename = 'knowledge_chunks'
  AND indexname LIKE 'idx_%';
```

---

## Relationship to Existing Tables

| Existing table | Relationship |
|---|---|
| `conversation_messages` | Source for `conversation_transcript` documents. `call_sid` used to query messages for ingestion. |
| `immigration_intakes` | Source for nightly `aggregate_intake_patterns()` → `faq` chunks per `case_type`. |
| `conversations` | `call_outcome` checked before auto-ingesting transcripts (`booking_made` or `transferred_to_staff` only). |
| `rag_query_logs` | References `call_sid` (voice) and `session_id` (web chat) for cross-table observability. |

No foreign keys from new tables into existing tables — ingestion is loosely coupled via application logic, not DB constraints. This avoids cascading issues if conversations are purged under retention policies.
