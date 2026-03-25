# Verification Tests

32 tests organized by category. Run these before production launch.

---

## Category 1: Audio Pipeline (Tests 1–5)

### Test 1: mulaw ↔ linear16 round-trip
**What**: Convert mulaw → linear16 → mulaw; verify audio is recognizable.  
**Pass**: Output mulaw differs from input by less than 2% bit error rate. No pops or clicks.  
**Run**: `pytest tests/test_audio_utils.py::test_roundtrip`

### Test 2: Audio chunking latency
**What**: Send 160-byte mulaw chunks (20ms @ 8kHz) to pipeline; measure time from final chunk to first audio byte returned.  
**Pass**: ≤1500ms end-to-end for the first response turn with all services active.  
**Run**: Integration test with ngrok + test Twilio account.

### Test 3: Native mulaw STT accuracy (optional benchmark)
**What**: Record a clean-speech .wav file. Play it into the pipeline. Compare Deepgram transcript to the known text.  
**Pass**: WER (Word Error Rate) ≤ 5% on standard immigration vocabulary.  
**Run**: Benchmark only (requires Deepgram live key + audio fixture). No automated unit test.
**What**: If `DEEPGRAM_USE_NATIVE_MULAW=true`, compare WER (word error rate) against linear16 baseline.  
**Pass**: WER difference ≤2%. Native mulaw saves 50–100ms/chunk.  
**Note**: Default is linear16 (safer). Only enable native mulaw passthrough if this passes.

### Test 4: Filler audio feedback prevention
**What**: Play filler audio file while Deepgram is muted. Verify no echo/feedback loop.  
**Pass**: No Deepgram transcript generated from filler playback. Deepgram resumes correctly after filler ends.  
**Run**: `pytest tests/test_websocket_handler.py::TestStreamTtsToTwilio tests/test_websocket_handler.py::TestOnSpeechStartBargeIn -v`

### Test 5: Barge-in mid-sentence
**What**: Start TTS playback, then send simultaneous caller audio.  
**Pass**: TTS stops within 200ms of `SpeechStarted` event. No crosstalk unless speech < 500ms after TTS end (crosstalk buffer).  
**Run**: `pytest tests/test_websocket_handler.py::TestStreamTtsToTwilio::test_barge_in_mid_stream_stops_early tests/test_websocket_handler.py::TestOnSpeechStartBargeIn -v`

---

## Category 2: STT & Language (Tests 6–9)

### Test 6: Deepgram EagerEndOfTurn latency savings
**What**: Compare first-response latency with and without `EagerEndOfTurn`.  
**Pass**: EagerEndOfTurn reduces first-response latency by 150–250ms on average.  
**Run**: Benchmark only (requires live Deepgram key + audio fixtures). No automated unit test.

### Test 7: Spanish language detection
**What**: Caller speaks Spanish in first utterance. Verify:  
- System prompt switches to Spanish version
- ElevenLabs voice switches to `ELEVENLABS_VOICE_ID_ES`
- All subsequent responses in Spanish
**Pass**: Language switch confirmed within first 1 response turn.  
**Run**: `pytest tests/test_websocket_handler.py::TestLanguageAutoSwitch -v`

### Test 8: Low-confidence fallback
**What**: Send intentionally garbled/noisy audio with confidence < 0.5.  
**Pass**: AI emits clarification request ("Could you repeat that?") without calling LLM. Does not hallucinate a response.  
**Run**: `pytest tests/test_websocket_handler.py::TestLowConfidenceStreak -v`

### Test 9: Consecutive low-confidence handling
**What**: Send 3 consecutive low-confidence utterances.  
**Pass**: After 3: AI offers "I'd like to text you our intake form to continue, or I can transfer you to a staff member."  
**Run**: `pytest tests/test_websocket_handler.py::TestLowConfidenceStreak::test_third_low_conf_queues_handoff_sentinel -v`

---

## Category 3: Conversation Flow (Tests 10–15)

### Test 10: FSM state transitions
**What**: Simulate a complete call: GREETING → IDENTIFICATION → URGENCY_TRIAGE → INTAKE → CONSULTATION_PITCH → BOOKING → CONFIRMATION → CLOSING.  
**Pass**: All 8 states entered in correct order. Redis conversation hash reflects each state change. No state skipped.  
**Run**: `pytest tests/test_conversation_state.py::TestAdvancePhase::test_full_phase_sequence`

### Test 11: Context window management
**What**: Simulate a 25-turn conversation. Verify context manager behavior at turn 7+.  
**Pass**: Context stays under 2000 tokens. Running summary is generated and contains name, language, urgency, case type. Last 6 turns verbatim.  
**Run**: `pytest tests/test_context_manager.py`

### Test 12: Per-state token limit enforcement
**What**: Trigger each FSM state, verify `max_tokens` is set correctly per state.  
**Pass**:

| State | max_tokens |
|---|---|
| GREETING | 75 |
| IDENTIFICATION | 80 |
| URGENCY_TRIAGE | 100 |
| INTAKE | 150 |
| CONSULTATION_PITCH | 250 |
| BOOKING | 100 |
| CONFIRMATION | 100 |
| CLOSING | 75 |

**Run**: `pytest tests/test_llm_agent.py::TestMaxTokensPerPhase`

### Test 13: Detained caller path
**What**: Caller says "My husband is being held by ICE."  
**Pass**: `is_detained=True` set in intake. `urgency_level=critical`. Immediate Twilio `<Dial>` to emergency attorney line within next response. No calendar booking offered.  
**Run**: Integration test (requires live call simulation: `docker compose up` + Twilio dev phone)

### Test 14: Returning caller recognition
**What**: Known client phone calls again. Their previous intake data is loaded.  
**Pass**: AI greets by name. Intake fields already collected are not re-asked. Conversation starts from appropriate FSM state.  
**Run**: Integration test (requires seeded DB + Redis + live call: `docker compose up` + seed script)

### Test 15: After-hours handling
**What**: Call arrives outside `OFFICE_HOURS_START/END` in `OFFICE_TIMEZONE`.  
**Pass**: Greeting adapts ("Our office is currently closed, I'm the AI assistant..."). Routine cases: next-business-day booking offered. Urgent cases: AI continues intake + on-call attorney (`ONCALL_ATTORNEY_PHONE`) receives SMS.  
**Run**: Integration test (set `OFFICE_HOURS_START=23 OFFICE_HOURS_END=23` in `.env.test`, then `docker compose up` + trigger call)

---

## Category 4: AI Integrations (Tests 16–19)

### Test 16: OpenAI prompt caching
**What**: Make two identical calls with the same static system prompt. Monitor OpenAI API response metadata.  
**Pass**: Second call shows `cached_tokens > 0` in API response. First token latency ≤400ms on cache hit.  
**Run**: Integration test (requires live OpenAI key: `docker compose up` + repeat identical prompt twice, inspect `cached_tokens` in logs)

### Test 17: Urgency classifier async execution
**What**: During a turn where urgency classification is triggered, measure its impact on TTS playback start.  
**Pass**: TTS playback starts at the same time regardless of whether urgency classifier task was also created. Classifier result available within 1s.  
**Run**: `pytest tests/test_urgency_classifier.py -v`

### Test 18: Lead scoring (post-call)
**What**: Simulate a completed call with known intake data. Trigger post-call background tasks.  
**Pass**: `lead_scores` row created within 60s of call end. Score between 0–100. `qualification_status` matches score range. `score_breakdown` JSONB contains all 5 components.  
**Run**: `pytest tests/test_lead_scorer.py -v`

### Test 19: Spanish AI response quality
**What**: Conduct a full Spanish-language simulated call.  
**Pass**: All AI responses in Spanish. Intake question flow covers same fields as English. Consultation pitch delivered in Spanish. Appointment confirmation SMS in Spanish.  
**Run**: Integration test (requires live OpenAI + ElevenLabs + Twilio: `docker compose up` + call with Spanish audio fixture)

---

## Category 5: Integrations (Tests 20–23)

### Test 20: GHL contact sync
**What**: Complete a call with a new caller. Verify GHL contact is created.  
**Pass**: New contact in GHL with correct phone, name, tags, lead score within 60s of call end.  
**Run**: Integration test (requires live GHL sandbox credentials: `GHL_API_KEY=... docker compose up`)

### Test 21: Google Calendar booking
**What**: Caller books a consultation via AI. Verify dual-write.  
**Pass**: Event appears in Google Calendar within 30s. Appointment appears in GHL calendar. `appointments` table row created. `google_calendar_event_id` and `ghl_appointment_id` both populated.  
**Run**: Integration test (requires Google service account JSON + GHL calendar: `GOOGLE_CALENDAR_ID=... docker compose up`)

### Test 22: GHL webhook HMAC validation
**What**: Send POST to `/ghl/webhook` with:  
  a) Valid HMAC signature
  b) Invalid/missing signature  
**Pass**: (a) returns 200 and processes event. (b) returns **403** immediately without processing.  
_(Note: implementation returns HTTP 403 FORBIDDEN — semantically correct for integrity failure vs. 401 which is for authentication.)_  
**Run**: `pytest tests/test_ghl_webhooks.py -v`

### Test 23: CRM caching + invalidation
**What**: 
  a) Call from a known phone: verify Redis `caller:{phone}` is checked first (no GHL API call if cache hit)
  b) Trigger GHL `contact.updated` webhook
  c) Call same phone again  
**Pass**: (a) GHL API not called on cache hit. (b) Redis key deleted. (c) GHL API called again, new cache entry created.  
**Run**: Integration test (requires Redis + GHL sandbox: `docker compose up` + seed Redis with known contact)

---

## Category 6: Resilience (Tests 24–27)

### Test 24: Circuit breaker trip
**What**: Simulate 3 consecutive OpenAI API failures within 60s.  
**Pass**: Circuit breaker trips. Subsequent calls return IVR TwiML instead of AI stream. After 30s (reset probe), circuit attempts to close on next probe success.  
**Run**: Integration test (set `OPENAI_API_KEY=invalid` temporarily: `docker compose up` + trigger 3 calls)

### Test 25: Concurrent call isolation
**What**: Simulate 12 simultaneous inbound calls (`MAX_CONCURRENT_CALLS=10`).  
**Pass**: First 10 calls get full AI. Calls 11–12 get IVR fallback. No call interferes with another's Redis state.  
**Run**: Integration test (`MAX_CONCURRENT_CALLS=10 docker compose up` + `ab -c 12 -n 12 http://localhost:8000/twilio/voice`)

### Test 26: Service failure fallback
**What**: Force ElevenLabs API to fail (bad credentials or timeout) mid-call.  
**Pass**: 1 retry after 200ms. On retry failure: pre-recorded fallback audio is played. Call is offered transfer to human.  
**Run**: Integration test (set `ELEVENLABS_API_KEY=invalid` temporarily + trigger live call)

### Test 27: Graceful shutdown
**What**: Start 3 active calls. Send SIGTERM to FastAPI process.  
**Pass**: No new connections accepted immediately. Active calls continue for up to 30s. All calls receive graceful goodbye and connection close. Process exits cleanly.  
**Run**: Integration test (`docker compose up` + start 3 calls + `kill -SIGTERM $(pgrep uvicorn)`)

---

## Category 7: Compliance (Tests 28–30)

### Test 28: Recording consent enforcement
**What**: Simulate a caller who says "no" when asked about recording consent.  
**Pass**: `recording_consent=False` in `conversations`. No Twilio `<Record>` issued. Call continues normally without recording.  
**Run**: `pytest tests/test_compliance_middleware.py::TestTwimlRecordingConsent -v`  
Full flow: Integration test (requires live call flow)

### Test 29: SMS TCPA consent check
**What**: Attempt to send post-call SMS to a caller with `sms_consent=False`.  
**Pass**: SMS not sent. No Twilio message API call made. Log entry shows "SMS skipped: no consent."  
**Run**: `pytest tests/test_compliance_middleware.py::TestCheckSmsConsent tests/test_compliance_middleware.py::TestSendConfirmationSmsConsentGate -v`

### Test 30: STOP keyword opt-out
**What**: Send "STOP" reply to a post-call SMS.  
**Pass**: Twilio Advanced Opt-Out processes it automatically. No further SMS sent to that number. `clients.sms_consent` updated to FALSE.  
**Run**: Integration test (requires live Twilio number with Advanced Opt-Out enabled: send "STOP" via SMS to the test number)

---

## Category 8: Analytics (Tests 31–32)

### Test 31: Per-call cost tracking
**What**: Complete a 5-minute test call. Check `call_logs` row.  
**Pass**: `cost_deepgram`, `cost_openai`, `cost_elevenlabs`, `cost_twilio`, `cost_total` all populated with non-zero values. `cost_total` matches sum of components within $0.001.  
**Run**: `pytest tests/test_cost_tracker.py -v`  
Full DB check: Integration test (`docker compose up` + complete a test call + `psql -c "SELECT cost_total FROM call_logs ORDER BY created_at DESC LIMIT 1;"`)

### Test 32: Post-call background task completion
**What**: End a test call and wait 120s. Check all post-call tasks.  
**Pass**: All of the following completed and stored:
- AI summary (2–3 sentences in `call_logs.ai_summary`)
- Action items array (`call_logs.ai_action_items`)
- Lead score (`lead_scores` table row)
- Sentiment score (`call_logs.sentiment_score`, –1.0 to 1.0)
- Structured intake data extracted (`immigration_intake` updated)
- Post-call SMS (if consent = TRUE, message sent to caller phone)
- GHL contact updated with tags + lead score

**Run**: Integration test (`docker compose up` + complete test call + `sleep 120` + `psql -c "SELECT ai_summary, sentiment_score FROM call_logs ORDER BY created_at DESC LIMIT 1;"`)
