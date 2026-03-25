# Implementation Guide

24 steps across 7 phases. Phases 1–2 must be sequential. Phases 3–6 can overlap once Phase 2 is complete.

---

## Phase 1: Foundation & Voice Pipeline

Steps 1–8 must be completed in order. This is the core real-time audio pipeline.

---

### Step 1: Project scaffolding & configuration

- Initialize FastAPI project with `app/main.py`, `app/config.py` (Pydantic `BaseSettings` loading from `.env`)
- `requirements.txt` with pinned deps:
  ```
  fastapi>=0.104.0
  uvicorn[standard]
  websockets>=12.0
  twilio>=9.0
  deepgram-sdk>=6.0
  openai>=1.0
  elevenlabs>=0.2
  redis>=5.0
  supabase
  sqlalchemy
  alembic
  pydub>=0.25
  scipy>=1.12
  numpy
  httpx>=0.27.0
  ```
- `docker-compose.yml` for local dev (PostgreSQL 16, Redis 7)
- Validate: `uvicorn app.main:app --reload`

---

### Step 2: Audio conversion utilities *(parallel with Step 3)*

Build `app/voice/audio_utils.py`:
- `mulaw_to_linear16(data: bytes, input_rate=8000, output_rate=16000) -> bytes`
  - Decode mulaw via `audioop.ulaw2lin()`, upsample via `scipy.signal.resample`
- `linear16_to_mulaw(data: bytes, input_rate=16000, output_rate=8000) -> bytes`
  - Downsample + `audioop.lin2ulaw()`
  - **Note:** `audioop` deprecated in Python 3.11, removed in Python 3.13. Pin Python 3.12 in Dockerfile, or use `audioop-lts` PyPI package as drop-in replacement.
- `base64_decode_audio(payload: str) -> bytes`
- `base64_encode_audio(data: bytes) -> str`

Unit test with known audio samples.

---

### Step 3: Database schema & migrations *(parallel with Step 2)*

- Create SQLAlchemy models in `app/database/models.py`
- Tables: `clients`, `immigration_intake`, `conversations`, `conversation_messages`, `appointments`, `call_logs`, `lead_scores`, `document_checklist`, `urgency_alerts`, `referral_tracking`, `voicemails`, `callback_queue`
- Enable extensions: `pgvector` (embeddings), `pgcrypto` (PII encryption)
- PII columns (`passport_number`, `a_number`, `date_of_birth`) encrypted at rest
- Run: `alembic revision --autogenerate -m "initial" && alembic upgrade head`

See [DATABASE.md](DATABASE.md) for complete schema.

---

### Step 4: Twilio Media Streams WebSocket endpoint *(depends on Step 1)*

Build `app/voice/websocket_handler.py`:
- FastAPI WebSocket route at `/media-stream`
- Handle Twilio events:
  - `connected` — log connection
  - `start` — extract `streamSid`, `callSid`, media format; initialize call state
  - `media` — decode base64 mulaw payload; forward to Deepgram
  - `stop` — trigger post-call cleanup
- Validate `X-Twilio-Signature` header on every request

Build `app/webhooks/twilio_webhooks.py`:
- `POST /webhooks/twilio/voice` — returns TwiML:
  ```xml
  <Response>
    <Connect>
      <Stream url="wss://your-domain/media-stream"/>
    </Connect>
  </Response>
  ```
- `POST /webhooks/twilio/status` — call status callback logging
- `POST /webhooks/recording-complete` — voicemail recording handler

---

### Step 5: Deepgram Flux streaming STT *(depends on Step 4)*

Build `app/voice/deepgram_stt.py`:

**Audio mode (choose one):**
- **Default:** `encoding=linear16&sample_rate=16000` — convert mulaw→linear16 first (Step 2). Safer accuracy.
- **Optional:** `DEEPGRAM_USE_NATIVE_MULAW=true` → `encoding=mulaw&sample_rate=8000` — skips conversion, saves 50–100ms/chunk. Requires accuracy benchmark first (see [VERIFICATION.md](VERIFICATION.md) Test 3).

**Multilingual:**
- Use `language=multi` parameter for Spanish/English auto-detection
- On first utterance: if `detected_language=es` → switch everything to Spanish (prompt, voice)
- Bilingual greeting: "Hello! / ¡Hola!" as initial greeting before detection

**Events to handle:**
- `Update` — interim (log only, don't act)
- `TurnInfo.EagerEndOfTurn` — early signal (~200ms before final). Start LLM on this, cancel if user keeps talking.
- `TurnInfo.EndOfTurn` — confirmed final transcript

**Low-confidence handling:**
- If `confidence < 0.5`: emit clarification ("Could you repeat that?") directly, skip LLM
- 3 consecutive low-confidence → offer SMS intake or transfer to human

Build `app/voice/language_detector.py`:
- `detect_language(utterance_metadata) -> str`
- Stores detected language in Redis conversation state
- Triggers prompt/voice switch (locked for the call after first detection)

---

### Step 6: OpenAI GPT-4o streaming completions *(depends on Step 5)*

Build `app/voice/openai_llm.py`:

```python
openai_client = openai.AsyncOpenAI(
    http_client=httpx.AsyncClient(
        http2=True,
        limits=httpx.Limits(
            max_connections=20,
            max_keepalive_connections=5,
            keepalive_expiry=60
        )
    )
)
```

**Per-state token limits** (avoids truncation — do NOT use a flat cap):

| FSM State | max_tokens |
|---|---|
| GREETING | 60 |
| IDENTIFICATION | 80 |
| INTAKE_QUESTIONS | 80 |
| URGENCY_CHECK | 100 |
| CONSULTATION_PITCH | 200 |
| BOOKING | 180 |
| CONFIRMATION | 120 |
| GOODBYE | 60 |

**Prompt caching:**
- Static system prompt prefix must be >1024 tokens, identical across all calls
- Dynamic content (caller name, CRM history, conversation turns) goes AFTER the static prefix
- Separate cached prompts for EN (`prompts/system_prompt_en.md`) and ES (`prompts/system_prompt_es.md`)
- Cache hit = ~50% token cost reduction + ~100ms faster

**Streaming:**
- `stream=True`, yield delta chunks as they arrive
- Buffer ~50-token chunks before forwarding to TTS (balances latency vs. naturalness)

Build `app/agent/prompts.py`:
- Load EN/ES system prompts from `prompts/` directory
- Enforce: "Always respond in the same language the caller is speaking"
- Include urgency detection instructions, intake question flow, consultation pitch

---

### Step 7: ElevenLabs Flash streaming TTS *(depends on Step 6)*

Build `app/voice/elevenlabs_tts.py`:
- Use WebSocket `stream-input` API: `wss://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream-input`
- Request `output_format=ulaw_8000` (skips PCM→mulaw conversion). Fallback to `pcm_16000` + conversion.
- Params: `optimize_streaming_latency=3`, `model_id=eleven_flash_v2_5`
- Voice selection: `ELEVENLABS_VOICE_ID_EN` (default) or `ELEVENLABS_VOICE_ID_ES` based on detected language

---

### Step 8: End-to-end pipeline integration *(depends on Steps 4–7)*

Wire the full pipeline in `websocket_handler.py`:

1. Twilio `media` → decode mulaw → forward to Deepgram stream
2. Deepgram `EagerEndOfTurn` → trigger LLM (speculative start)
3. Deepgram `EndOfTurn` → confirm or cancel speculative LLM call
4. Check low-confidence threshold. If OK → `language_detector` → GPT-4o streaming
5. GPT-4o text chunks → buffer to 50-token segments → ElevenLabs WebSocket
6. ElevenLabs audio → 20ms frames → base64 encode → Twilio `media` event
7. Send Twilio `mark` event after each chunk (playback tracking)

**Barge-in:** If Deepgram `SpeechStarted` fires while TTS playing → cancel TTS, process new speech.

**Crosstalk buffer:** Ignore `SpeechStarted` if it fires within 500ms of TTS ending. Only cancel on sustained speech (>500ms overlap).

**Filler audio:**
- Trigger when LLM processing exceeds 1000ms
- Rotate randomly through `assets/fillers/` (4+ files, never same twice in a row)
- Mute Deepgram forwarding during filler playback (prevents feedback loop)
- Resume Deepgram forwarding after filler ends

**Connection pre-warming:**
- At `/webhooks/twilio/voice` time: pre-open Deepgram + ElevenLabs WebSocket connections
- Twilio routing takes 1–2s — use this window
- 10s timeout on pre-warmed connections; re-open gracefully if expired

**Silence timeout:**
- 15s silence → "Are you still there? Take your time."
- 30s silence → polite goodbye → end call

**Call duration limit:**
- 15 min (soft): AI steers toward booking
- 20 min (hard): polite wrap-up → booking or goodbye → `call.update()` to end

**Resilience** (build `app/voice/resilience.py`):
- Wrap OpenAI + ElevenLabs: 1 retry, 200ms exponential backoff
- Retry fails → play pre-recorded fallback audio → transfer to human
- Circuit breaker: 3 failures/60s → trip (30s) → IVR fallback for all new calls
- Redis state: `circuit:{service_name}` hash (failure_count, last_failure_time)
- Concurrent isolation: `asyncio.Semaphore(MAX_CONCURRENT_CALLS=10)`, 5s per-stage timeout

Test with: `ngrok http 3000` → configure Twilio webhook

---

## Phase 2: AI Agent & Conversation Logic

---

### Step 9: Conversation state machine *(depends on Step 8)*

Build `app/voice/conversation_state.py`:
- FSM states: `GREETING → IDENTIFICATION → INTAKE_QUESTIONS → URGENCY_CHECK → CONSULTATION_PITCH → BOOKING → CONFIRMATION → GOODBYE`
- Each state defines: expected data fields, transition conditions, fallback behavior
- Tracked fields: `entry_date`, `visa_type`, `family_status`, `court_involvement`, `country_of_origin`
- Redis hash `conversation:{call_sid}` (24h TTL)

Build `app/voice/context_manager.py`:
- Keep last 6 turns verbatim
- Turns 7+: generate running summary via GPT-4o (incremental update)
- Hard cap: 2000 input tokens total
- Structure: `[static system prompt] + [running summary] + [last 6 turns] + [current input]`
- Summary includes: name, language, collected intake fields, urgency level, key facts

---

### Step 10: Immigration intake flow *(depends on Step 9)*

Build `app/agent/intake_flow.py`:
- Structured question sequence with conditional branching
- `court_involvement=True` → escalate to urgent path
- `detention=True` → immediate Twilio transfer
- Skip already-answered fields for returning callers (load from DB/Redis)

Build `app/agent/urgency_classifier.py`:
- GPT-4o function call: `classify_urgency(transcript, intake_data) -> {level, reason}`
- Levels: `critical`, `high`, `medium`, `low`
- **Run as `asyncio.create_task()` in parallel with TTS playback** — zero hot-path latency
- Critical → immediate transfer; High → expedited booking; Medium/Low → standard flow

---

### Step 11: Lead scoring *(parallel with Step 10)*

Build `app/agent/lead_scorer.py`:
- GPT-4o structured output: `response_format={"type": "json_object"}`
- **Fully post-call** — never during live call
- During call: lightweight heuristic score in Redis for routing only
- Score breakdown (0–100):
  - `case_viability` (0–3)
  - `urgency` (0–2)
  - `financial_fit` (0–2)
  - `engagement` (0–1)
  - `red_flags` (0 to –3)
- Store in `lead_scores` table; cache in Redis (`lead_score:{client_id}`, 30min TTL)

---

## Phase 3: CRM & Scheduling Integration

---

### Step 12: GoHighLevel API client *(parallel with Steps 9–11)*

Build `app/crm/ghl_client.py`:
- `httpx.AsyncClient` with Bearer auth, 100 req/min rate limiting
- Methods: `search_contact_by_phone()`, `create_contact()`, `update_contact()`, `get_available_slots()`, `create_appointment()`

Build `app/crm/contact_manager.py`:
- On inbound call: lookup GHL by phone → load history into AI context
- On call end: create/update contact with intake data, tags, lead score
- Sync to Supabase `clients` table for analytics

---

### Step 13: Calendar & booking service *(depends on Step 12)*

Build `app/scheduling/google_calendar.py`:
- Service account auth from JSON key
- `get_free_busy(calendar_id, date_range)` → busy slots
- `create_event(calendar_id, event_data)` → booking

Build `app/scheduling/slot_cache.py`:
- Redis sorted set `slots:{attorney_id}:{date}` (1h TTL)
- Cache miss: fetch from GHL + Google Calendar, merge, cache
- On booking: invalidate slot, verify still available (double-booking prevention)

Build `app/scheduling/calendar_service.py`:
- `get_available_slots(date, attorney_id=None)`, `book_consultation(client_id, slot, case_type)`
- Dual-write: GHL + Google Calendar
- **Timezone:** All times stored as UTC. Convert to `OFFICE_TIMEZONE` for caller display. Google Calendar events use `timeZone` field. Use `zoneinfo.ZoneInfo` (Python stdlib) — no `pytz`.

---

### Step 14: Reminder sequences + post-call SMS *(depends on Step 13)*

Build `app/scheduling/reminders.py`:
- On booking: trigger GHL automation workflow
- Fallback: schedule Twilio SMS at T–24h, T–1h, T–15min
- Bilingual SMS content based on `preferred_language`

**Post-call SMS (TCPA-compliant):**
- AI obtains verbal consent during call: "May I text you a confirmation?"
- Store `sms_consent` in `clients` table — never SMS without it
- Send within 60s of call end (background task)
- All SMS must include: "Reply STOP to unsubscribe."
- Enable Twilio Advanced Opt-Out (handles STOP/START/HELP automatically)

See [COMPLIANCE.md](COMPLIANCE.md) for full TCPA requirements.

---

## Phase 4: Call Routing & Transfers

---

### Step 15: Intelligent call routing + after-hours + voicemail *(depends on Steps 9, 12)*

Build `app/telephony/call_router.py`:
- **Cached CRM lookup** at TwiML webhook time:
  - Check Redis `caller:{phone}` (24h TTL) first
  - Cache miss → GHL API → cache result
  - Cache invalidated on GHL `contact.updated` webhook
- Routing decisions:
  - Existing client, active case → offer paralegal/attorney transfer
  - VIP/urgent tag → bypass AI, immediate `<Dial>`
  - New lead → `<Connect><Stream>` to AI
- **After-hours check** via `OFFICE_HOURS_START/END/TIMEZONE`:
  - After hours + urgent (detention/ICE) → SMS on-call attorney (`ONCALL_ATTORNEY_PHONE`) + AI does intake
  - After hours + routine → AI does full intake + next-business-day booking
  - Greeting adapts: "Our office is currently closed. I'm the AI assistant..."

Build `app/telephony/call_transfer.py`:
- Cold transfer: `call.update(twiml=<Dial>)`
- Warm transfer: conference bridge (AI briefs attorney first)
- Fallback: no answer after 30s → return to AI → voicemail or callback offer

Build `app/telephony/voicemail.py`:
- Trigger: failed transfer, caller request, or WebSocket drop
- `<Record maxLength="120" transcribe="false" recordingStatusCallback="/webhooks/recording-complete">`
- On webhook: fetch recording → Deepgram async transcription → GPT-4o summary → GHL task → optional attorney SMS

Build `app/telephony/outbound_callback.py`:
- Store callback requests in `callback_queue` table
- Cron job every 15min (office hours only)
- Load previous conversation context for AI-aware follow-up
- Retry up to 3 times; mark completed/failed

---

### Step 16: IVR fallback *(parallel with Step 15)*

Build `app/telephony/twiml_responses.py`:
- If AI WebSocket fails to connect or circuit breaker is open: return `<Gather>` IVR menu
- "Press 1 for new consultation, Press 2 for existing case, Press 0 for operator"
- Never drop a call

---

## Phase 5: Multi-Channel Social Media

---

### Step 17: Twilio Conversations for social channels *(depends on Steps 12–13)*

Build `app/social/webhook_handler.py`:
- `POST /webhooks/social/inbound` — Twilio Conversations (WhatsApp, FB Messenger, IG DM)
- Parse message → same AI agent in text mode (no TTS)
- Respond via Twilio Conversations API

Build `app/social/channel_router.py`:
- Detect channel, apply formatting (WhatsApp supports rich messages)
- Same intake question flow as voice but text-based
- On booking: send calendar link + confirmation

---

### Step 18: GHL social inbox integration *(parallel with Step 17)*

Build `app/webhooks/ghl_webhooks.py`:
- **Validate GHL HMAC signature** on every request using `GHL_WEBHOOK_SECRET`. Reject unsigned → 401.
- `message.received` webhook → route to AI for qualification
- `contact.updated` webhook → invalidate Redis `caller:{phone}` cache
- Update GHL contact with AI responses

---

## Phase 6: Logging, Analytics & Compliance

---

### Step 19: Structured logging *(parallel with Phase 3)*

Build `app/logging_analytics/call_logger.py`:
- `conversations` row: created synchronously at call start (once)
- `conversation_messages` rows: written via `asyncio.create_task()` per turn — never blocks pipeline
  - Buffer in Redis list `msg_buffer:{call_sid}` on DB failure; flush at call end
- **Post-call background task queue** (all `asyncio.create_task()` after WebSocket closes):
  1. AI summary (GPT-4o, single non-streaming call)
  2. Lead scoring (Step 11)
  3. Structured data extraction (Step 19b)
  4. Sentiment analysis (Step 21)
  5. Post-call SMS (Step 14)
  6. GHL contact update + tag sync
- Failed tasks: log + add to `failed_tasks:{call_sid}` Redis list (periodic retry job)

Build `app/logging_analytics/structured_data.py`:
- Extract: entry_date, visa_type, family_members, court_date from transcript
- Write to `immigration_intake` table + `extra_data` JSONB
- Auto-tag leads in GHL with priority/urgency

---

### Step 20: Compliance & data protection *(parallel with Step 19)*

- Recording consent: AI asks at call start, stores `recording_consent` in `conversations`
- No recording if consent declined — AI conversation continues normally
- Data retention job: purge PII after `DATA_RETENTION_DAYS` (90 days default)
- Transcript redaction: full content redacted after 30 days, metadata kept
- Audit log for all data access

See [COMPLIANCE.md](COMPLIANCE.md) for details.

---

### Step 21: Post-call analytics *(depends on Step 19)*

Build `app/logging_analytics/sentiment_scorer.py`:
- GPT-4o analysis: sentiment score (–1.0 to 1.0), frustration detection, satisfaction
- Flag low AI confidence calls for human review
- AI self-audit: check which intake fields were not collected, flag gaps

---

## Phase 7: Deployment & Production Readiness

---

### Step 22: Containerization

- `Dockerfile`: Python 3.12-slim, install `ffmpeg` (pydub), copy app, run uvicorn
- `docker-compose.yml`: app (3000), postgres (5432), redis (6379)
- Health check endpoint: `GET /health` — checks Redis ping, DB connection, returns service status

---

### Step 23: Production deployment

- Deploy region: **`us-east-1` (Virginia)** — closest to Twilio US media servers, Deepgram, OpenAI, ElevenLabs. Saves 30–80ms per round-trip.
- Platform must support long-lived WebSocket connections (verify before committing)
- TLS termination required (Twilio requires `wss://`)
- All secrets via env vars — nothing hardcoded

**Graceful shutdown** (in FastAPI `lifespan`):
- On SIGTERM: set `accepting_new_connections = False`
- New inbound calls → redirect TwiML to backup number or IVR
- Wait for active WebSocket connections to finish (up to 30s)
- After grace period: play "please call back in a moment" → close remaining
- Ensures zero-downtime deploys — no live calls dropped

---

### Step 24: Monitoring & observability

- Structured JSON logging with `call_sid` as correlation ID
- Key metrics: call volume, avg latency/turn, AI confidence, booking conversion rate, transfer rate, circuit breaker trips
- Alerts: WebSocket disconnects, API errors, latency >3s, consecutive low-confidence turns

**Per-call cost tracking** (stored in `call_logs`):
| Column | Calculation |
|---|---|
| `cost_deepgram` | STT seconds × $0.0043/min |
| `cost_openai` | (input tokens × $2.50/1M) + (output tokens × $10/1M) |
| `cost_elevenlabs` | TTS characters × plan rate |
| `cost_twilio` | (minutes × $0.014) + (SMS count × $0.0079) |
| `cost_total` | Sum of above |

Monthly dashboard query:
```sql
SELECT
  COUNT(*) AS calls,
  ROUND(AVG(cost_total)::numeric, 4) AS avg_cost_per_call,
  ROUND(SUM(cost_total)::numeric, 2) AS total_cost
FROM call_logs
WHERE created_at > NOW() - INTERVAL '30 days';
```
