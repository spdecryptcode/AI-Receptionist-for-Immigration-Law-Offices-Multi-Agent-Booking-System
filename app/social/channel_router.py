"""
Social channel message routing and formatting.

Responsibilities:
  1. `route_message()` — decide whether to answer via GPT or send a
     structured booking/info response based on intent detection
  2. `format_reply()` — apply per-channel constraints (WhatsApp markdown,
     SMS 160-char segments, Messenger plain text)
  3. `build_booking_message()` — compose a booking CTA with the calendar link

Each channel has different:
  - Character limits (SMS 160 / WhatsApp 4096 / Messenger 2000)
  - Markdown support (WhatsApp bold *text*, Messenger none, SMS none)
  - Emoji etiquette
"""
from __future__ import annotations

import dataclasses
import logging
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)


# ─── Data classes ─────────────────────────────────────────────────────────────

@dataclasses.dataclass
class ChannelContext:
    conversation_sid: str
    author: str
    channel: str          # "whatsapp" | "messenger" | "instagram" | "sms"
    history: list[dict]   # [{"role": ..., "content": ...}]
    ghl_contact_id: str
    language: str = "en"


# ─── Routing ──────────────────────────────────────────────────────────────────

_BOOKING_KEYWORDS_EN = frozenset([
    "appointment", "schedule", "book", "consult", "consultation",
    "meeting", "available", "availability", "slot", "when can",
])
_BOOKING_KEYWORDS_ES = frozenset([
    "cita", "consulta", "horario", "agendar", "disponible",
    "disponibilidad", "reunión", "cuándo", "quando",
])
_EMERGENCY_KEYWORDS = frozenset([
    "detained", "deported", "deport", "ice arrest", "raid",
    "detenido", "deportación", "arresto", "redada",
])


async def route_message(ctx: ChannelContext, body: str) -> tuple[str, str]:
    """
    Decide what reply to send and return `(reply_text, detected_language)`.

    Priority order:
      1. Emergency detection → urgent reply + office phone
      2. Booking intent → friendly CTA with link
      3. Default → GPT-4o text-appropriate conversational reply
    """
    lower = body.lower()

    # Detect language from content if history is short
    language = ctx.language
    if _is_spanish(lower):
        language = "es"

    # Priority 1: Emergency
    if any(kw in lower for kw in _EMERGENCY_KEYWORDS):
        reply = _emergency_reply(language)
        return reply, language

    # Priority 2: Booking intent
    booking_kws = _BOOKING_KEYWORDS_ES if language == "es" else _BOOKING_KEYWORDS_EN
    if any(kw in lower for kw in booking_kws):
        reply = build_booking_message(language, ctx.channel)
        return reply, language

    # Priority 3: GPT-4o conversational reply
    reply = await _gpt_reply(ctx, body, language)
    return reply, language


async def _gpt_reply(ctx: ChannelContext, body: str, language: str) -> str:
    """Generate a short, text-appropriate reply via GPT-4o."""
    lang_name = "Spanish" if language == "es" else "English"
    channel_note = {
        "sms": "Keep reply under 160 characters if possible.",
        "whatsapp": "You may use *bold* for emphasis. Keep under 300 words.",
        "messenger": "Keep reply conversational and under 200 words.",
        "instagram": "Keep reply friendly and under 150 words.",
    }.get(ctx.channel, "Keep reply concise.")

    system_prompt = (
        f"You are Aria, an AI intake specialist for an immigration law office. "
        f"Reply in {lang_name}. {channel_note} "
        "Do not provide legal advice. Offer to schedule a free consultation when appropriate. "
        "Be warm, empathetic, and professional."
    )
    messages = [{"role": "system", "content": system_prompt}]

    # Add recent history (last 6 turns)
    for turn in ctx.history[-12:]:
        if turn.get("role") in ("user", "assistant"):
            messages.append({"role": turn["role"], "content": turn["content"]})

    # Append current message if not already added
    if not ctx.history or ctx.history[-1].get("content") != body:
        messages.append({"role": "user", "content": body})

    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=settings.openai_api_key)
        resp = await client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            max_tokens=200,
            temperature=0.4,
        )
        return resp.choices[0].message.content.strip()
    except Exception as exc:
        logger.error(f"GPT reply error on {ctx.channel}: {exc}")
        return _fallback_reply(language)


# ─── Formatters ───────────────────────────────────────────────────────────────

_CHANNEL_LIMITS = {
    "sms": 160,
    "whatsapp": 4096,
    "messenger": 2000,
    "instagram": 1000,
}


def format_reply(text: str, channel: str) -> str:
    """
    Apply per-channel formatting constraints:
      - SMS: strip markdown, truncate to 160 chars with continuation note
      - WhatsApp: keep *bold* markup, allow long messages
      - Messenger/Instagram: strip markdown, respect character limits
    """
    if channel == "sms":
        clean = _strip_markdown(text)
        max_len = _CHANNEL_LIMITS["sms"]
        if len(clean) > max_len:
            clean = clean[:max_len - 20] + "... (cont'd in next msg)"
        return clean
    elif channel == "whatsapp":
        # WhatsApp supports *bold*, _italic_ — keep as-is
        limit = _CHANNEL_LIMITS["whatsapp"]
        return text[:limit]
    elif channel in ("messenger", "instagram"):
        clean = _strip_markdown(text)
        limit = _CHANNEL_LIMITS.get(channel, 2000)
        return clean[:limit]
    return text  # default: no transformation


def _strip_markdown(text: str) -> str:
    """Remove common markdown markers for plain-text channels."""
    import re
    text = re.sub(r'\*+([^*]+)\*+', r'\1', text)  # bold/italic
    text = re.sub(r'_+([^_]+)_+', r'\1', text)     # underscore italic
    text = re.sub(r'`[^`]+`', '', text)             # inline code
    text = re.sub(r'#+\s', '', text)                 # headers
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)  # links → label
    return text.strip()


# ─── Booking CTA ──────────────────────────────────────────────────────────────

def build_booking_message(language: str = "en", channel: str = "sms") -> str:
    """
    Return a booking call-to-action with the online scheduling link.
    Formatted appropriately for the channel.
    """
    booking_url = getattr(settings, "booking_url", f"https://{settings.base_host}/book")
    office_phone = getattr(settings, "office_direct_number", settings.twilio_phone_number)

    if language == "es":
        if channel == "whatsapp":
            return (
                f"*Consulta Gratuita Disponible* ✅\n\n"
                f"Reserve su cita en línea aquí:\n{booking_url}\n\n"
                f"O llámenos directamente: {office_phone}\n\n"
                f"Horario: Lunes a Viernes, 9am–5pm"
            )
        return (
            f"Consulta gratuita disponible. Reserve en línea: {booking_url} "
            f"o llame al {office_phone}"
        )
    else:
        if channel == "whatsapp":
            return (
                f"*Free Consultation Available* ✅\n\n"
                f"Book your appointment online here:\n{booking_url}\n\n"
                f"Or call us directly: {office_phone}\n\n"
                f"Hours: Monday–Friday, 9am–5pm"
            )
        return (
            f"Free consultation available. Book online: {booking_url} "
            f"or call {office_phone}"
        )


# ─── Fallbacks ────────────────────────────────────────────────────────────────

def _emergency_reply(language: str) -> str:
    phone = getattr(settings, "office_direct_number", settings.twilio_phone_number)
    if language == "es":
        return (
            f"🚨 Si usted o alguien que conoce está detenido o enfrenta deportación inmediata, "
            f"llame a nuestra línea de emergencias AHORA: {phone}\n\n"
            f"Estamos disponibles para situaciones de emergencia."
        )
    return (
        f"🚨 If you or someone you know is detained or facing immediate deportation, "
        f"please call our emergency line NOW: {phone}\n\n"
        f"We handle emergency immigration situations."
    )


def _fallback_reply(language: str) -> str:
    phone = getattr(settings, "office_direct_number", settings.twilio_phone_number)
    if language == "es":
        return f"Gracias por escribirnos. Para asistencia inmediata llame al {phone}."
    return f"Thank you for reaching out. For immediate assistance please call {phone}."


def _is_spanish(text: str) -> bool:
    _ES = frozenset([
        "hola", "gracias", "ayuda", "necesito", "quiero", "tengo",
        "cómo", "usted", "por favor", "buenos", "días", "estoy",
    ])
    return any(w in text for w in _ES)
