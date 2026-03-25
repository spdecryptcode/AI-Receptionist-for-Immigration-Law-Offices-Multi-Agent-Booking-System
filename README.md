# IVR Immigration — AI Receptionist for Immigration Law Offices

A fully automated AI phone receptionist built with **Python/FastAPI**. Handles inbound calls 24/7 — collects immigration intake data, qualifies leads, books consultations, and transfers urgent cases to attorneys in real time.

---

## What It Does

- **Answers every call** with a natural-sounding AI voice (English + Spanish)
- **Runs a structured intake** — case type, urgency triage, family/employment/asylum details
- **Detects urgent cases** (detention, court dates, deportation) and transfers immediately
- **Books consultations** directly in GoHighLevel + Google Calendar, no staff needed
- **Sends post-call SMS** confirmation with appointment details (TCPA-compliant)
- **Handles after-hours** — full intake + next-business-day booking, urgent SMS to on-call attorney
- **Multi-channel** — same AI agent works on WhatsApp, Facebook Messenger, Instagram DM
- **Zero dropped calls** — IVR fallback if AI pipeline fails; voicemail + callback queue if transfer fails

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python 3.12 + FastAPI |
| STT | Deepgram nova-3 (streaming WebSocket) |
| LLM | OpenAI GPT-4o (streaming completions) |
| TTS | ElevenLabs Flash v2.5 (streaming WebSocket) |
| Telephony | Twilio (Media Streams, SMS, Conversations) |
| CRM | GoHighLevel REST API |
| Calendar | GoHighLevel Calendar + Google Calendar (dual-sync) |
| Database | Supabase / PostgreSQL 16 (pgvector, pgcrypto) |
| Cache | Redis 7 |
| Containers | Docker + docker-compose |

**Target latency:** 1.0–1.5s end-to-end (end of speech → first audio byte)

---

## Architecture

```
Caller → Twilio (mulaw 8kHz) → FastAPI WebSocket Server
                                    ├→ Deepgram nova-3 STT (streaming WebSocket)
                                    ├→ OpenAI GPT-4o (streaming completions)
                                    ├→ ElevenLabs Flash v2.5 TTS (streaming)
                                    ├→ Audio: PCM 16kHz → mulaw 8kHz
                                    └→ Twilio outbound media stream → Caller

CRM Layer:  GoHighLevel API ↔ Supabase/PostgreSQL ↔ Redis cache
Scheduling: GoHighLevel Calendar + Google Calendar (dual-sync)
Channels:   Phone (Twilio), SMS, WhatsApp/FB/IG (Twilio Conversations)
```

---

## Project Structure

```
IVR_Immigration/
├── app/
│   ├── main.py                    # FastAPI entry, CORS, lifespan/graceful shutdown
│   ├── config.py                  # Pydantic BaseSettings from .env
│   ├── dependencies.py            # Shared clients (Redis, Supabase)
│   ├── voice/                     # Real-time voice pipeline
│   │   ├── websocket_handler.py   # Twilio Media Streams orchestrator
│   │   ├── deepgram_stt.py        # Deepgram nova-3 STT client
│   │   ├── openai_llm.py          # GPT-4o streaming completions
│   │   ├── elevenlabs_tts.py      # ElevenLabs Flash TTS client
│   │   ├── audio_utils.py         # mulaw↔linear16 conversion
│   │   ├── conversation_state.py  # Per-call FSM (Redis-backed)
│   │   ├── context_manager.py     # Sliding window context for long calls
│   │   ├── resilience.py          # Retry, circuit breaker, isolation
│   │   └── language_detector.py   # Spanish/English auto-detect
│   ├── agent/                     # AI logic
│   │   ├── prompts.py
│   │   ├── intake_flow.py
│   │   ├── urgency_classifier.py
│   │   └── lead_scorer.py
│   ├── crm/                       # GoHighLevel
│   ├── scheduling/                # Calendar & booking
│   ├── telephony/                 # Call routing, transfer, voicemail, callbacks
│   ├── social/                    # WhatsApp/FB/IG via Twilio Conversations
│   ├── database/                  # SQLAlchemy models, Alembic migrations
│   ├── logging_analytics/         # Call logging, structured data, sentiment
│   └── webhooks/                  # Twilio + GHL inbound webhooks
├── prompts/                       # Externalized system prompts (EN + ES)
├── assets/fillers/                # Pre-recorded filler audio (latency masking)
├── tests/
├── scripts/
├── docs/                          # Full documentation
│   ├── ARCHITECTURE.md
│   ├── IMPLEMENTATION.md
│   ├── DATABASE.md
│   ├── VERIFICATION.md
│   ├── DECISIONS.md
│   └── COMPLIANCE.md
├── .env.example
├── requirements.txt
├── Dockerfile
└── docker-compose.yml
```

---

## Quick Start (Local Dev)

### 1. Clone and configure

```bash
git clone <repo>
cd IVR_Immigration
cp .env.example .env
# Fill in all values in .env
```

### 2. Start services

```bash
docker-compose up -d   # PostgreSQL 16 + Redis 7
```

### 3. Install dependencies

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

### 4. Run migrations

```bash
alembic upgrade head
```

### 5. Start the server

```bash
uvicorn app.main:app --reload --port 3000
```

### 6. Expose via ngrok (for Twilio webhooks)

```bash
ngrok http 3000
# Set TWILIO_WEBHOOK_URL=https://xxxx.ngrok.io in .env
# Configure Twilio voice webhook: POST https://xxxx.ngrok.io/webhooks/twilio/voice
```

---

## Documentation

| File | Contents |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | System design, data flow, component descriptions |
| [docs/IMPLEMENTATION.md](docs/IMPLEMENTATION.md) | 24-step build guide across 7 phases |
| [docs/DATABASE.md](docs/DATABASE.md) | Full schema: 12 tables, indexes, retention policy |
| [docs/VERIFICATION.md](docs/VERIFICATION.md) | 32 test cases |
| [docs/DECISIONS.md](docs/DECISIONS.md) | Technology choices, alternatives, further considerations |
| [docs/COMPLIANCE.md](docs/COMPLIANCE.md) | TCPA, recording consent, PII, data retention |

---

## Environment Variables

See [`.env.example`](.env.example) for a full list with descriptions.

Key variables to configure before running:
- `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER`
- `OPENAI_API_KEY`
- `DEEPGRAM_API_KEY`
- `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID_EN`, `ELEVENLABS_VOICE_ID_ES`
- `GHL_API_KEY`, `GHL_LOCATION_ID`, `GHL_CALENDAR_ID`, `GHL_WEBHOOK_SECRET`
- `GOOGLE_SERVICE_ACCOUNT_KEY`
- `DATABASE_URL`, `REDIS_URL`
- `OFFICE_HOURS_START`, `OFFICE_HOURS_END`, `OFFICE_TIMEZONE`
- `ONCALL_ATTORNEY_PHONE`
