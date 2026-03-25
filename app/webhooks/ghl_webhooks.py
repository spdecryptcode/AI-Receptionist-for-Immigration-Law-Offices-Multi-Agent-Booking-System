"""
GoHighLevel (GHL) inbound webhooks.

GHL sends event notifications for:
  - contact.created / contact.updated
  - appointment.created / appointment.updated / appointment.cancelled
  - opportunity.created / opportunity.updated

All events are authenticated via HMAC-SHA256 signature in X-GHL-Signature header,
using the GHL_WEBHOOK_SECRET env var.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, Header, HTTPException, Request, status

from app.config import settings
from app.dependencies import get_redis_client

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/ghl", tags=["ghl"])


# ---------------------------------------------------------------------------
# Signature validation
# ---------------------------------------------------------------------------

def _validate_ghl_signature(body: bytes, signature_header: str) -> bool:
    """
    Validate GHL webhook signature.
    GHL signs the raw request body with HMAC-SHA256 using the webhook secret.
    Header value format: "sha256=<hex_digest>"
    """
    if not settings.ghl_webhook_secret:
        # If no secret is configured, skip validation (dev mode only)
        logger.warning("GHL_WEBHOOK_SECRET not set — skipping signature validation")
        return True

    if not signature_header.startswith("sha256="):
        return False

    provided_sig = signature_header[len("sha256="):]
    expected_sig = hmac.new(
        settings.ghl_webhook_secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(expected_sig, provided_sig)


# ---------------------------------------------------------------------------
# POST /ghl/webhook  — unified GHL event receiver
# ---------------------------------------------------------------------------

@router.post("/webhook")
async def ghl_webhook(
    request: Request,
    x_ghl_signature: str = Header(default="", alias="X-GHL-Signature"),
):
    """
    Receive all GHL webhook events. Validates signature and routes by event type.
    """
    body = await request.body()

    if not _validate_ghl_signature(body, x_ghl_signature):
        logger.warning("GHL webhook signature validation failed")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid GHL signature",
        )

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON payload",
        )

    event_type = payload.get("type", "")
    logger.info(f"GHL webhook received: {event_type}")

    redis = get_redis_client()

    if event_type in ("contact.created", "contact.updated"):
        await _handle_contact_event(payload, redis)
    elif event_type.startswith("appointment."):
        await _handle_appointment_event(payload, redis)
    elif event_type.startswith("opportunity."):
        await _handle_opportunity_event(payload, redis)
    else:
        logger.debug(f"Unhandled GHL event type: {event_type}")

    return {"ok": True}


# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------

async def _handle_contact_event(payload: dict, redis) -> None:
    """Sync GHL contact updates into Redis for in-flight call sessions."""
    contact = payload.get("contact", {})
    ghl_id = contact.get("id", "")
    phone = contact.get("phone", "")

    if ghl_id and phone:
        # Update the phone→ghl_id mapping in Redis
        await redis.hset("ghl:contacts", phone, ghl_id)
        logger.info(f"GHL contact synced: {phone} → {ghl_id}")


async def _handle_appointment_event(payload: dict, redis) -> None:
    """Queue appointment sync events for the database writer."""
    event_type = payload.get("type", "")
    appointment = payload.get("appointment", {})
    appt_id = appointment.get("id", "")

    logger.info(f"GHL appointment event {event_type}: {appt_id}")
    await redis.rpush("ghl:appointment_events", json.dumps({
        "type": event_type,
        "appointment": appointment,
    }))


async def _handle_opportunity_event(payload: dict, redis) -> None:
    """Queue opportunity events for lead score updates."""
    event_type = payload.get("type", "")
    opportunity = payload.get("opportunity", {})
    contact_id = opportunity.get("contactId", "")

    logger.info(f"GHL opportunity event {event_type}: contact {contact_id}")
    await redis.rpush("ghl:opportunity_events", json.dumps({
        "type": event_type,
        "opportunity": opportunity,
    }))
