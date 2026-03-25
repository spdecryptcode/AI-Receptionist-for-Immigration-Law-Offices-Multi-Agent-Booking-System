# Database Schema

PostgreSQL 16 via Supabase. Extensions required: `pgvector`, `pgcrypto`, `uuid-ossp`.

---

## Design Principles

- **Tiered data collection**: Tier 1 (urgency + case type, first 5 min) → Tier 2 (case-specific details, next 5 min) → Tier 3 (optional extras). AI skips lower tiers if caller is impatient.
- **Case-type conditional fields**: `immigration_intake` has separate column groups per case type. Only relevant columns are populated.
- **PII encryption**: `passport_number`, `a_number`, `date_of_birth` encrypted at rest via `pgcrypto`.
- **JSONB escape hatch**: `extra_data` JSONB column on `immigration_intake` for any fields not covered by structured columns.
- **Soft deletes**: `deleted_at` on `clients` for compliance-safe removal.
- **Audit trail**: `created_at` + `updated_at` on every table.

---

## Tables (12)

### 1. `clients`
Core contact record — one row per unique phone number.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `ghl_contact_id` | VARCHAR UNIQUE | GoHighLevel sync |
| `phone` | VARCHAR(20) NOT NULL UNIQUE | Primary lookup key |
| `email` | VARCHAR(255) | |
| `first_name`, `last_name` | VARCHAR(100) | |
| `aliases` | VARCHAR(255) | Other names used |
| `date_of_birth` | DATE | Encrypted via pgcrypto |
| `country_of_origin` | VARCHAR(100) | |
| `preferred_language` | VARCHAR(20) | DEFAULT 'en' |
| `current_address_city`, `current_address_state` | VARCHAR(100) | |
| `client_status` | ENUM | `new_lead`, `intake_scheduled`, `intake_complete`, `active_client`, `closed`, `do_not_contact` |
| `referral_source` | VARCHAR(100) | How they found the firm |
| `total_calls` | INT | DEFAULT 0 |
| `sms_consent` | BOOLEAN | DEFAULT FALSE — TCPA verbal consent |
| `last_contact_at` | TIMESTAMP | |
| `created_at`, `updated_at` | TIMESTAMP | |
| `deleted_at` | TIMESTAMP NULL | Soft delete for compliance |

---

### 2. `immigration_intake`
All structured data collected during AI call. Maximum data collection.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `client_id` | UUID FK → clients | |
| `conversation_id` | UUID FK → conversations | Which call collected this |

**Tier 1 — Urgency triage (collected first)**

| Column | Type | Notes |
|---|---|---|
| `is_detained` | BOOLEAN | DEFAULT FALSE |
| `has_court_date` | BOOLEAN | DEFAULT FALSE |
| `court_date` | DATE NULL | |
| `court_location` | VARCHAR(255) NULL | |
| `visa_expiration_date` | DATE NULL | |
| `urgency_level` | ENUM | `critical`, `high`, `medium`, `routine` |
| `urgency_reason` | TEXT | |

**Tier 1 — Case classification**

| Column | Type | Notes |
|---|---|---|
| `case_type` | ENUM | `family_sponsorship`, `employment_visa`, `asylum`, `removal_defense`, `daca`, `tps`, `naturalization`, `other` |
| `current_immigration_status` | VARCHAR(100) | e.g., "H-1B", "undocumented", "LPR" |
| `a_number` | VARCHAR(50) | Encrypted — Alien Registration Number |

**Tier 2 — Family-based** *(populated if `case_type = family_sponsorship`)*

| Column | Type |
|---|---|
| `petitioner_relationship` | ENUM: `spouse_usc`, `spouse_lpr`, `parent_usc`, `child_usc`, `sibling_usc`, `other` |
| `petitioner_status` | ENUM: `us_citizen`, `lpr`, `unknown` |
| `marital_status` | ENUM: `single`, `married`, `divorced`, `widowed`, `separated` |
| `num_dependents` | INT DEFAULT 0 |
| `dependents_in_us` | BOOLEAN |

**Tier 2 — Employment-based** *(populated if `case_type = employment_visa`)*

| Column | Type |
|---|---|
| `employer_name` | VARCHAR(255) |
| `job_title` | VARCHAR(255) |
| `salary_range` | VARCHAR(50) — e.g., "$80k-$100k" |
| `employer_willing_to_sponsor` | BOOLEAN NULL |
| `education_level` | ENUM: `high_school`, `bachelors`, `masters`, `doctorate`, `other` |
| `years_experience` | INT |

**Tier 2 — Asylum** *(populated if `case_type = asylum`)*

| Column | Type | Notes |
|---|---|---|
| `country_of_persecution` | VARCHAR(100) | |
| `arrival_date_us` | DATE | |
| `asylum_filing_deadline` | DATE | 1-year from arrival |
| `has_filed_asylum` | BOOLEAN | DEFAULT FALSE |
| `persecution_type` | VARCHAR(100) | General category only — NOT detailed narrative |

**Tier 2 — Removal/deportation** *(populated if `case_type = removal_defense`)*

| Column | Type | Notes |
|---|---|---|
| `has_nta` | BOOLEAN | DEFAULT FALSE — Notice to Appear |
| `removal_hearing_date` | DATE NULL | |
| `has_bond_hearing` | BOOLEAN | DEFAULT FALSE |
| `bond_hearing_date` | DATE NULL | |
| `prior_deportation` | BOOLEAN | DEFAULT FALSE |

**Tier 2 — General qualifying fields**

| Column | Type | Notes |
|---|---|---|
| `entry_method` | ENUM | `legal_visa`, `border_crossing_no_inspection`, `visa_overstay`, `unknown` |
| `years_in_us` | INT | |
| `has_criminal_record` | BOOLEAN NULL | Yes/no only — details deferred to attorney |
| `has_prior_visa_denial` | BOOLEAN NULL | |
| `has_prior_immigration_case` | BOOLEAN NULL | |
| `us_family_connections` | BOOLEAN | Anyone USC/LPR in family |

**Tier 3 — Nice-to-have**

| Column | Type | Notes |
|---|---|---|
| `passport_number` | VARCHAR(100) | Encrypted |
| `passport_country` | VARCHAR(100) | |
| `preferred_contact_method` | ENUM | `phone`, `email`, `text` |
| `best_callback_time` | VARCHAR(50) | |
| `budget_discussed` | BOOLEAN | DEFAULT FALSE |
| `timeline_expectation` | VARCHAR(100) | |

**Flexible overflow**

| Column | Type | Notes |
|---|---|---|
| `extra_data` | JSONB | DEFAULT '{}' — anything not covered above |
| `intake_completeness_pct` | INT | 0–100 — percentage of fields collected |
| `fields_deferred_to_attorney` | TEXT[] | Sensitive topics flagged for attorney |
| `created_at`, `updated_at` | TIMESTAMP | |

---

### 3. `conversations`
One row per call or chat session.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `client_id` | UUID FK → clients | |
| `channel` | ENUM | `phone`, `sms`, `whatsapp`, `facebook`, `instagram`, `web_chat` |
| `direction` | ENUM | `inbound`, `outbound` |
| `twilio_call_sid` | VARCHAR(255) UNIQUE NULL | |
| `started_at`, `ended_at` | TIMESTAMP | |
| `duration_seconds` | INT | |
| `recording_consent` | BOOLEAN | DEFAULT FALSE |
| `recording_url` | VARCHAR(500) NULL | |
| `ai_model_used` | VARCHAR(50) | "gpt-4o" |
| `stt_service` | VARCHAR(50) | "deepgram-flux" |
| `tts_service` | VARCHAR(50) | "elevenlabs-flash-v2.5" |
| `call_outcome` | ENUM | `booking_made`, `transferred_to_staff`, `callback_requested`, `info_only`, `dropped`, `voicemail` |
| `transferred_to` | VARCHAR(100) NULL | Attorney/paralegal name |
| `ai_confidence_avg` | FLOAT | Average confidence across all turns |
| `created_at` | TIMESTAMP | |

---

### 4. `conversation_messages`
Every turn of dialogue.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `conversation_id` | UUID FK → conversations | |
| `sequence_number` | INT | Ordering within conversation |
| `role` | ENUM | `caller`, `assistant`, `system` |
| `content` | TEXT | Transcript (caller) or generated text (assistant) |
| `audio_duration_ms` | INT NULL | |
| `stt_confidence` | FLOAT NULL | Deepgram confidence for caller turns |
| `ai_confidence` | FLOAT NULL | LLM confidence for assistant turns |
| `tokens_used` | INT NULL | Cost tracking |
| `latency_ms` | INT NULL | End-of-speech to first audio byte |
| `embedding` | VECTOR(1536) NULL | pgvector for semantic search |
| `timestamp` | TIMESTAMP | DEFAULT NOW() |

---

### 5. `appointments`
Booked consultations.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `client_id` | UUID FK → clients | |
| `intake_id` | UUID FK → immigration_intake NULL | |
| `ghl_appointment_id` | VARCHAR(255) UNIQUE NULL | GHL sync |
| `google_calendar_event_id` | VARCHAR(500) UNIQUE NULL | |
| `appointment_type` | ENUM | `initial_consultation`, `follow_up`, `document_review`, `court_prep` |
| `scheduled_at` | TIMESTAMP NOT NULL | Stored as UTC |
| `duration_minutes` | INT | DEFAULT 60 |
| `attorney_name` | VARCHAR(100) | |
| `status` | ENUM | `scheduled`, `confirmed`, `completed`, `cancelled`, `no_show`, `rescheduled` |
| `reminder_24h_sent`, `reminder_1h_sent`, `reminder_15m_sent` | BOOLEAN | DEFAULT FALSE |
| `meeting_notes` | TEXT NULL | |
| `booked_via` | ENUM | `ai_phone`, `ai_social`, `manual`, `website` |
| `created_at`, `updated_at` | TIMESTAMP | |

---

### 6. `call_logs`
Twilio-level call metadata + AI post-call analysis.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `conversation_id` | UUID FK → conversations | |
| `client_id` | UUID FK → clients | |
| `twilio_call_sid` | VARCHAR(255) UNIQUE | |
| `from_number`, `to_number` | VARCHAR(20) | |
| `call_direction` | ENUM | `inbound`, `outbound` |
| `call_status` | ENUM | `completed`, `no_answer`, `busy`, `failed`, `cancelled` |
| `duration_seconds` | INT | |
| `hit_soft_limit`, `hit_hard_limit` | BOOLEAN | Duration limit flags |
| `ivr_path` | VARCHAR(255) NULL | IVR buttons pressed (if fallback used) |
| `was_transferred` | BOOLEAN | DEFAULT FALSE |
| `transfer_destination` | VARCHAR(100) NULL | |
| `ai_summary` | TEXT | GPT-4o post-call summary (2–3 sentences) |
| `ai_action_items` | TEXT[] | Extracted next steps for staff |
| `sentiment_score` | FLOAT NULL | –1.0 (negative) to 1.0 (positive) |
| `caller_frustration_detected` | BOOLEAN | DEFAULT FALSE |
| `fields_collected_count` | INT | Number of intake fields populated |
| `fields_missing` | TEXT[] | Intake fields not collected |
| `cost_deepgram` | DECIMAL(10,6) | Per-call STT cost |
| `cost_openai` | DECIMAL(10,6) | Per-call LLM cost |
| `cost_elevenlabs` | DECIMAL(10,6) | Per-call TTS cost |
| `cost_twilio` | DECIMAL(10,6) | Per-call telephony cost |
| `cost_total` | DECIMAL(10,6) | Sum of above |
| `created_at` | TIMESTAMP | |

---

### 7. `lead_scores`
AI-computed qualification score.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `client_id` | UUID FK → clients | |
| `intake_id` | UUID FK → immigration_intake NULL | |
| `score` | INT | CHECK (0–100) |
| `qualification_status` | ENUM | `hot` (70–100), `warm` (40–69), `cold` (0–39) |
| `score_breakdown` | JSONB | Component scores (see below) |
| `routing_recommendation` | ENUM | `senior_attorney`, `junior_attorney`, `paralegal`, `follow_up_only` |
| `reasoning` | TEXT | AI explanation |
| `calculated_at` | TIMESTAMP | |

**Score breakdown components:**
- `case_viability` (0–3): immediate relative of USC = 3, complex removal = 1
- `urgency` (0–2): detained/court date = 2, expiring visa = 1, routine = 0
- `financial_fit` (0–2): professional income = 2, moderate = 1, unclear = 0
- `engagement` (0–1): completed full intake = 1, partial = 0
- `red_flags` (0 to –3): criminal record = –2, prior fraud = –3, prior denials = –1

---

### 8. `document_checklist`
Track which documents client has. Yes/no only — never store actual documents.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `client_id` | UUID FK → clients | |
| `intake_id` | UUID FK → immigration_intake | |
| `document_type` | ENUM | `passport`, `birth_certificate`, `visa_stamps`, `i94`, `employment_letter`, `tax_returns`, `marriage_certificate`, `divorce_decree`, `police_clearance`, `medical_exam`, `photos`, `other` |
| `has_document` | BOOLEAN NULL | NULL = not asked, TRUE = has it, FALSE = doesn't |
| `notes` | VARCHAR(255) NULL | e.g., "expired passport" |
| `created_at` | TIMESTAMP | |

---

### 9. `urgency_alerts`
Real-time escalation tracking.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `client_id` | UUID FK → clients | |
| `conversation_id` | UUID FK → conversations | |
| `alert_type` | ENUM | `ice_detention`, `court_date_imminent`, `visa_expiring`, `asylum_deadline`, `daca_expiring`, `ice_raid` |
| `alert_level` | ENUM | `critical`, `high` |
| `details` | TEXT | |
| `deadline_date` | DATE NULL | |
| `action_taken` | ENUM | `transferred_to_attorney`, `expedited_booking`, `sms_sent_to_staff`, `pending` |
| `resolved` | BOOLEAN | DEFAULT FALSE |
| `resolved_by` | VARCHAR(100) NULL | |
| `created_at`, `resolved_at` | TIMESTAMP | |

---

### 10. `referral_tracking`
Marketing ROI — how leads find the firm.

| Column | Type |
|---|---|
| `id` | UUID PK |
| `client_id` | UUID FK → clients |
| `source` | ENUM: `google_ads`, `facebook_ads`, `instagram`, `referral_client`, `referral_attorney`, `website`, `walk_in`, `community_org`, `other` |
| `source_detail` | VARCHAR(255) NULL |
| `first_contact_channel` | ENUM: `phone`, `sms`, `whatsapp`, `facebook`, `instagram`, `web_chat` |
| `converted_to_booking` | BOOLEAN DEFAULT FALSE |
| `converted_to_client` | BOOLEAN DEFAULT FALSE |
| `created_at` | TIMESTAMP |

---

### 11. `voicemails`
Recorded voicemails from failed transfers or caller requests.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `call_sid` | VARCHAR(255) NOT NULL | |
| `client_id` | UUID FK → clients NULL | May be unknown caller |
| `conversation_id` | UUID FK → conversations NULL | |
| `caller_phone` | VARCHAR(20) NOT NULL | |
| `recording_url` | VARCHAR(500) NOT NULL | Twilio recording URL |
| `duration_seconds` | INT | |
| `transcript` | TEXT NULL | Async Deepgram transcription |
| `ai_summary` | TEXT NULL | GPT-4o 1–2 sentence summary |
| `urgency` | ENUM | `critical`, `high`, `medium`, `routine` DEFAULT `routine` |
| `follow_up_status` | ENUM | `pending`, `assigned`, `completed`, `no_action` DEFAULT `pending` |
| `assigned_to` | VARCHAR(100) NULL | Attorney/paralegal name |
| `ghl_task_id` | VARCHAR(255) NULL | Linked GHL follow-up task |
| `created_at` | TIMESTAMP | |

---

### 12. `callback_queue`
Scheduled outbound callback requests.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `client_id` | UUID FK → clients NULL | |
| `caller_phone` | VARCHAR(20) NOT NULL | |
| `previous_conversation_id` | UUID FK → conversations NULL | Context-aware callbacks |
| `preferred_callback_time` | TIMESTAMP NULL | Caller's requested time |
| `reason` | VARCHAR(255) NULL | e.g., "transfer failed", "after hours" |
| `status` | ENUM | `pending`, `in_progress`, `completed`, `failed`, `cancelled` DEFAULT `pending` |
| `attempts` | INT | DEFAULT 0 |
| `max_attempts` | INT | DEFAULT 3 |
| `last_attempt_at` | TIMESTAMP NULL | |
| `completed_at` | TIMESTAMP NULL | |
| `outbound_call_sid` | VARCHAR(255) NULL | Twilio SID of callback call |
| `created_at` | TIMESTAMP | |

---

## Key Indexes

```sql
-- Inbound call lookup (hot path — must be fast)
CREATE UNIQUE INDEX idx_clients_phone ON clients(phone);

-- CRM sync
CREATE UNIQUE INDEX idx_clients_ghl ON clients(ghl_contact_id);

-- Intake queries
CREATE INDEX idx_intake_client ON immigration_intake(client_id);
CREATE INDEX idx_intake_urgency ON immigration_intake(urgency_level)
  WHERE urgency_level IN ('critical','high');

-- Conversation lookups
CREATE INDEX idx_conversations_client ON conversations(client_id);
CREATE UNIQUE INDEX idx_conversations_call_sid ON conversations(twilio_call_sid);

-- Appointment scheduling
CREATE INDEX idx_appointments_scheduled ON appointments(scheduled_at)
  WHERE status = 'scheduled';
CREATE INDEX idx_appointments_client ON appointments(client_id);

-- Message retrieval + semantic search
CREATE INDEX idx_messages_conversation ON conversation_messages(conversation_id, sequence_number);
CREATE INDEX idx_messages_embedding ON conversation_messages
  USING ivfflat(embedding vector_cosine_ops);

-- Alert monitoring
CREATE INDEX idx_alerts_unresolved ON urgency_alerts(alert_level)
  WHERE resolved = FALSE;

-- Lead management
CREATE INDEX idx_lead_scores_status ON lead_scores(qualification_status);

-- Voicemail triage
CREATE INDEX idx_voicemails_pending ON voicemails(follow_up_status)
  WHERE follow_up_status = 'pending';
CREATE INDEX idx_voicemails_caller ON voicemails(caller_phone);

-- Callback queue processing
CREATE INDEX idx_callback_pending ON callback_queue(status, preferred_callback_time)
  WHERE status = 'pending';
```

---

## Data Retention Policy (automated job)

| Data | Retention | Action |
|---|---|---|
| Full transcripts (`conversation_messages.content`) | 30 days | Redact content, keep metadata |
| Call recordings (`conversations.recording_url`) | 30 days | Delete from Twilio, null URL |
| PII fields (passport, DOB, A-number) | 90 days | Purge after `clients.deleted_at` + `DATA_RETENTION_DAYS` |
| Lead scores & analytics | Indefinite | No PII involved |
| Urgency alerts | Indefinite | Audit trail |

---

## What is NOT Collected (deferred to attorney)

The AI explicitly avoids collecting these — flags for attorney review instead:
- Detailed criminal history narrative (yes/no flag only)
- Full asylum persecution story (general category only)
- Abuse/trauma details for VAWA/U-visa/T-visa cases
- Specific visa interview rejection Q&A
- Any admission of immigration fraud
- Government portal passwords or credentials
- Actual document files (checklist of what they have only)

---

## AI Intake Call Data Collection Timeline

| Time | Tier | Data Collected |
|---|---|---|
| 0–2 min | 1 | Greeting, recording consent, SMS consent, name, phone, urgency triage |
| 2–5 min | 1 | Case type, current immigration status, A-number (if known) |
| 5–8 min | 2 | Case-type specific: family/employment/asylum/removal fields |
| 8–10 min | 2 | Entry method, criminal record (y/n), family connections |
| 10–12 min | 3 | Document checklist (y/n), preferred contact, callback time |
| 12–14 min | — | Consultation pitch, booking, confirmation |
| Post-call | — | AI summary, lead score, action items, sentiment score |
