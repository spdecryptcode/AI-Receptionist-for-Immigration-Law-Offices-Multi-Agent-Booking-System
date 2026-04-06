# RAG Implementation Guide

14 steps across 8 phases. Phases 1–3 must be sequential. Phases 4–7 can overlap once Phase 3 is complete.

---

## Phase 1: Database & Infrastructure

Steps 1–3 must be completed in order before any RAG code can run.

---

### Step 1: Alembic migration — RAG tables

Create `app/database/migrations/XXXX_add_rag_knowledge_base.py`.

New tables: `knowledge_documents`, `knowledge_chunks`, `rag_query_logs`.

Key schema points:
- `knowledge_chunks.parent_chunk_id` is a self-referential FK (add with `ALTER TABLE` after table creation to avoid forward-reference issue)
- `knowledge_chunks.tsvector_col` set via BEFORE INSERT/UPDATE trigger — not populated manually
- HNSW index parameters: `m=16, ef_construction=64` (good default; increase `m` to 32 for higher recall at cost of memory)
- `knowledge_documents.expires_at` covered by a partial index (`WHERE expires_at IS NOT NULL`) — keep the index small

See [RAG_DATABASE.md](RAG_DATABASE.md) for complete schema, index DDL, and the tsvector trigger.

```bash
alembic revision -m "add_rag_knowledge_base"
# fill in the upgrade/downgrade functions
alembic upgrade head
```

Verify:
```sql
SELECT indexname FROM pg_indexes WHERE tablename = 'knowledge_chunks';
-- should include: idx_knowledge_chunks_embedding_hnsw
--                 idx_knowledge_chunks_tsvector_gin
```

---

### Step 2: SQLAlchemy ORM models

Add to `app/database/models.py`:

- `KnowledgeSourceType` — Python `Enum` matching the PostgreSQL enum
- `KnowledgeDocument` — ORM class mapped to `knowledge_documents`; include `expires_at: Optional[datetime]`
- `KnowledgeChunk` — ORM class mapped to `knowledge_chunks`; include:
  - `embedding: Vector(1536)` (pgvector already used in `conversation_messages`)
  - `parent_chunk_id: Optional[UUID]` self-FK
  - `quality_score: float`
- `RAGQueryLog` — ORM class mapped to `rag_query_logs`

Do **not** use the ORM for retrieval queries involving `<=>` (vector cosine distance) — use raw asyncpg SQL for those to avoid ORM overhead. ORM is used only for ingestion inserts and admin list endpoints.

---

### Step 3: Add `tiktoken` to requirements

```
# requirements.txt
tiktoken>=0.7.0
```

`tiktoken` is used for token counting in chunking (`cl100k_base` encoding, same as GPT-4o / text-embedding-3-small).

No other new dependencies required. All LLM calls go through the existing `AsyncOpenAI` client singleton.

---

## Phase 2: Ingestion Pipeline

---

### Step 4: `DocumentIngester` class (`app/rag/ingestion.py`)

Build the ingestion pipeline in this order — each function is independently testable:

**4a. `chunk_document(text, title, source_type, language) → list[tuple[str, list[str]]]`**

Returns list of `(parent_text, [child_texts])` tuples.

- Split text at sentence boundaries using regex `r'(?<=[.!?])\s+'`
- Accumulate sentences into parent chunks until tiktoken count ≥ 400
- 100-token overlap: carry last 2–3 sentences of previous parent into next parent
- Subdivide each parent into child chunks (~100 tokens, 25-token overlap) using same logic

**4b. `enrich_chunk(chunk, doc_title) → str` (async)**

GPT-4o-mini call:
```
system: "You generate brief document context prefixes."
user: "Document: {doc_title}\n\nExcerpt:\n{chunk}\n\nWrite 1-2 sentences situating this excerpt within the document."
max_tokens: 60, temperature: 0.0
```

Returns `context_prefix + "\n\n" + chunk`. Store both parts separately in DB.

Run in batches with `asyncio.gather()` + `asyncio.Semaphore(10)`.

**4c. `score_chunk_quality(chunk) → float` (async)**

GPT-4o-mini call (batch multiple chunks per call to reduce cost):
```
user: "Rate each text's informativeness for an immigration law knowledge base, 0-10. Output JSON array of scores only.\n\n{numbered chunks}"
max_tokens: 20
```

**4d. `embed_chunks(texts: list[str]) → list[list[float]]` (async)**

1. For each text, check Redis `emb:cache:{sha256(text)}` → return cached vector if found
2. Batch remaining texts (up to 100 per OpenAI request)
3. Call `openai_client.embeddings.create(model="text-embedding-3-small", input=batch)`
4. Cache each new embedding: `redis.setex(f"emb:cache:{sha256(text)}", 604800, json.dumps(vector))`
5. Return all vectors

**4e. `ingest_document(title, source_type, language, content, metadata, expires_at=None) → UUID` (async)**

Full pipeline:
```
1. sha256(content) → check knowledge_documents.content_hash
2. If unchanged → return existing document_id, skip

3. chunk_document() → parent/child tuples
4. asyncio.gather(enrich_chunk() for each child)
5. asyncio.gather(score_chunk_quality() in batches)
6. Filter out children with quality_score < 5
7. embed_chunks(enriched child texts)

8. Open DB transaction:
   a. INSERT INTO knowledge_documents (new or upsert on content_hash conflict)
   b. INSERT INTO knowledge_chunks (all parent rows, then child rows with parent_chunk_id)
   c. Verify row count matches expectation
   d. DELETE old chunks for document_id if this is an update
   e. COMMIT

9. Return document_id
```

Atomic transaction in step 8 prevents retrieval gap during document updates.

**4f. `ingest_conversation_transcript(call_sid) → UUID | None` (async)**

```
1. Query conversation_messages WHERE call_sid = $1 ORDER BY turn_index
2. Format as: "Turn {n} [User]: {content}\nTurn {n} [Sofia]: {content}\n..."
3. ingest_document(
     title=f"Call Transcript {call_sid}",
     source_type="conversation_transcript",
     language=detected_language,
     content=formatted_dialogue,
     metadata={"call_sid": call_sid, "outcome": outcome}
   )
```

Only called when `call_outcome IN ('booking_made', 'transferred_to_staff')`.

**4g. `aggregate_intake_patterns() → None` (async, nightly)**

```
1. SELECT case_type, array_agg(DISTINCT urgency_reason) AS reasons,
          array_agg(DISTINCT current_immigration_status) AS statuses,
          COUNT(*) AS case_count
   FROM immigration_intakes
   GROUP BY case_type
   HAVING COUNT(*) >= 5

2. For each case_type group, call GPT-4o-mini:
   "Generate 3-5 FAQ-style Q&A pairs commonly asked by clients with case_type={x},
    based on these common urgency reasons: {reasons}. Output as JSON array of {q, a}."

3. For each Q&A pair: ingest_document(..., source_type='faq', metadata={'case_type': x})
```

---

## Phase 3: Retrieval Engine

---

### Step 5: `RAGRetriever` class (`app/rag/retrieval.py`)

Build in this order:

**5a. `classify_query_complexity(query: str) → Literal["fast", "full"]`**

```python
CASE_KEYWORDS = {
    "visa", "court", "ice", "detained", "daca", "deportation",
    "i-485", "i-130", "i-765", "i-131", "n-400", "work permit",
    "asylum", "green card", "renewal", "denied", "petition",
    "removal", "ead", "travel document", "sponsor", "h-1b",
    "h1b", "tps", "daca", "parole", "naturalization"
}
words = query.lower().split()
if len(words) < 8 and not any(kw in query.lower() for kw in CASE_KEYWORDS):
    return "fast"
return "full"
```

**5b. `hybrid_search(query_vector, query_text, language, source_types, case_type_filter, limit=20) → list[ChunkResult]`**

Raw asyncpg query — see [RAG_ARCHITECTURE.md](RAG_ARCHITECTURE.md) for full SQL. Returns list of `ChunkResult` dataclasses with: `id`, `content`, `parent_chunk_id`, `embedding`, `title`, `source_type`, `score`.

**5c. `retrieve(query, language, phase, channel, call_sid=None, session_id=None) → list[ChunkResult]`**

Main entry point. Full logic:

```
1. Check result cache: redis.get(f"rag:cache:{sha256(query+language+phase+types)}")
   → deserialize and return if found

2. classify_query_complexity(query) → path

3. If path == "full":
   - asyncio.gather(
       expand_with_hyde(query, language),         # → hyde_answer
       generate_query_variants(query, language),  # → [v1, v2, v3]
       extract_self_query_filters(query)          # → {case_type, form_type}
     )
   - Embed: query, hyde_answer, v1, v2, v3 (5 vectors)
   - asyncio.gather(hybrid_search() × 5)
   - Reciprocal Rank Fusion → top-16 deduplicated results (one per document_id)
   Else (path == "fast"):
   - Embed query (check emb:cache first)
   - hybrid_search() × 1 → top-16

4. Cross-language fallback:
   if language == "es" and len(results) < 2:
       results = hybrid_search(language="en")
       fallback_used = True

5. rerank_with_gpt4o_mini(query, results[:16]) → scored_results
   - if max(scores) < 5 → log confidence_gate=True, return []

6. MMR diversity pass on scored_results → top-5

7. parent_chunk_resolution(): for each child chunk in top-5,
   fetch parent row content if parent_chunk_id is not None

8. Cache result: redis.setex(cache_key, 300, json.dumps(results))

9. asyncio.create_task(log_rag_query(query_hash, ...))  # non-blocking

10. Return top-5 ChunkResult list
```

**5d. `prefetch(phase, language, case_type=None) → None`**

Generates a phase-seeded query and calls `retrieve()` to warm the cache:
```python
PHASE_SEED_QUERIES = {
    "URGENCY_TRIAGE": "What are emergency immigration situations and when to act?",
    "INTAKE": "What documents and information does a client need for their {case_type} case?",
    "CONSULTATION_PITCH": "What immigration legal services does the firm offer?",
    "BOOKING": "How do I schedule a consultation with an immigration attorney?",
}
query = PHASE_SEED_QUERIES.get(phase, "").format(case_type=case_type or "immigration")
await retrieve(query, language, phase)  # result cached, return value ignored
```

---

### Step 6: `RAGContextBuilder` class (`app/rag/context_builder.py`)

**`build_rag_context(chunks, token_budget, channel) → str`**

```
1. lost-in-middle reorder:
   if len(chunks) >= 2:
       reordered = [chunks[0], *chunks[2:], chunks[1]]
   else:
       reordered = chunks

2. For each chunk in reordered:
   block = f"[{chunk.source_type.upper()}] {chunk.title}\n\n{chunk.content}\n\n---\n"
   if tiktoken_count(assembled + block) > token_budget:
       break
   assembled += block

3. Return:
   "[Retrieved Knowledge]\n\n"
   + assembled
   + "\nWhen using the above, cite the source type in brackets, e.g. 'You may qualify [FAQ].'"
```

Token budgets:
- Voice: 1200
- Web chat: 2500

---

## Phase 4: Voice Integration

---

### Step 7: Inject RAG into `build_messages()` (`app/voice/context_manager.py`)

Modify `build_messages()`:

```python
# Only run RAG for phases after IDENTIFICATION
if state.phase not in (Phase.GREETING, Phase.IDENTIFICATION):
    last_user_msg = next(
        (t["content"] for t in reversed(state.turns) if t["role"] == "user"), None
    )
    if last_user_msg:
        try:
            retriever = get_rag_retriever()
            chunks = await asyncio.wait_for(
                retriever.retrieve(last_user_msg, state.language, state.phase, channel="voice"),
                timeout=0.35
            )
            if chunks:
                rag_context = context_builder.build_rag_context(chunks, token_budget=1200, channel="voice")
                dynamic_context += "\n\n" + rag_context
        except asyncio.TimeoutError:
            logger.warning("RAG retrieval timed out for call %s — proceeding without", state.call_sid)
        except Exception:
            logger.exception("RAG retrieval error — proceeding without")
```

RAG is appended to `message[1]` (dynamic context), after the existing summary + intake blocks. `message[0]` (static system prompt) is never modified.

**Wire in speculative pre-fetch:**

In `app/agent/llm_agent.py` (or wherever FSM phase transitions are triggered), add after each phase advance:
```python
asyncio.create_task(
    get_rag_retriever().prefetch(new_phase, state.language, state.case_type)
)
```

---

## Phase 5: Web Chat

---

### Step 8: Session management (`app/chat/session.py`)

- `create_session(ip: str, user_agent: str) → dict`: generate UUID session_id; store in Redis as hash `chat_session:{session_id}` with TTL 24h. Fields: `history` (JSON list), `language` (`en`), `created_at` (ISO), `ip`.
- `get_session(session_id) → dict | None`: Redis hash get; return None if missing/expired.
- `update_session_history(session_id, role, content)`: FIFO append to `history` list; trim to 16 entries (8 turns).
- `check_rate_limit(ip) → bool`: Redis counter `chat_rate:{ip}:{int(time()/60)}`, INCR + EXPIRE 60s; return False if count > 30.

---

### Step 9: Chat router (`app/chat/router.py`)

Endpoints:

**`GET /chat`** — Serve HTML/JS chat widget inline (same pattern as `app/dashboard/router.py`). Bilingual (EN/ES via `lang` query param). Establishes WebSocket to `/chat/ws/{session_id}`.

**`POST /chat/session`**
```json
Request: {} (empty or optional metadata)
Response: {"session_id": "uuid", "csrf_token": "sha256_based_token"}
```
Creates session, sets `HttpOnly` cookie `chat_sid`. CSRF token returned in body for use in WebSocket URL or header.

**`WebSocket /chat/ws/{session_id}`**
```
1. Validate session_id from path exists in Redis
2. Validate CSRF token from query param or first message
3. Check rate limit for session IP → close with 4029 if exceeded

4. Receive JSON message: {"content": "...", "language": "en|es"}
5. Update session language if provided

6. Build conversation-level RAG query:
   history_summary = compress_last_n_turns(session.history[-3:])
   rag_query = f"{history_summary} {message.content}".strip()

7. chunks = await retriever.retrieve(rag_query, language, phase="web_chat", channel="web_chat", session_id=session_id)
8. rag_context = context_builder.build_rag_context(chunks, token_budget=2500, channel="web_chat")

9. messages = [
       {"role": "system", "content": load_system_prompt(language)},   # message[0] — cache-stable
       {"role": "system", "content": history_block + "\n\n" + rag_context},  # message[1]
       *session.history[-8:]  # last 8 turns verbatim
   ]

10. stream = await openai_client.chat.completions.create(
        model=settings.openai_model, messages=messages, stream=True, max_tokens=400
    )

11. For each chunk in stream:
        await websocket.send_json({"type": "chunk", "content": chunk.choices[0].delta.content or ""})
    await websocket.send_json({"type": "done"})

12. Persist: INSERT INTO conversations + conversation_messages (channel="web_chat")
13. update_session_history(session_id, "user", message.content)
14. update_session_history(session_id, "assistant", full_response)
```

**`GET /chat/history/{session_id}`** — returns Redis `history` list for the session (requires matching cookie).

---

### Step 10: Register routers and initialize singletons (`app/main.py`)

In the FastAPI lifespan `startup` block, after existing singleton initialization:

```python
# Initialize RAG retriever singleton
from app.rag.retrieval import RAGRetriever
from app.rag.ingestion import DocumentIngester
from app.rag.context_builder import RAGContextBuilder

rag_retriever = RAGRetriever(openai_client=get_openai_client(), redis=get_redis())
app.state.rag_retriever = rag_retriever

# Auto-import prompts/*.md files on startup
ingester = DocumentIngester(openai_client=get_openai_client(), redis=get_redis(), db=get_db())
await ingester.sync_prompt_files(Path("prompts"))

# Start nightly pattern aggregation task
asyncio.create_task(run_nightly_aggregation(ingester))
```

In the router registration block:
```python
from app.chat.router import router as chat_router
from app.rag.router import router as rag_router

app.include_router(chat_router, prefix="/chat")
app.include_router(rag_router, prefix="/rag")
```

In `app/dependencies.py` add:
```python
def get_rag_retriever() -> RAGRetriever:
    return app.state.rag_retriever
```

---

## Phase 6: Admin Ingestion API

---

### Step 11: Admin RAG router (`app/rag/router.py`)

All endpoints protected by the existing dashboard session cookie check (same `require_session` dependency used in `app/dashboard/router.py`).

| Method | Path | Description |
|---|---|---|
| `POST` | `/rag/documents` | Ingest document from JSON body `{title, source_type, language, content, metadata, expires_at?}` |
| `POST` | `/rag/documents/upload` | Multipart `.txt` or `.md` file upload; dispatched as background task |
| `GET` | `/rag/documents` | Paginated list with per-document chunk count and quality score distribution |
| `DELETE` | `/rag/documents/{id}` | Hard delete (cascades to chunks atomically) |
| `POST` | `/rag/index-transcripts` | Background task: batch-ingest transcripts in date range `{from_date, to_date}` |
| `GET` | `/rag/search` | Test retrieval: `?q={query}&lang=en&phase=INTAKE` — returns chunks with path, scores, cache status |
| `GET` | `/rag/analytics` | Aggregates `rag_query_logs` for KB gap analysis, cache hit rate, p50/p95 latency |

The `POST /rag/documents/upload` endpoint processes the file in a background task:
```python
background_tasks.add_task(ingester.ingest_document, title, source_type, language, content, metadata)
return {"status": "ingestion_queued", "document_title": title}
```

---

## Phase 7: Data Currency

---

### Step 12: Post-call transcript auto-ingestion (`app/logging_analytics/call_logger.py`)

In the existing post-call background task chain (after lead scoring, sentiment, GHL sync), add:

```python
if call_outcome in (CallOutcome.booking_made, CallOutcome.transferred_to_staff):
    asyncio.create_task(ingester.ingest_conversation_transcript(call_sid))
```

This is the last task in the chain — it does not block any existing post-call operations.

---

### Step 13: Startup prompt file sync (`app/main.py`)

In `ingester.sync_prompt_files(prompts_dir: Path)`:

```python
for md_file in prompts_dir.glob("*.md"):
    content = md_file.read_text()
    content_hash = sha256(content.encode()).hexdigest()
    existing = await db.fetch_one(
        "SELECT id FROM knowledge_documents WHERE content_hash = $1", content_hash
    )
    if not existing:
        await ingest_document(
            title=md_file.stem.replace("_", " ").title(),
            source_type="firm_policy",
            language=detect_language_from_filename(md_file.name),  # _en / _es suffix
            content=content,
            metadata={"source_file": md_file.name}
        )
```

Language detection from filename: `system_prompt_en.md` → `en`, `system_prompt_es.md` → `es`, other → `both`.

---

### Step 14: Nightly intake aggregation (`app/main.py` + `app/rag/ingestion.py`)

```python
async def run_nightly_aggregation(ingester: DocumentIngester):
    while True:
        await asyncio.sleep(86400)  # 24 hours
        try:
            await ingester.aggregate_intake_patterns()
        except Exception:
            logger.exception("Nightly RAG aggregation failed")
```

`aggregate_intake_patterns()` is idempotent — it upserts existing `faq` chunks for each `case_type` using the same SHA-256 content-hash dedup logic as all other ingestion.

---

## File Manifest

### New files

| File | Contents |
|---|---|
| `app/rag/__init__.py` | Empty |
| `app/rag/ingestion.py` | `DocumentIngester` — chunking, enrichment, quality scoring, embedding, ingest, transcript indexer, nightly aggregation |
| `app/rag/retrieval.py` | `RAGRetriever` — adaptive depth, HyDE, RAG Fusion, hybrid search, cross-language fallback, reranking, confidence gate, MMR, parent resolution, caching, observability |
| `app/rag/context_builder.py` | `RAGContextBuilder` — lost-in-middle reorder, token budgeting, citation instruction |
| `app/rag/router.py` | Admin API — document CRUD, transcript batch index, test search, analytics |
| `app/chat/__init__.py` | Empty |
| `app/chat/router.py` | Web chat widget HTML + WebSocket endpoint + history endpoint |
| `app/chat/session.py` | Session creation, get, rate limiting |
| `app/database/migrations/XXXX_add_rag_knowledge_base.py` | Alembic migration — 3 tables, HNSW index, GIN index, tsvector trigger |

### Modified files

| File | Change |
|---|---|
| `app/database/models.py` | Add `KnowledgeDocument`, `KnowledgeChunk`, `RAGQueryLog` ORM classes |
| `app/voice/context_manager.py` | Inject RAG into `build_messages()` for phases ≥ `URGENCY_TRIAGE`; 350ms timeout; graceful degradation |
| `app/agent/llm_agent.py` | Fire `retriever.prefetch()` as background task on each phase transition |
| `app/main.py` | Register `/chat` and `/rag` routers; init RAGRetriever singleton + prompt sync + nightly aggregation in lifespan |
| `app/dependencies.py` | Add `get_rag_retriever()` singleton getter |
| `app/logging_analytics/call_logger.py` | Add `ingest_conversation_transcript()` to post-call task chain |
| `requirements.txt` | Add `tiktoken>=0.7.0` |
