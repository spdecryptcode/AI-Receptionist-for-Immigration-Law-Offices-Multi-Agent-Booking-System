# Technical Decisions

Decisions made during design and their rationale.

---

## Core Architecture

### DIY pipeline over Retell AI

**Decision:** Build custom FastAPI pipeline (Twilio + Deepgram + OpenAI + ElevenLabs).

**Rejected:** Retell AI managed conversation platform.

**Reasons:**
- **Cost:** Retell at 1,000 min/month = ~$15–25/day ($450–750/month) vs. DIY ~$0.69/call × 1,000 min ≈ ~$200–250/month. ~50% savings at scale.
- **Control:** Custom FSM, per-state token limits, conditional intake branching, immigration-specific urgency routing cannot be done on Retell without platform workarounds.
- **Prompt caching:** Not available on managed platforms — loses the >1024 token static prefix optimization.
- **Data compliance:** Retell stores conversation data on their servers. DIY means all data in your own Supabase, under your own retention policy (critical for HIPAA-adjacent immigration PII).

---

## Telephony

### Twilio Media Streams (WebSocket) over Twilio Programmable Voice (REST)

**Decision:** Twilio Media Streams via WebSocket.

**Why:** Real-time bidirectional audio streaming. No polling, no latency spikes. Necessary for sub-1.5s pipeline.

**Trade-off acknowledged:** More infrastructure complexity than Twilio `<Play>` + `<Gather>`. Worth it for conversational quality.

---

## Speech-to-Text

### Deepgram Flux over Whisper, Azure Speech, Google Speech-to-Text

**Decision:** Deepgram Flux streaming.

**Why:**
- WebSocket streaming with `EndOfTurn` and `EagerEndOfTurn` events — no polling, no segment detection logic to build
- `EagerEndOfTurn` gives ~200ms latency advantage over standard end-of-turn detection
- `language=multi` handles Spanish/English in a single stream — no separate Spanish STT pipeline
- Superior accuracy for immigration terminology and Spanish-accented English
- Competitive pricing vs. OpenAI Whisper API

**Trade-off:** More expensive than Whisper ($0.86/hr vs. ~$0.36/hr). Justified by streaming capability and accuracy.

---

## Language Model

### OpenAI GPT-4o over GPT-4o-mini, Claude, Gemini

**Decision:** GPT-4o for primary AI and all post-call tasks.

**Why:**
- Function calling for urgency classifier with `strict=True` (reliable structured output)
- Streaming completions with predictable latency
- Prompt caching (>1024 token static prefix) available — reduces repeat-call costs by ~50%
- Best balance of speed and quality for immigration intake

**Rejected GPT-4o-mini:** Tested worse at complex immigration topic recognition, conditional intake branching, and Spanish-language quality. The $0.30/call vs. $0.69/call savings not worth quality drop.

---

## Text-to-Speech

### ElevenLabs Flash v2.5 over ElevenLabs Turbo, Google TTS, Amazon Polly

**Decision:** ElevenLabs Flash v2.5 via WebSocket `stream-input` API.

**Why:**
- Flash v2.5: ~75ms first chunk (vs Turbo v2.5 ~200ms) — 125ms latency saving
- WebSocket `stream-input` allows token-by-token streaming — AI output → TTS → caller with minimal buffering
- `output_format=ulaw_8000` eliminates PCM→mulaw conversion step in hot path
- Highest voice naturalism for legal/professional context

**Trade-off:** More expensive than Polly or Google TTS. Voice naturalism matters for trust in legal conversations.

**Note on `optimize_streaming_latency=3` vs `=4`:**
- `=3`: Aggressive latency optimization, minimal quality tradeoff. Recommended.
- `=4`: Maximum latency optimization. Noticeable quality degradation on long sentences. Not used.

---

## Database

### Supabase (PostgreSQL 16) over PlanetScale, MongoDB, Neon

**Decision:** Supabase with PostgreSQL 16.

**Why:**
- `pgvector` extension for semantic search over conversation history (future feature)
- `pgcrypto` for PII field encryption at rest
- PostgreSQL relational model fits structured immigration intake data
- Supabase managed service: automatic backups, point-in-time recovery, row-level security

---

## Cache / State

### Redis 7 over Memcached, DynamoDB, in-memory

**Decision:** Redis for all ephemeral state.

**Why:**
- Conversation state hash survives process restarts/redeploys (critical for graceful shutdown)
- Sorted sets for calendar slot cache with natural expiry
- Atomic operations for circuit breaker state
- Sub-millisecond lookups for call routing hot path
- Redis data structures (Lists, Sorted Sets, Hashes) match each use case precisely

---

## Timezone Handling

### `zoneinfo` (Python stdlib) over `pytz`

**Decision:** Use `zoneinfo.ZoneInfo` exclusively. Never use `pytz`.

**Why:**
- `pytz` uses a non-standard timezone API that produces incorrect results with DST transitions when used with `datetime.replace()` instead of `pytz.localize()`
- `zoneinfo` is the stdlib replacement (Python 3.9+) with correct IANA timezone handling
- No extra dependency; follows Python community recommendation

**Usage pattern:**
```python
from zoneinfo import ZoneInfo
import datetime

office_tz = ZoneInfo(settings.OFFICE_TIMEZONE)
local_time = datetime.datetime.now(tz=office_tz)
utc_time = local_time.astimezone(datetime.timezone.utc)
```

All times stored in PostgreSQL as UTC. Converted to `OFFICE_TIMEZONE` only for display to caller.

---

## Audio Conversion

### Python `audioop` (pinned Python 3.12) over ffmpeg subprocess

**Decision:** Use `audioop.ulaw2lin()` / `audioop.lin2ulaw()` in-process.

**Why:** Spawning an ffmpeg subprocess per audio frame adds 10–50ms overhead per frame and is not viable in the hot path. In-process is ~5ms/chunk.

**Important:** `audioop` is deprecated in Python 3.11 and removed in Python 3.13.

**Mitigation:**
- Pin Python 3.12 in Dockerfile (`FROM python:3.12-slim`)
- OR install `audioop-lts` from PyPI (drop-in replacement for Python 3.13)

---

## Social Media

### Twilio Conversations API over native channel APIs (Meta Graph API, etc.)

**Decision:** Twilio Conversations as unified message layer for WhatsApp, Facebook Messenger, Instagram DM.

**Why:**
- Single webhook receiver for all channels
- Twilio handles channel-specific authentication and message formatting
- One API to send replies across all channels
- Integration consistency with existing Twilio voice infrastructure

---

## Further Considerations

### ElevenLabs WebSocket `stream-input` vs REST (non-streaming)

The REST TTS endpoint has ~600–800ms first audio latency. The WebSocket `stream-input` reduces this to ~75ms by allowing the AI to stream text tokens directly into TTS as they're generated. The REST approach is simpler to implement but adds ~600ms to every response turn — unacceptable for conversational quality.

### Deepgram EagerEndOfTurn

`EagerEndOfTurn` is a signal that Deepgram sends ~200ms before it's confident the speaker is done. It allows starting the LLM speculatively. If the caller continues speaking, the speculative call is cancelled. This is aggressive but worthwhile — false positive rate is ~5–8% in testing. Each false positive costs one discarded OpenAI API call (~$0.0005). The latency savings (150–250ms per turn) far outweigh the rare false positive cost.

### OpenAI Realtime API (GPT-4o Audio)

OpenAI announced a Realtime API that handles the full STT + LLM + TTS pipeline natively. Evaluated but not used because:
1. Cannot use Deepgram Flux (locked to OpenAI Whisper STT — worse for Spanish/accented English)
2. Cannot use ElevenLabs voices (locked to OpenAI TTS)
3. Less control over FSM, per-state token limits, and context window management
4. No prompt caching on Realtime API (as of evaluation date)

Revisit if Realtime API adds bring-your-own-STT and bring-your-own-TTS capabilities.
