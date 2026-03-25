# Compliance Guide

---

## TCPA (Telephone Consumer Protection Act)

### Scope
TCPA applies to any automated outbound voice calls and SMS messages. This system:
- Receives inbound calls (TCPA generally does not apply to inbound)
- Sends post-call SMS (TCPA **does** apply)
- Initiates callback calls from `callback_queue` (TCPA **does** apply)

### SMS Consent

**Rule:** Never send an SMS unless `clients.sms_consent = TRUE`.

**How consent is collected:**
The AI obtains verbal consent during the call, before sending any SMS:
> "Before we continue, may I text you a summary of our conversation and appointment confirmation? Message and data rates may apply. Reply STOP at any time to unsubscribe."

The AI stores the response:
- "Yes" / affirmative → `sms_consent = TRUE` (written to DB)
- "No" / silence → `sms_consent = FALSE` (default)

**Enforcement:**
```python
# In post_call_sms.py — always check consent before sending
async def send_post_call_sms(client_id: str, message: str):
    client = await get_client(client_id)
    if not client.sms_consent:
        logger.info(f"SMS skipped: no consent for {client_id}")
        return
    await twilio_client.messages.create(...)
```

**Required content on every SMS:**
- "Reply STOP to unsubscribe." (must be present, verbatim or equivalent)

**Twilio Advanced Opt-Out:**
- Enable in Twilio console: `Settings → Messaging → Advanced Opt-Out`
- Handles STOP, STOPALL, UNSUBSCRIBE, CANCEL, END, QUIT automatically
- Handles HELP, INFO automatically
- After STOP: Twilio blocks all outbound SMS to that number from your account

**On STOP received (webhook or Twilio auto-handle):**
```python
# Update DB to reflect revoked consent
await db.execute(
    "UPDATE clients SET sms_consent = FALSE WHERE phone = :phone",
    {"phone": from_number}
)
```

### Callback Calls (Outbound)
- Callbacks only initiated because caller explicitly requested them during an inbound call
- "Caller requested callback" constitutes prior express consent
- Store `reason = 'caller_requested'` in `callback_queue`
- Do not initiate callbacks from cold leads that never called in

---

## Recording Consent

### Federal and State Law
- **Federal (two-party equivalent):** ECPA allows one-party consent at federal level, but 11 US states require all-party consent: California, Connecticut, Delaware, Florida, Illinois, Maryland, Massachusetts, Michigan, Montana, Nevada, New Hampshire, Oregon, Pennsylvania, Washington
- **Safe approach:** Always ask. This system does.

### Consent Flow

AI asks at the start of every call:
> "This call may be recorded for quality and training purposes. Is that okay with you?"

- Consent given → `recording_consent = TRUE`, Twilio `<Record>` is used
- Consent declined → `recording_consent = FALSE`, no recording initiated. **Call continues normally.**

### What Not to Do
- Never record without asking
- Never add `<Record>` to TwiML if `recording_consent = FALSE`
- Never guilt-trip callers who decline consent

---

## Attorney-Client Privilege

### AI is NOT an attorney

The AI must never:
- Give legal advice or legal opinions
- Interpret immigration law or predict case outcomes
- Tell callers what to do in their immigration situation
- Make promises about outcomes or timelines

The AI should always:
- Position itself as a scheduling and intake assistant for the law office
- Refer substantive questions to the attorney: "That's a great question for the attorney to answer during your consultation."
- Collect facts without analyzing them

Include in system prompt:
```
You are an intake assistant for [Firm Name] Immigration Law. 
You are NOT an attorney and cannot provide legal advice. 
When asked for legal opinions, explain that you will make sure that 
question is addressed in their consultation.
```

---

## PII Protection

### Data Encrypted at Rest
The following fields use `pgcrypto` `pgp_sym_encrypt()`:
- `clients.date_of_birth`
- `immigration_intake.passport_number`
- `immigration_intake.a_number`

**Encryption:**
```sql
UPDATE immigration_intake
SET passport_number = pgp_sym_encrypt(
  '123456789',
  current_setting('app.encryption_key')
)
WHERE id = '...';
```

**Decryption (only authorized queries):**
```sql
SELECT pgp_sym_decrypt(passport_number::bytea, current_setting('app.encryption_key'))
FROM immigration_intake
WHERE id = '...';
```

### Data in Transit
- All API calls over TLS/HTTPS
- Twilio WebSocket is `wss://` (TLS required by Twilio)
- Redis connection: use `rediss://` (TLS) in production

### What is NOT Stored
- Social Security Numbers
- Full financial account details
- Medical records or detailed health information
- Actual document files (checklist of what they have only)
- Detailed abuse/trauma narratives (flagged for attorney, not stored in AI intake)
- Immigration fraud admissions

---

## Data Retention

Automated retention job (run daily):

| Data | Retention from `created_at` | Action |
|---|---|---|
| Call recording URLs | 30 days | Delete from Twilio, set `recording_url = NULL` |
| Full transcripts (`conversation_messages.content`) | 30 days | Set content to `'[REDACTED]'`, keep metadata |
| PII fields (passport, DOB, A-number) | 90 days after `clients.deleted_at` | `pgcrypto` wipe |
| Lead scores, analytics metadata | Indefinite | No PII involved |
| Urgency alerts | Indefinite | Required audit trail |

**Deletion request handling:**
When a client requests data deletion under CCPA or similar:
1. Set `clients.deleted_at = NOW()`
2. Retention job picks up and purges PII fields after configured period
3. Log the deletion request in `urgency_alerts` with `alert_type = other` for audit trail
4. Anonymous aggregate data (call counts, conversion rates) may be retained

---

## Audit Logging

All sensitive data access should be logged:
- Decryption of PII fields
- GHL contact data export
- Manual call recording access
- Bulk data exports

Log schema (append to `call_logs.ai_action_items` or a separate audit table):
```json
{
  "action": "decrypt_pii",
  "field": "passport_number",
  "client_id": "...",
  "accessed_by": "staff@firm.com",
  "timestamp": "2024-01-01T00:00:00Z",
  "ip": "..."
}
```

---

## Disclaimer (include in AI intro)

The AI must state near the beginning of every call:
> "I'm an AI assistant for [Firm Name] Immigration Law. I'm not an attorney and can't provide legal advice, but I can gather some information and schedule you with one of our attorneys today."

This should be in the system prompt and included in the GREETING FSM state.

---

## CCPA (California Consumer Privacy Act)

If any callers are California residents:
- They have the right to know what data is collected
- They have the right to request deletion
- They have the right to opt out of sale of personal information (this system does not sell data)
- Your privacy policy must disclose AI use in client intake

**Practical steps:**
1. Add privacy policy URL to post-call SMS
2. Train staff on deletion request handling (see above)
3. Document what data categories are collected (see `DATABASE.md`)

---

## Security Checklist

- [ ] All API keys in environment variables, never in code or logs
- [ ] `TWILIO_AUTH_TOKEN` used to validate Twilio request signatures on every webhook
- [ ] `GHL_WEBHOOK_SECRET` used to validate GHL HMAC signatures on every webhook
- [ ] Redis uses password auth (`requirepass` in redis.conf)
- [ ] PostgreSQL uses SSL connection (`?sslmode=require`)
- [ ] Supabase row-level security enabled on PII tables
- [ ] No PII logged to stdout/stderr or log aggregators
- [ ] Rate limiting on all webhook endpoints (Twilio handles this, but add application-level too)
- [ ] HTTPS only in production (TLS termination at load balancer or ngrok in dev)
- [ ] Docker secrets or vault for encryption key (not `.env` in production)
