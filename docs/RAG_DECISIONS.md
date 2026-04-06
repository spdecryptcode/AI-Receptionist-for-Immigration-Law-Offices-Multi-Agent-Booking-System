# RAG Technical Decisions

Design decisions for the RAG system and their rationale. Complements the core [DECISIONS.md](DECISIONS.md).

---

## Vector Store

### pgvector (PostgreSQL) over Pinecone, Weaviate, Qdrant, Chroma

**Decision:** Use the already-provisioned `pgvector` extension in the existing Supabase PostgreSQL instance.

**Rejected:** Dedicated vector databases (Pinecone, Weaviate, Qdrant).

**Reasons:**
- **Zero new infrastructure**: `pgvector` extension is already enabled; `Vector` column type is already in use on `conversation_messages`. No new service to deploy, monitor, or pay for.
- **Transactional consistency**: Document ingestion and chunk updates happen inside a single PostgreSQL transaction alongside application data. External vector stores cannot participate in DB transactions, creating consistency risks.
- **Hybrid search is native**: PostgreSQL's `tsvector` + GIN index enables BM25-style full-text search alongside HNSW vector search in the same query. Combining these in an external vector store requires two separate services and application-level merging.
- **Scale adequacy**: pgvector HNSW handles millions of vectors reliably. An immigration firm's KB will realistically contain 50K–500K chunks — well within pgvector's operational range.
- **Atomic updates**: pgvector rows are just table rows; updating a document's chunks is a standard transaction. External stores require their own upsert/delete APIs with no transactional guarantees.

**Trade-off acknowledged:** At >10M vectors, a dedicated vector store would outperform pgvector on recall and latency. The current scale does not justify this.

---

## Embedding Model

### `text-embedding-3-small` (1536 dims) over `text-embedding-3-large`, `text-embedding-ada-002`

**Decision:** `text-embedding-3-small` at 1536 dimensions.

**Rejected:** `text-embedding-3-large` (3072 dims), `text-embedding-ada-002` (1536 dims).

**Reasons:**
- **Cost vs. quality**: `text-embedding-3-small` is 5× cheaper than `text-embedding-3-large` ($0.02 vs. $0.13 per million tokens). For immigration FAQ content — structured, domain-specific, not requiring multilingual cross-lingual transfer — the quality difference is negligible.
- **No new API key**: Already on OpenAI; uses the existing `AsyncOpenAI` client singleton without any new credentials.
- **MTEB benchmark**: For English retrieval tasks, `text-embedding-3-small` scores 62.3 vs. `text-embedding-3-large` at 64.6 — a 2pt difference not worth 5× cost.
- **Latency**: Smaller model embeds faster — important for query-time embedding during voice calls (350ms budget).
- **Dimensionality reduction option**: `text-embedding-3-small` supports Matryoshka representation at lower dimensions (e.g., 512) if storage becomes a concern later.

**Why not `ada-002`:** Older model, worse performance, same price as `text-embedding-3-small`.

---

## Reranker

### GPT-4o-mini over Cohere Rerank, BGE-Reranker, no reranking

**Decision:** GPT-4o-mini as a zero-shot reranker with a single batched scoring call.

**Rejected:** Cohere Rerank API, BGE cross-encoder, no reranking.

**Reasons:**
- **No new API key**: GPT-4o-mini is already available on the OpenAI account. Cohere Rerank requires a Cohere key and a new billing relationship.
- **Immigration domain knowledge**: GPT-4o-mini understands immigration-specific terminology and context (DACA, I-485, removal proceedings). A generic cross-encoder (BGE, MiniLM) has no domain knowledge and scores based on surface similarity alone.
- **Single batched call**: All top-16 candidates scored in one call. Cohere Rerank also does one call, but at higher cost per call ($0.002 per 1K tokens vs. ~$0.0006 for GPT-4o-mini).
- **Confidence gate integration**: The scoring output (0–10) directly enables the confidence gate threshold check — no extra transformation needed.

**Trade-off acknowledged:** Cohere Rerank is purpose-built and marginally faster (~100ms vs. ~150ms). Not worth a new external dependency.

---

## Chunking Strategy

### Parent-child chunking over flat fixed-size chunks, recursive chunking

**Decision:** Two-level parent-child chunking. Parents ~400 tokens (retrieved/returned to LLM), children ~100 tokens (embedded/matched against queries).

**Rejected:** Flat 400-token chunks with overlap; semantic/recursive chunking (LangChain `RecursiveCharacterTextSplitter`).

**Reasons:**
- **Precision vs. richness tension**: Small chunks (~100 tokens) produce more precise embedding matches — the vector is specific to a narrow concept. But small chunks lack the surrounding context for the LLM to give a useful answer. Parent-child solves both sides simultaneously.
- **No extra library**: The two-level split is implemented with sentence-boundary regex + tiktoken. LangChain's recursive splitter does more or less the same thing but adds a large dependency with no benefit specific to this use case.
- **Flat 400-token chunks**: A 400-token chunk embedding is "diluted" — it represents the average of many concepts in the chunk. A child 100-token embedding is sharper. Retrieval precision measurably improves.

---

## Query Strategy

### HyDE + RAG Fusion over single-query retrieval

**Decision:** On `full` path queries, generate a hypothetical answer (HyDE) plus 3 query variants and run 5 parallel searches merged via Reciprocal Rank Fusion.

**Rejected:** Single-query vector search; query expansion only (no fusion).

**Reasons:**
- **HyDE rationale**: A user asking "can my employer fire me while my H-1B petition is pending?" uses colloquial language that doesn't match how immigration law content is written. A hypothetical answer ("An H-1B holder generally remains authorized to work during a timely filed extension...") uses the same vocabulary as the KB. Embedding the hypothetical answer closes this vocabulary gap.
- **RAG Fusion**: Different phrasings of the same question retrieve different chunks due to embedding space geometry. A single query retrieves only what happens to be near that one vector. Running 5 parallel searches and merging with RRF dramatically increases recall.
- **RRF over score averaging**: Scores from different queries are not on the same scale (each query's softmax is independent). RRF uses rank positions, which are comparable across queries. Standard k=60 smooths out high-rank outliers.
- **Adaptive depth**: On simple queries ("what is a green card?"), the full pipeline is wasteful — 3 extra LLM calls before the search. The complexity classifier routes simple queries to a single search call, halving cost on ~40% of expected web chat queries.

---

## Contextual Retrieval

### Contextual enrichment at ingest over post-retrieval augmentation

**Decision:** Each chunk is enriched with a GPT-4o-mini-generated context prefix at ingest time, before embedding.

**Rejected:** Enriching retrieved chunks at query time; no enrichment.

**Reasons:**
- **Ingest-time cost, not query-time cost**: Enrichment is a one-time cost per chunk, not per query. A KB chunk about "Form I-485 fee waiver eligibility" is enriched once at ingest and cached forever. At query time (during a voice call), there is no LLM call for enrichment — just vector lookup.
- **Retrieval error reduction**: Anthropic's contextual retrieval research shows a ~49% reduction in retrieval failure rate when chunks include 1–2 sentences of document-level context. Without context, "The filing fee must be submitted with form" is ambiguous — it could be any form. With context ("This is an excerpt from the I-485 instructions regarding Application to Register Permanent Residence"), the embedding captures the full meaning.
- **Post-retrieval augmentation**: Enriching after retrieval (expanding each result before sending to LLM) would require an extra LLM call in the hot query path — unacceptable for voice's 350ms budget.

---

## Hybrid Search Weights

### 0.7 vector + 0.3 full-text over pure vector, pure BM25, equal weighting

**Decision:** Linear combination `score = 0.7 × cosine_similarity + 0.3 × ts_rank`.

**Rejected:** Pure vector search (`0.7` → `1.0`), pure BM25, `0.5/0.5` equal split.

**Reasons:**
- **Immigration terminology is exact**: Immigration law uses precise technical terms — `I-485`, `EAD`, `N-400`, `TPS`, `DACA`. A user typing "I-485" expects documents containing exactly "I-485" to rank highly. Full-text search guarantees exact-string recall; vector search can fail on rare/acronym terms if the embedding space doesn't cluster them precisely.
- **Semantic understanding still needed**: "what happens if my Green Card application is rejected?" and "I-485 denial consequences" are semantically equivalent. Vector search catches this; BM25 misses it unless the user's words exactly match the KB.
- **0.7/0.3 empirical split**: Standard starting point from academic literature on hybrid retrieval. Favors semantic over lexical. Can be tuned via `GET /rag/search` test endpoint.

---

## Voice RAG Timeout

### 350ms hard timeout over no timeout, longer timeout

**Decision:** `asyncio.wait_for(..., timeout=0.35)` — if RAG retrieval exceeds 350ms, voice call proceeds without it.

**Rejected:** 500ms or longer timeout; no timeout (block until completion).

**Reasons:**
- **Voice latency budget is non-negotiable**: The full pipeline target is 1.0–1.5s end-to-end (STT ~400ms + LLM first token ~400ms + TTS ~75ms + network). Blocking voice on a 500ms RAG timeout would push the total to 1.5–2.0s — perceptible to callers and damaging to conversational quality.
- **Graceful degradation is safe**: If RAG doesn't retrieve anything, the call still proceeds with the static system prompt + conversation summary + intake data. Sofia can still have a coherent, helpful conversation without retrieved knowledge. An emergency caller detained by ICE should never experience a delayed AI response because a vector query ran slow.
- **Redis cache hits are ~5ms**: The vast majority of RAG calls during an active call will hit the cache (warmed by speculative pre-fetch on phase transition). The 350ms timeout is a safety net for cold-path calls only.

---

## Web Chat Context Query

### Conversation-level query (last 3 turns + message) over message-only query

**Decision:** Retrieve using `compress(last 3 turns) + " " + user_message` as the embedding query.

**Rejected:** Embedding only the current user message.

**Reasons:**
- **Follow-up questions are frequent in web chat**: Voice calls are first contact — every turn is mostly independent. Web chat attracts returning visitors who ask follow-ups: "You mentioned I need Form I-485 — what does that cost?" The word "that" refers to context established 2 turns ago.
- **Not done for voice**: In voice, each turn is rapid (~15 words). The conversation context is already present in the sliding window of 6 turns fed directly to the LLM. Adding compression overhead would approach the 350ms budget.
- **Web chat has no latency constraint**: Web chat users tolerate ~1–2s response time. The extra 20–50ms to build a conversation-level query is invisible.

---

## Citation Injection

### Inline citation instruction over no citation, separate citation post-processing

**Decision:** Append a citation instruction to `message[1]`, relying on the LLM to cite sources inline.

**Rejected:** No citations; post-processing to extract and append citations.

**Reasons:**
- **Audibility for attorneys**: Immigration attorneys need to verify AI-surfaced legal information. A response containing "[FAQ]" or "[CASE_GUIDE]" signals the source category immediately, allowing attorneys to locate and verify the underlying document.
- **Trust signals for web chat users**: Users are more likely to trust and act on responses that cite a source type ("You are generally eligible to work while your application is pending [FAQ]") vs. bare assertions.
- **No post-processing needed**: The LLM reliably follows the brief inline instruction. Post-processing (parsing LLM output to append citations) is fragile and adds latency.
- **Voice citation behavior**: In voice, citation labels are not spoken (ElevenLabs would read "[FAQ]" aloud). Sofia naturally says responses without brackets. The instruction is included but has no negative impact since the AI omits bracketed labels in spoken responses.

---

## Staleness Expiry

### `expires_at` on `knowledge_documents` over no expiry, separate TTL management

**Decision:** Nullable `expires_at TIMESTAMPTZ` column on `knowledge_documents`. All retrieval queries include `expires_at IS NULL OR expires_at > NOW()`.

**Rejected:** No expiry (stale content remains indefinitely); Redis-only TTL; separate expiry table.

**Reasons:**
- **Immigration policy changes frequently**: USCIS processing times, fee schedules, travel ban policies, TPS designations change on a monthly basis. A chunk stating "Processing time for I-485 is approximately 8 months" becomes incorrect and potentially harmful if surfaced 6 months later.
- **Partial index keeps query cost low**: `WHERE expires_at IS NOT NULL` partial index means the filter on the small subset of expiring documents costs near-zero. Documents without `expires_at` (the majority) are unaffected.
- **Admin can set custom expiry**: The `POST /rag/documents` endpoint accepts `expires_at`. A firm admin can set a policy annoucement to expire on a known date (e.g., the date a fee increase takes effect).
- **Auto-expiry defaults**: `policy_news` → 30 days automatically. Other types → NULL (never expires). No manual management needed for routine KB content.

---

## RAG Observability

### Dedicated `rag_query_logs` table over logging only, no observability

**Decision:** Persist per-query metrics to a `rag_query_logs` table via non-blocking `asyncio.create_task()`.

**Rejected:** Log-file-only monitoring; no observability.

**Reasons:**
- **KB gap detection**: The most actionable insight in RAG operations is knowing which queries consistently fail confidence gating (`confidence_gate_triggered = TRUE`). These are queries users are asking that the KB cannot answer. Without structured logging, this signal is buried in log files.
- **`GET /rag/analytics`**: A single endpoint aggregates gated-out queries, cache hit rate, p50/p95 latency, and language breakdown. Attorneys and firm admins can self-serve KB quality insights without querying log infrastructure.
- **Non-blocking**: `asyncio.create_task()` means the log write never adds latency to the retrieval hot path. The task runs after `retrieve()` has already returned its result.
- **Log files are insufficient**: Log files require grep, log aggregation pipelines, or external tooling. A DB table with indexed `created_at` and `confidence_gate_triggered` columns enables instant SQL queries from `psql` or the analytics endpoint.
