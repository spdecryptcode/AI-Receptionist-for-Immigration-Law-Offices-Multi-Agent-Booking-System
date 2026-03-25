# Architecture

## Overview

The system is a real-time voice pipeline that connects Twilio's media stream to three AI services in sequence — STT → LLM → TTS — all over persistent WebSocket connections to minimize latency.

```
Caller → Twilio (mulaw 8kHz)
           │
           ▼
    FastAPI WebSocket /media-stream
           │
    ┌──────┴───────────────────────────────┐
    │                                      │
    ▼                                      ▼
Deepgram Flux STT                  Pre-warmed connections
(streaming WebSocket)              opened at call routing time
    │
    │ EndOfTurn event + transcript
    ▼
OpenAI GPT-4o
(streaming completions, HTTP/2)
    │
    │ text chunks (~50 token batches)
    ▼
ElevenLabs Flash v2.5 TTS
(streaming WebSocket stream-input)
    │
    │ audio chunks (ulaw 8kHz or PCM→mulaw)
    ▼
Twilio media event (base64 mulaw)
    │
    ▼
Caller hears response
```

**Call routing (pre-AI):**
```
Inbound call → /webhooks/twilio/voice
                    │
                    ├─ Check Redis caller cache → GHL contact lookup
                    ├─ Check office hours
                    │
                    ├─ Existing VIP/urgent client → <Dial> direct transfer
                    ├─ Outside office hours + urgent → SMS on-call attorney + AI intake
                    └─ Default → <Connect><Stream> → AI pipeline
```

---

## Target Latency Budget

| Stage | Target | Notes |
|---|---|---|
| Twilio → server | ~50ms | Network |
| mulaw → linear16 conversion | ~5ms | scipy.signal.resample |
| Deepgram STT (streaming) | ~300–500ms | Flux model, EagerEndOfTurn saves 200ms |
| OpenAI GPT-4o first token | ~300–500ms | HTTP/2 pool, prompt cache hit saves ~100ms |
| ElevenLabs first audio chunk | ~75ms | Flash v2.5 stream-input WebSocket |
| server → Twilio | ~50ms | Network |
| **Total** | **~1.0–1.5s** | With all optimizations active |

---

## Component Descriptions

### `app/voice/websocket_handler.py`
The central orchestrator. One WebSocket connection per active call. Handles the full event loop:
- Receives Twilio `media` events → decodes base64 mulaw
- Forwards audio to Deepgram
- On `EndOfTurn` → sends transcript to LLM pipeline
- Streams TTS audio back to Twilio as `media` events (20ms frames)
- Manages barge-in (stop TTS on `SpeechStarted`), silence timeouts, call duration limits, filler audio

### `app/voice/deepgram_stt.py`
Deepgram Flux streaming STT client. Sends audio frames, receives:
- `Update` — interim transcript (not acted on)
- `TurnInfo.EagerEndOfTurn` — early end-of-turn signal (triggers LLM 200ms early)
- `TurnInfo.EndOfTurn` — confirmed final transcript

Default: `linear16 16kHz` (safer accuracy). Configurable mulaw passthrough via `DEEPGRAM_USE_NATIVE_MULAW`.

Low-confidence (`< 0.5`) results emit a clarification request instead of hitting the LLM.

### `app/voice/openai_llm.py`
GPT-4o streaming completions using `openai.AsyncOpenAI` over persistent HTTP/2. Key behaviors:
- Per-state `max_tokens` limits (60–200 tokens depending on FSM state)
- Static system prompt prefix (>1024 tokens) for OpenAI prompt caching
- Buffers ~50-token chunks before forwarding to TTS for audio naturalness

### `app/voice/elevenlabs_tts.py`
ElevenLabs Flash v2.5 via WebSocket `stream-input` API. Receives text chunks, streams back audio. Requests `ulaw_8000` output format to skip PCM→mulaw conversion. Falls back to `pcm_16000` if unsupported.

### `app/voice/conversation_state.py`
FSM with states: `GREETING → IDENTIFICATION → INTAKE_QUESTIONS → URGENCY_CHECK → CONSULTATION_PITCH → BOOKING → CONFIRMATION → GOODBYE`. State stored in Redis hash `conversation:{call_sid}` (24h TTL).

### `app/voice/context_manager.py`
Sliding window context manager for long calls. Keeps last 6 turns verbatim + a GPT-4o-generated running summary of older turns. Hard cap: 2000 input tokens. Prevents 2x cost/latency increase on long calls.

### `app/voice/resilience.py`
- **Retry**: 1 retry with 200ms exponential backoff on OpenAI/ElevenLabs failures
- **Circuit breaker**: 3 failures in 60s → trip (30s open) → route new calls to IVR → auto-reset probe
- **Concurrent call isolation**: `asyncio.Semaphore(10)` on LLM calls; per-stage 5s timeout; per-call namespaced Redis keys

### `app/voice/language_detector.py`
Extracts `detected_language` from Deepgram metadata on first utterance. If Spanish detected: switches system prompt to `system_prompt_es.md`, switches ElevenLabs voice to `ELEVENLABS_VOICE_ID_ES`, stores `preferred_language=es` in client record. Language is locked for the call after first detection.

### `app/agent/urgency_classifier.py`
GPT-4o function calling: `classify_urgency(transcript, intake_data) → {level, reason}`. Runs as `asyncio.create_task()` in parallel with TTS playback — zero latency impact. Result is checked before the next AI response.

### `app/agent/lead_scorer.py`
Fully post-call. GPT-4o structured output scoring 0–100. During the call, a lightweight heuristic (detention/court date flags) drives routing decisions. Full score is stored in `lead_scores` table after hang-up.

### `app/telephony/call_router.py`
Pre-AI routing decision at TwiML webhook time. Checks Redis caller cache (24h TTL) for GHL contact summary, falling back to live GHL API only on cache miss. Checks office hours. Returns appropriate TwiML.

### `app/telephony/voicemail.py`
Triggered on failed transfer or explicit caller request. Uses Twilio `<Record>` → async Deepgram transcription → GPT-4o summary → GHL follow-up task creation → urgent SMS to attorney.

### `app/telephony/outbound_callback.py`
Manages the `callback_queue` table. Cron job (every 15 min during office hours) checks pending callbacks, initiates outbound Twilio calls, loads previous conversation for context-aware AI follow-up. Retries up to 3 times.

### `app/logging_analytics/call_logger.py`
Writes `conversation_messages` rows via `asyncio.create_task()` — fire-and-forget, never blocks pipeline. Buffers in Redis list as fallback. Post-call background task queue runs: AI summary, lead scoring, structured data extraction, sentiment analysis, post-call SMS, GHL sync. Failed tasks retry via `failed_tasks:{call_sid}` Redis list.

---

## Redis Key Namespace

| Key Pattern | Type | TTL | Purpose |
|---|---|---|---|
| `conversation:{call_sid}` | Hash | 24h | FSM state + collected fields |
| `caller:{phone}` | String | 24h | Cached GHL contact summary |
| `slots:{attorney_id}:{date}` | Sorted Set | 1h | Calendar availability cache |
| `lead_score:{client_id}` | String | 30min | Heuristic lead score during call |
| `circuit:{service_name}` | Hash | — | Circuit breaker state + failure count |
| `msg_buffer:{call_sid}` | List | 1h | DB write buffer (fallback) |
| `failed_tasks:{call_sid}` | List | 7d | Failed post-call task retry queue |

---

## Data Flow: Full Call Lifecycle

```
1. Inbound call arrives at Twilio
2. POST /webhooks/twilio/voice
   └─ Redis caller cache lookup → GHL (cache miss only)
   └─ Office hours check
   └─ Pre-warm Deepgram + ElevenLabs WebSocket connections
   └─ Return TwiML <Connect><Stream>

3. Twilio opens WebSocket to /media-stream
   └─ "connected" event → create conversation row in DB (sync)
   └─ "start" event → bind stream_sid, start silence timer, start duration timer

4. Real-time conversation loop (per turn):
   └─ "media" event → decode base64 mulaw → forward to Deepgram
   └─ Deepgram SpeechStarted → stop TTS if playing (barge-in)
   └─ Deepgram EndOfTurn → check confidence
      ├─ Low confidence (<0.5) → emit "Could you repeat that?" directly
      └─ OK → language detect → send to GPT-4o
   └─ GPT-4o streams text → buffer 50 tokens → ElevenLabs TTS
   └─ ElevenLabs streams audio → 20ms frames → Twilio media events
   └─ Urgency classification runs as background task (parallel)
   └─ conversation_messages row written async (background task)

5. Call ends ("stop" event or 20-min hard limit)
   └─ WebSocket closes
   └─ Background task queue fires:
      ├─ AI summary (GPT-4o)
      ├─ Full lead scoring (GPT-4o)
      ├─ Structured data extraction
      ├─ Sentiment analysis
      ├─ Post-call SMS (if sms_consent=true)
      └─ GHL contact update + tag sync

6. Voicemail path (if transfer fails):
   └─ Twilio <Record> → recording webhook → Deepgram async transcription
   └─ GPT-4o summary → GHL task → urgent SMS if applicable
```

---

## Deployment Notes

- Deploy in `us-east-1` (Virginia) — closest region to Twilio US media servers, Deepgram, OpenAI, ElevenLabs
- Must support long-lived WebSocket connections (verify with chosen platform)
- TLS is required by Twilio for `wss://` media streams
- Graceful shutdown: FastAPI lifespan handles SIGTERM with 30s drain period — no live calls dropped on deploy
