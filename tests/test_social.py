"""
Unit tests for social channel modules:
  - app/social/channel_router.py   (format, route, booking CTA)
  - app/social/webhook_handler.py  (language detect, context, signature)
"""
import os
os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "testtoken")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+10000000000")
os.environ.setdefault("OPENAI_API_KEY", "sk-test")
os.environ.setdefault("DEEPGRAM_API_KEY", "dg-test")
os.environ.setdefault("ELEVENLABS_API_KEY", "el-test")
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_ANON_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")
os.environ.setdefault("BASE_URL", "https://test.example.com")
os.environ.setdefault("GHL_API_KEY", "ghl-test")
os.environ.setdefault("GHL_LOCATION_ID", "loc-test")
os.environ.setdefault("ELEVENLABS_VOICE_ID_EN", "voice-en-test")
os.environ.setdefault("ELEVENLABS_VOICE_ID_ES", "voice-es-test")
os.environ.setdefault("GHL_CALENDAR_ID", "cal-test")
os.environ.setdefault("GOOGLE_CALENDAR_ID", "gcal-test")

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.social.channel_router import (
    ChannelContext,
    _strip_markdown,
    build_booking_message,
    format_reply,
    route_message,
)
from app.social.webhook_handler import (
    _detect_language,
    _load_context,
    _save_context,
    _verify_twilio_signature,
)


# ===========================================================================
# channel_router — _strip_markdown
# ===========================================================================

class TestStripMarkdown:
    def test_removes_bold_asterisks(self):
        assert _strip_markdown("**bold text**") == "bold text"

    def test_removes_single_asterisk_italic(self):
        assert _strip_markdown("*italic*") == "italic"

    def test_removes_underscore_italic(self):
        assert _strip_markdown("_italic_") == "italic"

    def test_removes_inline_code(self):
        result = _strip_markdown("use `code` here")
        assert "`" not in result

    def test_removes_headers(self):
        result = _strip_markdown("## Section Title")
        assert "##" not in result
        assert "Section Title" in result

    def test_converts_link_to_label(self):
        result = _strip_markdown("[click here](https://example.com)")
        assert "click here" in result
        assert "https://example.com" not in result

    def test_plain_text_unchanged(self):
        text = "Hello, how can I help you today?"
        assert _strip_markdown(text) == text


# ===========================================================================
# channel_router — format_reply
# ===========================================================================

class TestFormatReply:
    def test_sms_strips_markdown(self):
        result = format_reply("**Bold** reply", "sms")
        assert "**" not in result
        assert "Bold reply" in result

    def test_sms_truncates_long_text_with_continuation_note(self):
        # long text → truncated to 140 chars + "... (cont'd in next msg)" suffix
        long_text = "A" * 200
        result = format_reply(long_text, "sms")
        assert "cont'd" in result
        assert len(result) < 200

    def test_sms_short_text_not_truncated(self):
        short = "Hello, how can I help?"
        assert format_reply(short, "sms") == short

    def test_whatsapp_preserves_bold_markup(self):
        text = "*bold* and _italic_"
        result = format_reply(text, "whatsapp")
        assert "*bold*" in result

    def test_whatsapp_respects_4096_char_limit(self):
        long_text = "x" * 5000
        result = format_reply(long_text, "whatsapp")
        assert len(result) <= 4096

    def test_messenger_strips_markdown(self):
        result = format_reply("**check** this [link](url)", "messenger")
        assert "**" not in result
        assert "url" not in result

    def test_instagram_strips_markdown(self):
        result = format_reply("*hello* world", "instagram")
        assert "*" not in result

    def test_instagram_respects_1000_char_limit(self):
        long_text = "y" * 1500
        result = format_reply(long_text, "instagram")
        assert len(result) <= 1000

    def test_unknown_channel_returns_original(self):
        text = "**some** text"
        assert format_reply(text, "unknown_channel") == text


# ===========================================================================
# channel_router — build_booking_message
# ===========================================================================

class TestBuildBookingMessage:
    def test_english_default_contains_booking_url(self):
        result = build_booking_message("en")
        assert "http" in result or "book" in result.lower()

    def test_english_contains_free_consultation(self):
        result = build_booking_message("en")
        assert "consultation" in result.lower() or "free" in result.lower()

    def test_spanish_contains_consulta(self):
        result = build_booking_message("es")
        assert "consulta" in result.lower()

    def test_spanish_contains_gratuita(self):
        result = build_booking_message("es")
        assert "gratuita" in result.lower()

    def test_whatsapp_english_has_bold_formatting(self):
        result = build_booking_message("en", "whatsapp")
        assert "*" in result

    def test_whatsapp_spanish_has_bold_formatting(self):
        result = build_booking_message("es", "whatsapp")
        assert "*" in result

    def test_sms_english_is_compact(self):
        result = build_booking_message("en", "sms")
        assert len(result) < 400  # SMS should be concise


# ===========================================================================
# channel_router — route_message
# ===========================================================================

class TestRouteMessage:
    def _make_ctx(self, language="en") -> ChannelContext:
        return ChannelContext(
            conversation_sid="CS123",
            author="+15551234567",
            channel="sms",
            history=[],
            ghl_contact_id="",
            language=language,
        )

    async def test_emergency_keyword_triggers_emergency_reply(self):
        ctx = self._make_ctx()
        reply, lang = await route_message(ctx, "I was detained by ICE officers")
        assert "🚨" in reply or "detained" in reply.lower() or "emergency" in reply.lower()

    async def test_detenido_triggers_emergency_reply(self):
        ctx = self._make_ctx(language="es")
        reply, lang = await route_message(ctx, "estoy detenido")
        assert "🚨" in reply

    async def test_booking_keyword_triggers_booking_cta(self):
        ctx = self._make_ctx()
        reply, lang = await route_message(ctx, "I want to book an appointment")
        assert "consult" in reply.lower() or "book" in reply.lower() or "http" in reply

    async def test_booking_keyword_spanish(self):
        ctx = self._make_ctx(language="es")
        reply, lang = await route_message(ctx, "quiero una cita")
        assert "consulta" in reply.lower() or "cita" in reply.lower() or "http" in reply

    async def test_gpt_fallback_for_normal_message(self):
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock(message=MagicMock(content="I can help you with that."))]
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_resp)

        ctx = self._make_ctx()
        with patch("openai.AsyncOpenAI", return_value=mock_client):
            reply, lang = await route_message(ctx, "What are your office hours?")

        assert "help" in reply.lower() or len(reply) > 0

    async def test_spanish_detected_from_message_content(self):
        ctx = self._make_ctx(language="en")
        # "hola" is a Spanish marker → language should be detected as "es"
        # Emergency detection won't fire; no booking keyword → GPT fallback
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock(message=MagicMock(content="Respuesta en español"))]
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_resp)

        with patch("openai.AsyncOpenAI", return_value=mock_client):
            reply, detected_lang = await route_message(ctx, "hola necesito ayuda con mi caso")

        assert detected_lang == "es"

    async def test_gpt_exception_uses_fallback_reply(self):
        ctx = self._make_ctx()
        with patch("openai.AsyncOpenAI", side_effect=Exception("openai error")):
            reply, lang = await route_message(ctx, "What documents do I need?")
        # Should return a fallback reply, not raise
        assert isinstance(reply, str)
        assert len(reply) > 0


# ===========================================================================
# webhook_handler — _detect_language
# ===========================================================================

class TestDetectLanguage:
    def test_hola_detected_as_spanish(self):
        assert _detect_language("hola, como estas") == "es"

    def test_gracias_detected_as_spanish(self):
        assert _detect_language("gracias por su ayuda") == "es"

    def test_english_message_detected_as_english(self):
        # Avoid Spanish marker words like "visa", "caso", "ayuda"
        assert _detect_language("please call me back tomorrow morning") == "en"

    def test_empty_string_defaults_to_english(self):
        assert _detect_language("") == "en"

    def test_abogado_detected_as_spanish(self):
        assert _detect_language("necesito un abogado") == "es"


# ===========================================================================
# webhook_handler — _load_context / _save_context
# ===========================================================================

class TestLoadContext:
    async def test_returns_default_on_cache_miss(self):
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=None)
        ctx = await _load_context(mock_redis, "CS_MISSING")
        assert ctx["history"] == []
        assert ctx["language"] == "en"
        assert ctx["ghl_contact_id"] == ""

    async def test_returns_parsed_context_on_cache_hit(self):
        stored = json.dumps({
            "history": [{"role": "user", "content": "hello"}],
            "ghl_contact_id": "GHL123",
            "language": "es",
            "channel": "whatsapp",
        })
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=stored)
        ctx = await _load_context(mock_redis, "CS123")
        assert ctx["language"] == "es"
        assert ctx["ghl_contact_id"] == "GHL123"
        assert len(ctx["history"]) == 1

    async def test_returns_default_on_invalid_json(self):
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value="not-valid-json")
        ctx = await _load_context(mock_redis, "CS_BAD")
        assert ctx["history"] == []


class TestSaveContext:
    async def test_calls_setex_with_correct_key_prefix(self):
        mock_redis = AsyncMock()
        ctx = {"history": [], "ghl_contact_id": "", "language": "en", "channel": "sms"}
        await _save_context(mock_redis, "CS_SAVE_123", ctx)
        mock_redis.setex.assert_called_once()
        key = mock_redis.setex.call_args[0][0]
        assert "CS_SAVE_123" in key

    async def test_serializes_context_as_json(self):
        mock_redis = AsyncMock()
        ctx = {"history": [{"role": "user", "content": "hi"}], "language": "es", "ghl_contact_id": "G1", "channel": "sms"}
        await _save_context(mock_redis, "CS_SER", ctx)
        stored_value = mock_redis.setex.call_args[0][2]
        parsed = json.loads(stored_value)
        assert parsed["language"] == "es"


# ===========================================================================
# webhook_handler — _verify_twilio_signature
# ===========================================================================

class TestVerifyTwilioSignature:
    def test_skips_validation_when_no_auth_token(self):
        """With empty auth token, validation is skipped (no exception raised)."""
        with patch("app.social.webhook_handler.settings") as mock_settings:
            mock_settings.twilio_auth_token = ""
            # Should not raise
            _verify_twilio_signature("https://example.com/social/inbound", b"body=hello", "any-sig")

    def test_raises_403_on_invalid_signature(self):
        """Valid auth token but wrong signature → HTTPException 403."""
        mock_validator = MagicMock()
        mock_validator.validate = MagicMock(return_value=False)
        mock_validator_cls = MagicMock(return_value=mock_validator)

        with (
            patch("app.social.webhook_handler.settings") as mock_settings,
            patch("twilio.request_validator.RequestValidator", mock_validator_cls),
        ):
            mock_settings.twilio_auth_token = "real-token"
            with pytest.raises(HTTPException) as exc_info:
                _verify_twilio_signature(
                    "https://example.com/social/inbound",
                    b"body=test",
                    "wrong-sig",
                )
        assert exc_info.value.status_code == 403
