"""
Voicemail processing pipeline.

Triggered by Twilio's RecordingStatusCallback webhook after a recording completes.
Pipeline:
  1. Download recording audio from Twilio
  2. Transcribe via Deepgram pre-recorded async API (faster than polling)
  3. Summarise transcript via GPT-4o (< 150 tokens)
  4. Create a task in GHL assigned to the intake coordinator
  5. Optionally alert the on-call attorney via SMS (for EMERGENCY flagged voicemails)
  6. Store audit row in `voicemail_logs` Redis queue → db_worker

All steps after download are best-effort (errors are logged, not raised).
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_DG_URL = "https://api.deepgram.com/v1/listen"
_EMERGENCY_KEYWORDS = frozenset(
    [
        "detained", "deportation", "ice", "arrest", "raid", "deport",
        "detenido", "deportación", "detención", "arresto",
        "emergency", "emergencia", "urgent", "urgente",
    ]
)


# ─── Public entrypoint ────────────────────────────────────────────────────────

async def process_voicemail(
    recording_sid: str,
    recording_url: str,
    caller_number: str,
    call_sid: str,
    duration_sec: int,
    language: str = "en",
) -> None:
    """
    Full voicemail processing pipeline.  Designed to be called as a background task
    so the Twilio webhook can return 204 immediately.

    recording_url — Twilio URL (without .mp3 suffix; we append it)
    """
    logger.info(f"[{call_sid}] Voicemail received: recording_sid={recording_sid} duration={duration_sec}s")

    # 1. Download audio
    audio_bytes = await _download_recording(recording_url)
    if not audio_bytes:
        logger.error(f"[{call_sid}] Failed to download recording {recording_sid}")
        await _queue_voicemail_row(call_sid, recording_sid, caller_number, "", "", "download_failed")
        return

    # 2. Transcribe
    transcript = await _transcribe(audio_bytes, language)
    if not transcript:
        logger.warning(f"[{call_sid}] Transcription empty for recording {recording_sid}")
        transcript = "[Transcription unavailable]"

    logger.info(f"[{call_sid}] Transcript ({len(transcript)} chars): {transcript[:120]}...")

    # 3. Summarise
    summary = await _summarise(transcript, language)

    # 4. Detect emergency
    is_emergency = _is_emergency(transcript)

    # 5. Create GHL task
    ghl_task_id = await _create_ghl_task(
        caller_number=caller_number,
        transcript=transcript,
        summary=summary,
        is_emergency=is_emergency,
        duration_sec=duration_sec,
    )

    # 6. SMS attorney if emergency
    if is_emergency:
        asyncio.create_task(
            _alert_attorney_sms(caller_number, summary),
            name=f"vm_sms:{recording_sid}",
        )

    # 7. Queue audit row
    await _queue_voicemail_row(
        call_sid=call_sid,
        recording_sid=recording_sid,
        caller_number=caller_number,
        transcript=transcript,
        summary=summary,
        status="processed",
        ghl_task_id=ghl_task_id or "",
        is_emergency=is_emergency,
    )
    logger.info(f"[{call_sid}] Voicemail pipeline complete. emergency={is_emergency}")


# ─── Step 1: Download recording ───────────────────────────────────────────────

async def _download_recording(recording_url: str) -> Optional[bytes]:
    """
    Download a Twilio recording as MP3.
    Twilio requires Basic Auth (account SID + auth token).
    """
    url = recording_url if recording_url.endswith(".mp3") else f"{recording_url}.mp3"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                url,
                auth=(settings.twilio_account_sid, settings.twilio_auth_token),
            )
            resp.raise_for_status()
            return resp.content
    except Exception as exc:
        logger.error(f"Recording download error: {exc}")
        return None


# ─── Step 2: Transcribe via Deepgram ─────────────────────────────────────────

async def _transcribe(audio_bytes: bytes, language: str = "en") -> str:
    """
    Submit MP3 audio to Deepgram pre-recorded endpoint.
    Uses nova-3 with multi-language detection.
    Returns the best transcript string.
    """
    params = {
        "model": "nova-3",
        "language": "multi",
        "punctuate": "true",
        "smart_format": "true",
        "diarize": "false",
    }
    headers = {
        "Authorization": f"Token {settings.deepgram_api_key}",
        "Content-Type": "audio/mp3",
    }
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                _DG_URL,
                content=audio_bytes,
                headers=headers,
                params=params,
            )
            resp.raise_for_status()
            data = resp.json()
            channels = data.get("results", {}).get("channels", [])
            if channels:
                alts = channels[0].get("alternatives", [])
                if alts:
                    return alts[0].get("transcript", "").strip()
        return ""
    except Exception as exc:
        logger.error(f"Deepgram transcription error: {exc}")
        return ""


# ─── Step 3: GPT-4o summary ───────────────────────────────────────────────────

async def _summarise(transcript: str, language: str = "en") -> str:
    """
    One-paragraph GPT-4o summary of the voicemail.
    Caps at 3 sentences / ~150 tokens.
    """
    if not transcript or transcript == "[Transcription unavailable]":
        return "No transcription available."

    lang_hint = "Respond in English." if language == "en" else "Responde en español."
    system_prompt = (
        f"You are an assistant for an immigration law office. {lang_hint} "
        "Summarize the voicemail in 1-3 sentences. "
        "Include: caller's name (if mentioned), reason for call, urgency level, "
        "and any specific dates or case numbers mentioned. Be concise."
    )
    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=settings.openai_api_key)
        resp = await client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Voicemail transcript:\n{transcript}"},
            ],
            max_tokens=150,
            temperature=0.2,
        )
        return resp.choices[0].message.content.strip()
    except Exception as exc:
        logger.error(f"GPT-4o voicemail summary error: {exc}")
        return transcript[:300]


# ─── Step 4: Emergency detection ─────────────────────────────────────────────

def _is_emergency(transcript: str) -> bool:
    lower = transcript.lower()
    return any(kw in lower for kw in _EMERGENCY_KEYWORDS)


# ─── Step 5: Create GHL task ─────────────────────────────────────────────────

async def _create_ghl_task(
    caller_number: str,
    transcript: str,
    summary: str,
    is_emergency: bool,
    duration_sec: int,
) -> Optional[str]:
    """
    Look up (or create) the GHL contact by phone, then create a follow-up task.
    Returns the task ID on success.
    """
    try:
        from app.crm.ghl_client import get_ghl_client
        ghl = get_ghl_client()

        # Find contact
        contacts = await ghl.search_contacts(phone=caller_number)
        contact_id: Optional[str] = None
        if contacts:
            contact_id = contacts[0].get("id")
        else:
            # Create minimal contact
            created = await ghl.create_contact({"phone": caller_number})
            contact_id = created.get("contact", {}).get("id") if created else None

        if not contact_id:
            logger.warning(f"Could not resolve GHL contact for {caller_number}")
            return None

        priority = "high" if is_emergency else "normal"
        title = (
            f"{'🚨 URGENT — ' if is_emergency else ''}Voicemail from {caller_number} "
            f"({duration_sec}s)"
        )
        body = f"Summary: {summary}\n\nFull transcript:\n{transcript[:1000]}"

        task = await ghl.create_task(
            contact_id=contact_id,
            title=title,
            body=body,
            due_date=_next_business_day_iso(),
            assignee_id=getattr(settings, "ghl_default_assignee_id", None) or "",
        )
        return (task or {}).get("id")
    except Exception as exc:
        logger.error(f"GHL task creation error: {exc}")
        return None


# ─── Step 6: Attorney SMS alert ───────────────────────────────────────────────

async def _alert_attorney_sms(caller_number: str, summary: str) -> None:
    attorney_phone = getattr(settings, "attorney_alert_phone", None)
    if not attorney_phone:
        return
    try:
        import asyncio
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _send_sms_sync, attorney_phone, caller_number, summary)
    except Exception as exc:
        logger.error(f"Attorney SMS alert error: {exc}")


def _send_sms_sync(attorney_phone: str, caller_number: str, summary: str) -> None:
    from twilio.rest import Client as TwilioClient
    client = TwilioClient(settings.twilio_account_sid, settings.twilio_auth_token)
    body = (
        f"🚨 URGENT VOICEMAIL from {caller_number}\n\n{summary}\n\n"
        f"— IVR System"
    )
    client.messages.create(
        to=attorney_phone,
        from_=settings.twilio_phone_number,
        body=body[:1600],
    )


# ─── Step 7: Queue audit row ──────────────────────────────────────────────────

async def _queue_voicemail_row(
    call_sid: str,
    recording_sid: str,
    caller_number: str,
    transcript: str,
    summary: str,
    status: str,
    ghl_task_id: str = "",
    is_emergency: bool = False,
) -> None:
    """Push a voicemail audit row to Redis → db_worker writes to voicemail_logs."""
    try:
        import redis.asyncio as aioredis
        redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
        payload = json.dumps({
            "type": "voicemail_log",
            "call_sid": call_sid,
            "recording_sid": recording_sid,
            "caller_number": caller_number,
            "transcript": transcript[:2000],
            "summary": summary,
            "ghl_task_id": ghl_task_id,
            "is_emergency": is_emergency,
            "status": status,
        })
        await redis_client.rpush("voicemail_log_queue", payload)
        await redis_client.aclose()
    except Exception as exc:
        logger.error(f"Failed to queue voicemail row: {exc}")


# ─── Utility ──────────────────────────────────────────────────────────────────

def _next_business_day_iso() -> str:
    """Return ISO date string for the next business day (Mon-Fri)."""
    from datetime import date, timedelta
    d = date.today() + timedelta(days=1)
    while d.weekday() >= 5:  # Sat=5, Sun=6
        d += timedelta(days=1)
    return d.isoformat()
