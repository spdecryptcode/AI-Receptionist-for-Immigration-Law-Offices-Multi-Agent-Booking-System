# RAG Architecture

Retrieval-Augmented Generation layer added on top of the existing GPT-4o + pgvector stack. Covers two surfaces: **web chat widget** (`/chat`) and **voice call enhancement** (Sofia's context window). No new API keys or external vector databases required.

---

## System Overview

```
User message (web chat or voice turn)
         │
         ▼
 classify_query_complexity()
         │
    ┌────┴────────────┐
    │                 │
  fast path         full path
  (simple query)    (complex/case-specific query)
    │                 │
    │        ┌────────┴──────────────────────────────┐
    │        │                                       │
    │  expand_with_hyde()         generate_query_variants()
    │  + self_query_filters()     (3 rephrased variants)
    │        │                                       │
    │        └────────┬──────────────────────────────┘
    │                 │
    ▼                 ▼
  hybrid_search()  parallel hybrid_search() ×5
  (single query)   (original + HyDE + 3 variants)
         │
         ▼
  Reciprocal Rank Fusion  (full path only)
         │
         ▼
  cross_language_fallback()  (Spanish KB < 2 results → retry EN)
         │
         ▼
  rerank_with_gpt4o_mini()  (top-16 → scored 0–10)
         │
         ▼
  confidence_gate()  (top score < 5 → return empty)
         │
         ▼
  mmr_diversity_pass()  (cosine distance > 0.15 between selected)
         │
         ▼
  parent_chunk_resolution()  (child match → return parent content)
         │
         ▼
  build_rag_context()
    - lost-in-middle reorder (rank-1 first, rank-2 last)
    - token budget enforcement
    - citation label injection
         │
         ▼
  inject into message[1]  →  GPT-4o
```

---

## Optimization Stack

15 optimizations applied in sequence. Each is independently removable without breaking the others.

| # | Optimization | Where | Cost | Impact |
|---|---|---|---|---|
| 1 | **Adaptive Retrieval Depth** | `retrieval.py` | None (string logic) | Halves LLM calls on simple queries |
| 2 | **HyDE** | `retrieval.py` | 1 GPT-4o-mini call | ~30% recall improvement |
| 3 | **RAG Fusion (Multi-Query)** | `retrieval.py` | 1 GPT-4o-mini call | Reduces single-query retrieval blind spots |
| 4 | **Self-Querying Metadata Filters** | `retrieval.py` | Batched with #2/#3 | Narrows vector search scope |
| 5 | **Hybrid Search** | `retrieval.py` | asyncpg SQL | 15–20% over pure vector |
| 6 | **Cross-Language Fallback** | `retrieval.py` | 1 extra SQL query | Handles ES bootstrapping gap |
| 7 | **Contextual Retrieval** | `ingestion.py` | GPT-4o-mini at ingest | ~49% retrieval error reduction |
| 8 | **Parent-Child Chunking** | `ingestion.py` + `retrieval.py` | None at query time | Precise match, rich context |
| 9 | **Reranking** | `retrieval.py` | 1 GPT-4o-mini call | Reorders by true relevance |
| 10 | **Confidence Gating** | `retrieval.py` | None | Prevents noise injection |
| 11 | **MMR Diversity** | `retrieval.py` | None (math) | Prevents 5 near-identical chunks |
| 12 | **Lost-in-the-Middle Mitigation** | `context_builder.py` | None | Ensures most relevant chunks are attended |
| 13 | **Citation Injection** | `context_builder.py` | None | Auditability + user trust |
| 14 | **Speculative Pre-Fetch** | `context_manager.py` | Background task | Voice RAG latency → near-zero |
| 15 | **Staleness Expiry** | DB + SQL filters | None | Policy news auto-expires |

---

## Component Descriptions

### `app/rag/ingestion.py` — `DocumentIngester`

Responsible for all content entering the knowledge base.

**Parent-child chunking strategy:**
- Documents split into parent chunks (~400 tokens, sentence-boundary aware)
- Each parent further subdivided into child chunks (~100 tokens)
- Child embeddings stored with `parent_chunk_id` FK to parent row
- At query time: child embeddings are matched (small = precise), parent content is returned to LLM (large = rich)

**Contextual Retrieval enrichment:**
- Before embedding, each child chunk is enriched via GPT-4o-mini:
  `"Given document '{title}', write 1-2 sentences contextualizing this excerpt: {chunk}"`
- The generated context prefix is prepended to the chunk text before embedding, and stored in `context_prefix`
- Reduces retrieval failure rate by ~49% (Anthropic research)

**Quality scoring:**
- GPT-4o-mini scores each chunk 0–10 for informativeness at ingest time
- Chunks scoring < 5 are dropped before DB insert
- Prevents low-quality transcript fragments from polluting retrieval

**Atomic document versioning:**
- On re-ingest of a changed document (hash mismatch):
  `INSERT new chunks → verify count → DELETE old chunks → COMMIT`
- Single transaction — no retrieval gap during document updates

**Embedding cache:**
- Before calling OpenAI embeddings, checks Redis `emb:cache:{sha256(text)}` (TTL 7 days)
- Saves 50–80ms + $0.00002 per cache hit on repeated/near-identical text

**Auto-ingestion sources:**
| Trigger | Source | `source_type` |
|---|---|---|
| Startup lifespan | `prompts/*.md` files (hash-checked) | `firm_policy` |
| Post-call (outcome = booking/transfer) | `conversation_messages` for that `call_sid` | `conversation_transcript` |
| Nightly background task | `immigration_intakes` aggregated by `case_type` | `faq` |
| Admin API | Manual upload | any |

---

### `app/rag/retrieval.py` — `RAGRetriever`

**Adaptive Retrieval Depth — fast vs. full path:**

```
fast path  (word count < 8 AND no case keywords):
  → single hybrid_search() call
  → skip HyDE, skip RAG Fusion, skip self-query filters
  → proceed directly to reranking

full path (everything else):
  → HyDE + self-query filters + 3 query variants in one asyncio.gather()
  → 5 parallel hybrid_search() calls
  → Reciprocal Rank Fusion merge
  → proceed to reranking
```

Case keywords that trigger `full` path: `visa, court, ICE, detained, DACA, deportation, I-485, I-130, I-765, N-400, work permit, asylum, green card, renewal, denied, petition, removal, EAD, travel document, sponsor`

**Hybrid search SQL:**
```sql
SELECT
  kc.id,
  kc.content,
  kc.parent_chunk_id,
  kc.embedding,
  kd.title,
  kd.source_type,
  1 - (kc.embedding <=> $1::vector) AS cos_sim,
  ts_rank(kc.tsvector_col, plainto_tsquery($2)) AS text_rank
FROM knowledge_chunks kc
JOIN knowledge_documents kd ON kd.id = kc.document_id
WHERE kc.language IN ($3, 'both')
  AND kc.quality_score >= 5
  AND (kd.expires_at IS NULL OR kd.expires_at > NOW())
  AND ($4::text IS NULL OR kd.metadata->>'case_type' = $4)
ORDER BY (0.7 * cos_sim + 0.3 * text_rank) DESC
LIMIT 20
```

Raw asyncpg — no SQLAlchemy ORM overhead on vector ops.

**Reciprocal Rank Fusion:**

$$\text{score}(d) = \sum_{i=1}^{n} \frac{1}{k + \text{rank}_i(d)}, \quad k = 60$$

Top-16 deduplicated results (one per `document_id`) passed to reranker.

**Cross-Language Fallback:**
- Triggered when `language == 'es'` and `len(results) < 2` after hybrid search
- Retries with `language IN ('en', 'both')`
- LLM responds in Spanish regardless (language is a system prompt property, not a retrieval property)

**Reranking:**
- Single GPT-4o-mini call with all top-16 chunks numbered
- Prompt: `"Rate each chunk's relevance to the query on a scale of 0–10. Output only a JSON array of scores in the same order."`
- Confidence gate: if `max(scores) < 5` → return `[]` (no RAG injection)

**MMR Diversity:**

After scoring, MMR selects the final top-5:
1. Start with highest-scored chunk
2. Each next selection = chunk with highest score AND cosine distance > 0.15 to all already-selected chunks
3. Pure numpy math on embedding vectors already in memory — no LLM call

**RAG Observability:**
- After every `retrieve()` call: `asyncio.create_task(log_rag_query(...))`
- Non-blocking, persists to `rag_query_logs` table
- Fields: `query_hash`, `channel`, `top_score`, `chunks_returned`, `confidence_gate_triggered`, `cache_hit`, `retrieval_ms`, `language`

---

### `app/rag/context_builder.py` — `RAGContextBuilder`

Formats retrieved chunks into a token-budgeted string for injection into `message[1]`.

**Lost-in-the-Middle Mitigation:**

Research shows LLMs attend most strongly to context at the start and end of their input, and least strongly to the middle. After MMR selection, chunks are reordered:

```
Position 1 → rank-1 chunk   (highest relevance — most attended)
Position 2 → rank-3 chunk
Position 3 → rank-4 chunk
Position 4 → rank-5 chunk
Position 5 → rank-2 chunk   (second highest relevance — also attended)
```

**Context format per chunk:**
```
[FAQ] Green Card Eligibility

To be eligible for a green card through marriage...

---
```

**Citation instruction** (appended to `message[1]`):
```
When using the retrieved knowledge above, cite the source type in brackets
at the end of the relevant sentence, e.g. "You may be eligible [FAQ]."
```

**Token budgets:**
| Channel | RAG budget | Summary | Intake | Total dynamic |
|---|---|---|---|---|
| Voice | 1200 tokens | 500 | 300 | ~2000 |
| Web chat | 2500 tokens | 600 | — | ~3100 |

---

### `app/voice/context_manager.py` — Voice Integration

RAG is injected into `build_messages()` at `message[1]` (dynamic system context), never `message[0]` (static system prompt — must stay cache-stable for OpenAI prefix caching).

**When RAG fires:**
- Phase must be ≥ `URGENCY_TRIAGE` (skip GREETING/IDENTIFICATION — not useful)
- At least one user turn must exist in history
- Hard timeout: `asyncio.wait_for(..., timeout=0.35)` — if RAG exceeds 350ms, voice proceeds without it

**Phase-based source_type filtering:**
| Phase | Sources searched |
|---|---|
| URGENCY_TRIAGE | `faq`, `case_guide` |
| INTAKE | all |
| CONSULTATION_PITCH | `firm_policy`, `faq` |
| BOOKING, CONFIRMATION, CLOSING | `firm_policy` |

**Speculative Pre-Fetch:**
- When FSM transitions to a new phase, fires `asyncio.create_task(retriever.prefetch(phase, language, case_type))`
- Runs concurrently with TTS playback of the phase-opener sentence
- Warms the Redis result cache (`rag:cache:*`) before the caller's response is transcribed
- On the next turn, `retrieve()` hits cache → near-zero retrieval latency

---

### `app/chat/` — Web Chat Widget

Dedicated channel for prospective clients on the firm's website.

**Query strategy (conversation-level):**

Unlike voice (where each turn is largely independent), web chat conversations frequently contain follow-up questions ("what about that form you mentioned earlier?"). The retrieval query is constructed as:

```python
query = compress_last_n_turns(history[-3:]) + " " + user_message
```

Where `compress_last_n_turns()` produces a 1–2 sentence summary of the last 3 exchanges.

**Streaming pipeline:**
```
WebSocket message received
  → session validation (Redis)
  → rate limit check (30 msg/min per IP)
  → language detection (from session or auto-detect on first message)
  → conversation-level RAG query construction
  → retrieve() → build_rag_context()
  → assemble: [system_prompt] + [RAG + history context] + [last 8 turns]
  → GPT-4o stream=True
  → stream chunks over WebSocket
  → persist to conversation_messages (channel=web_chat)
  → update Redis session history (FIFO, max 16 turns)
```

---

## Redis Key Schema (RAG additions)

| Key pattern | TTL | Contents |
|---|---|---|
| `emb:cache:{sha256(text)}` | 7 days | JSON array of floats (1536-dim embedding vector) |
| `rag:cache:{sha256(query+lang+phase+types)}` | 5 min | JSON array of chunk dicts |
| `chat_session:{session_id}` | 24 h | `{history, language, created_at, ip}` |
| `chat_rate:{ip}:{window_minute}` | 60 s | Integer counter (max 30) |

---

## Data Flow: Voice Call with RAG

```
Caller speaks
  │
  ▼
Deepgram STT → transcript
  │
  ▼
context_manager.build_messages()
  │
  ├─ message[0]: static system prompt (cache-stable, > 1024 tokens)
  ├─ message[1]: dynamic context
  │    ├─ [Earlier conversation summary]
  │    ├─ [Intake collected so far]
  │    ├─ [Urgency: score / label]
  │    └─ [Retrieved Knowledge]        ← RAG injection (≤ 1200 tokens)
  │         ├─ [FAQ] Green Card Eligibility
  │         │   ...rank-1 parent chunk...
  │         ├─ [CASE_GUIDE] H-1B Transfer Process
  │         │   ...rank-3 parent chunk...
  │         │   ...
  │         └─ [FIRM_POLICY] Consultation Booking
  │             ...rank-2 parent chunk...
  └─ messages[2..N]: last 6 verbatim turns
  │
  ▼
GPT-4o streaming
  │
  ▼
ElevenLabs TTS → Twilio → Caller
```

---

## Data Flow: Web Chat Message

```
Browser WebSocket message
  │
  ▼
session_id validation (Redis)
rate limit check (Redis)
  │
  ▼
conversation-level query = compress(last 3 turns) + user message
  │
  ▼
RAGRetriever.retrieve(query, language, channel="web_chat")
  │
  ├─ classify_query_complexity() → fast | full
  ├─ [full] asyncio.gather(HyDE, 3 variants, self-query filters)
  ├─ parallel hybrid_search() × 1 or 5
  ├─ RRF merge
  ├─ cross-language fallback if needed
  ├─ rerank (GPT-4o-mini)
  ├─ confidence gate
  └─ MMR → parent resolution → top-5 chunks
  │
  ▼
build_rag_context(chunks, token_budget=2500)
  - lost-in-middle reorder
  - citation instruction appended
  │
  ▼
GPT-4o stream=True
  │
  ▼
WebSocket stream chunks → browser
  │
  ▼
persist to DB (channel=web_chat)
update Redis session history
```
