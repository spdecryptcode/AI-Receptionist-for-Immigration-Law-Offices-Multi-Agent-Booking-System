"""
Social channel inbound webhook handler.

Handles inbound messages from Twilio Conversations API:
  - WhatsApp (via Twilio sandbox or business number)
  - Facebook Messenger (via Twilio Flex / Conversations)
  - Instagram Direct Messages

All channels share the same message processing pipeline:
  1. Parse the Twilio Conversations webhook payload
  2. Look up / create GHL contact
  3. Load conversation context from Redis (keyed by Conversations SID)
  4. Send message to GPT-4o for a brief, text-appropriate reply
  5. Reply via Twilio Conversations API
  6. Detect if caller wants to book — send booking link
  7. Log to analytics queue

FastAPI router mounted at /social/* in main.py.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Optional

import redis.asyncio as aioredis
from fastapi import APIRouter, Form, Header, HTTPException, Request, Response

from app.config import settings
from app.social.channel_router import route_message, format_reply, ChannelContext

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/social", tags=["social"])

_CONTEXT_PREFIX = "social_ctx:"
_CONTEXT_TTL = 86400  # 24 hours
_MAX_CONTEXT_TURNS = 8


# ─── Twilio Conversations webhook ─────────────────────────────────────────────

@router.post("/inbound")
async def social_inbound(
    request: Request,
    # Twilio Conversations sends form-encoded payloads
    ConversationSid: str = Form(...),
    Body: str = Form(""),
    Author: str = Form(""),        # phone/channel address of the sender
    Source: str = Form(""),        # "whatsapp", "messenger", "instagram", "sms"
    AccountSid: str = Form(""),
    x_twilio_signature: str = Header("", alias="X-Twilio-Signature"),
) -> Response:
    """
    Receives inbound messages from Twilio Conversations.
    Returns 204 quickly; processing is async.
    """
    # Validate Twilio signature
    raw_body = await request.body()
    _verify_twilio_signature(str(request.url), raw_body, x_twilio_signature)

    # Route and process asynchronously
    import asyncio
    asyncio.create_task(
        _process_social_message(
            conversation_sid=ConversationSid,
            body=Body.strip(),
            author=Author,
            channel=Source.lower() or "sms",
        ),
        name=f"social:{ConversationSid[:12]}",
    )
    return Response(status_code=204)


# ─── WhatsApp Sandbox webhook (standard Twilio Messaging format) ──────────────

@router.post("/whatsapp")
async def whatsapp_sandbox_inbound(
    request: Request,
    From: str = Form(""),
    To: str = Form(""),
    Body: str = Form(""),
    MessageSid: str = Form(""),
    AccountSid: str = Form(""),
    ProfileName: str = Form(""),
    x_twilio_signature: str = Header("", alias="X-Twilio-Signature"),
) -> Response:
    """
    Receives inbound WhatsApp messages from the Twilio sandbox.
    Uses standard Twilio Messaging webhook format (From/To/Body).
    Set this URL in Twilio Console → WhatsApp Sandbox Settings.
    """
    # Validate using form params dict (body stream already consumed by FastAPI)
    _verify_twilio_signature_form(
        str(request.url),
        {"From": From, "To": To, "Body": Body,
         "MessageSid": MessageSid, "AccountSid": AccountSid, "ProfileName": ProfileName},
        x_twilio_signature,
    )

    # Use sender number as conversation key so context persists across messages
    conversation_key = From.replace("whatsapp:", "").replace("+", "").strip() or MessageSid

    import asyncio
    asyncio.create_task(
        _process_whatsapp_message(
            conversation_key=conversation_key,
            body=Body.strip(),
            from_=From,
            to=To,
            profile_name=ProfileName,
        ),
        name=f"wa:{MessageSid[:12] if MessageSid else conversation_key[:12]}",
    )
    return Response(status_code=204)


async def _process_whatsapp_message(
    conversation_key: str,
    body: str,
    from_: str,
    to: str,
    profile_name: str,
) -> None:
    """Process a WhatsApp sandbox message and reply via Twilio Messages API."""
    if not body:
        return

    logger.info(f"[WA:{conversation_key}] message from {from_} ({profile_name}): {body[:80]}")

    redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
    try:
        ctx = await _load_context(redis_client, conversation_key)
        ctx["channel"] = "whatsapp"
        ctx["author"] = from_

        if not ctx.get("ghl_contact_id"):
            ctx["ghl_contact_id"] = await _lookup_ghl_contact(from_) or ""

        ctx["history"].append({"role": "user", "content": body})

        channel_ctx = ChannelContext(
            conversation_sid=conversation_key,
            author=from_,
            channel="whatsapp",
            history=ctx["history"],
            ghl_contact_id=ctx.get("ghl_contact_id", ""),
            language=ctx.get("language", _detect_language(body)),
        )
        reply_text, ctx["language"] = await route_message(channel_ctx, body)
        formatted = format_reply(reply_text, "whatsapp")

        # Reply: from_=sandbox number (To field), to=sender (From field)
        await _send_whatsapp_sandbox_reply(from_=to, to=from_, text=formatted)

        ctx["history"].append({"role": "assistant", "content": reply_text})
        if len(ctx["history"]) > _MAX_CONTEXT_TURNS * 2:
            ctx["history"] = ctx["history"][-(_MAX_CONTEXT_TURNS * 2):]

        await _save_context(redis_client, conversation_key, ctx)
        await _log_analytics(conversation_key, "whatsapp", from_, body, reply_text)

    finally:
        await redis_client.aclose()


async def _send_whatsapp_sandbox_reply(from_: str, to: str, text: str) -> None:
    """Send a WhatsApp reply via Twilio Messages API (sandbox)."""
    import asyncio
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, _send_whatsapp_sandbox_reply_sync, from_, to, text)
    except Exception as exc:
        logger.error(f"WhatsApp sandbox reply failed to {to}: {exc}")


def _send_whatsapp_sandbox_reply_sync(from_: str, to: str, text: str) -> None:
    from twilio.rest import Client as TwilioClient
    client = TwilioClient(settings.twilio_account_sid, settings.twilio_auth_token)
    client.messages.create(from_=from_, to=to, body=text)


# ─── Message processing pipeline ─────────────────────────────────────────────

async def _process_social_message(
    conversation_sid: str,
    body: str,
    author: str,
    channel: str,
) -> None:
    """Full processing pipeline for one inbound social message."""
    if not body:
        return

    logger.info(f"[{conversation_sid}] {channel} message from {author}: {body[:80]}")

    # 1. Load / create context
    redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
    try:
        ctx = await _load_context(redis_client, conversation_sid)
        ctx["channel"] = channel
        ctx["author"] = author

        # 2. GHL contact lookup (cached per conversation)
        if not ctx.get("ghl_contact_id"):
            ctx["ghl_contact_id"] = await _lookup_ghl_contact(author) or ""

        # 3. Add user turn to history
        ctx["history"].append({"role": "user", "content": body})

        # 4. Detect booking intent / get AI reply
        channel_ctx = ChannelContext(
            conversation_sid=conversation_sid,
            author=author,
            channel=channel,
            history=ctx["history"],
            ghl_contact_id=ctx.get("ghl_contact_id", ""),
            language=ctx.get("language", _detect_language(body)),
        )
        reply_text, ctx["language"] = await route_message(channel_ctx, body)

        # 5. Format reply for channel constraints
        formatted = format_reply(reply_text, channel)

        # 6. Send reply
        await _send_reply(conversation_sid, formatted)

        # 7. Add AI turn to history
        ctx["history"].append({"role": "assistant", "content": reply_text})

        # Trim context window
        if len(ctx["history"]) > _MAX_CONTEXT_TURNS * 2:
            ctx["history"] = ctx["history"][-(_MAX_CONTEXT_TURNS * 2):]

        # 8. Save context
        await _save_context(redis_client, conversation_sid, ctx)

        # 9. Analytics event
        await _log_analytics(conversation_sid, channel, author, body, reply_text)

    finally:
        await redis_client.aclose()


# ─── Context persistence ──────────────────────────────────────────────────────

async def _load_context(redis_client: aioredis.Redis, conv_sid: str) -> dict:
    key = f"{_CONTEXT_PREFIX}{conv_sid}"
    raw = await redis_client.get(key)
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
    return {"history": [], "ghl_contact_id": "", "language": "en", "channel": ""}


async def _save_context(redis_client: aioredis.Redis, conv_sid: str, ctx: dict) -> None:
    key = f"{_CONTEXT_PREFIX}{conv_sid}"
    await redis_client.setex(key, _CONTEXT_TTL, json.dumps(ctx))


# ─── Reply sending ────────────────────────────────────────────────────────────

async def _send_reply(conversation_sid: str, text: str) -> None:
    """Send a reply via Twilio Conversations REST API (sync call in executor)."""
    import asyncio
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, _send_reply_sync, conversation_sid, text)
    except Exception as exc:
        logger.error(f"[{conversation_sid}] Failed to send reply: {exc}")


def _send_reply_sync(conversation_sid: str, text: str) -> None:
    from twilio.rest import Client as TwilioClient
    client = TwilioClient(settings.twilio_account_sid, settings.twilio_auth_token)
    client.conversations.v1.conversations(conversation_sid).messages.create(
        body=text,
        author="system",
    )


# ─── GHL contact lookup ───────────────────────────────────────────────────────

async def _lookup_ghl_contact(author: str) -> Optional[str]:
    """
    Attempt to find a GHL contact by phone/channel address.
    Returns contact_id or None.
    """
    try:
        from app.crm.ghl_client import get_ghl_client
        ghl = get_ghl_client()
        # Strip WhatsApp prefix: "whatsapp:+15551234567" → "+15551234567"
        phone = author.split(":")[-1] if ":" in author else author
        contacts = await ghl.search_contacts(phone=phone)
        if contacts:
            return contacts[0].get("id")
    except Exception as exc:
        logger.error(f"GHL contact lookup error for {author}: {exc}")
    return None


# ─── Analytics logging ────────────────────────────────────────────────────────

async def _log_analytics(
    conversation_sid: str,
    channel: str,
    author: str,
    user_msg: str,
    reply: str,
) -> None:
    try:
        redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
        payload = json.dumps({
            "type": "social_message",
            "conversation_sid": conversation_sid,
            "channel": channel,
            "author": author,
            "user_message": user_msg[:500],
            "reply": reply[:500],
            "ts": int(time.time() * 1000),
        })
        async with redis_client:
            await redis_client.rpush("analytics_events", payload)
    except Exception as exc:
        logger.debug(f"Social analytics log error: {exc}")


# ─── Signature validation ─────────────────────────────────────────────────────

def _verify_twilio_signature(url: str, raw_body: bytes, signature: str) -> None:
    """
    Validate Twilio's HMAC-SHA1 signature.
    Raises HTTP 403 on failure.
    """
    if not settings.twilio_auth_token:
        return  # skip in test environments where token is not set
    try:
        from twilio.request_validator import RequestValidator
        validator = RequestValidator(settings.twilio_auth_token)
        # For form-encoded Twilio webhooks, pass the raw body as bytes
        if not validator.validate(url, raw_body, signature):
            raise HTTPException(status_code=403, detail="Invalid Twilio signature")
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Signature validation error: {exc}")
        raise HTTPException(status_code=403, detail="Signature validation failed")


def _verify_twilio_signature_form(url: str, params: dict, signature: str) -> None:
    """
    Validate Twilio signature using already-parsed form params dict.
    Used when the request body stream has already been consumed by FastAPI.
    """
    if not settings.twilio_auth_token:
        return
    try:
        from twilio.request_validator import RequestValidator
        validator = RequestValidator(settings.twilio_auth_token)
        if not validator.validate(url, params, signature):
            raise HTTPException(status_code=403, detail="Invalid Twilio signature")
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Signature validation error: {exc}")
        raise HTTPException(status_code=403, detail="Signature validation failed")


# ─── Language detection ───────────────────────────────────────────────────────

def _detect_language(text: str) -> str:
    """Simple heuristic: check for Spanish markers."""
    _ES_MARKERS = frozenset([
        "hola", "gracias", "ayuda", "abogado", "visa", "caso",
        "necesito", "quiero", "tengo", "estoy", "cómo", "cuándo",
        "por favor", "buenos", "días", "tardes",
    ])
    lower = text.lower()
    if any(marker in lower for marker in _ES_MARKERS):
        return "es"
    return "en"
