"""
Unit tests for TwiML builder functions.

These tests do NOT require a live Twilio connection — they just verify that
each builder returns valid XML containing the expected TwiML verbs/nouns.
"""
import re
import pytest

# ---------------------------------------------------------------------------
# Minimal settings stub so importing twiml_responses doesn't need a full env
# ---------------------------------------------------------------------------
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

from app.telephony.twiml_responses import (
    twiml_ai_stream,
    twiml_ivr_menu,
    twiml_ivr_digit,
    twiml_new_consultation_offer,
    twiml_voicemail,
    twiml_existing_case_voicemail,
    twiml_front_desk_transfer,
    twiml_after_hours,
    twiml_at_capacity,
    twiml_error_goodbye,
)


def _is_xml(text: str) -> bool:
    return text.strip().startswith("<?xml") or text.strip().startswith("<Response")


class TestTwimlAiStream:
    def test_returns_xml(self):
        xml = twiml_ai_stream("CA123")
        assert _is_xml(xml)

    def test_contains_connect(self):
        xml = twiml_ai_stream("CA123")
        assert "<Connect>" in xml or "<connect>" in xml.lower()

    def test_contains_stream(self):
        xml = twiml_ai_stream("CA123")
        assert "Stream" in xml or "stream" in xml


class TestTwimlIvrMenu:
    def test_english_returns_xml(self):
        xml = twiml_ivr_menu("en")
        assert _is_xml(xml)

    def test_spanish_returns_xml(self):
        xml = twiml_ivr_menu("es")
        assert _is_xml(xml)

    def test_gather_present(self):
        xml = twiml_ivr_menu("en")
        assert "Gather" in xml or "gather" in xml.lower()

    def test_different_languages_produce_different_xml(self):
        en = twiml_ivr_menu("en")
        es = twiml_ivr_menu("es")
        assert en != es


class TestTwimlIvrDigit:
    def test_digit_1_returns_xml(self):
        xml = twiml_ivr_digit("1")
        assert _is_xml(xml)

    def test_digit_2_returns_xml(self):
        xml = twiml_ivr_digit("2")
        assert _is_xml(xml)

    def test_unknown_digit_returns_xml(self):
        xml = twiml_ivr_digit("9")
        assert _is_xml(xml)


class TestTwimlNewConsultationOffer:
    def test_english(self):
        xml = twiml_new_consultation_offer("en")
        assert _is_xml(xml)

    def test_spanish(self):
        xml = twiml_new_consultation_offer("es")
        assert _is_xml(xml)


class TestTwimlVoicemail:
    def test_returns_xml(self):
        xml = twiml_voicemail("en")
        assert _is_xml(xml)

    def test_contains_record(self):
        xml = twiml_voicemail("en")
        assert "Record" in xml or "record" in xml.lower()


class TestTwimlExistingCaseVoicemail:
    def test_returns_xml(self):
        xml = twiml_existing_case_voicemail("en")
        assert _is_xml(xml)


class TestTwimlFrontDeskTransfer:
    def test_returns_xml(self):
        xml = twiml_front_desk_transfer("en")
        assert _is_xml(xml)

    def test_contains_dial(self):
        xml = twiml_front_desk_transfer("en")
        assert "Dial" in xml or "dial" in xml.lower()


class TestTwimlAfterHours:
    def test_english(self):
        xml = twiml_after_hours("en")
        assert _is_xml(xml)

    def test_spanish(self):
        xml = twiml_after_hours("es")
        assert _is_xml(xml)

    def test_different_languages(self):
        en = twiml_after_hours("en")
        es = twiml_after_hours("es")
        assert en != es


class TestTwimlAtCapacity:
    def test_returns_xml(self):
        xml = twiml_at_capacity("en")
        assert _is_xml(xml)


class TestTwimlErrorGoodbye:
    def test_returns_xml(self):
        xml = twiml_error_goodbye("en")
        assert _is_xml(xml)
