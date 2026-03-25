"""
Unit tests for app/crm/contact_manager.py — VERIFICATION.md Tests 14, 20, 23.

Covers:
  normalise_phone:
    - 10-digit US number → +1NNNNNNNNNN
    - 11-digit 1XXXXXXXXXX → +1XXXXXXXXXX
    - already E.164 (+...) unchanged
    - strips dashes, spaces, parentheses
    - non-US-length passes through with any leading digits

  lookup_caller:
    - Redis cache HIT → returns (name, contact_id) without GHL call (Test 23)
    - Redis cache MISS → calls GHL, returns (name, contact_id), caches result
    - GHL returns no contact → returns (None, None)
    - Redis error is swallowed, still tries GHL
    - GHL error is swallowed, returns (None, None)

  sync_call_to_crm:
    - existing contact_id: calls update_contact, add_tags, add_note
    - no contact_id: calls create_contact, add_note
    - _build_tags queued to Redis db_sync_queue via rpush

  _build_tags:
    - lead_score ≥75 → "lead-hot"
    - lead_score ≥50 → "lead-warm"
    - lead_score <50 → "lead-cold"
    - EMERGENCY urgency → "urgency-emergency"
    - HIGH urgency → "urgency-high"
    - case_type present → "case-<type>" (sanitised)
    - language=="es" → "spanish-speaker"
    - "ivr-lead" always present

  _build_call_notes:
    - contains call_sid, score, urgency label

  _intake_to_custom:
    - maps known intake keys to GHL custom field names
    - skips None values
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
os.environ.setdefault("BASE_URL", "test.example.com")
os.environ.setdefault("GHL_API_KEY", "ghl-test")
os.environ.setdefault("GHL_LOCATION_ID", "loc-test")
os.environ.setdefault("ELEVENLABS_VOICE_ID_EN", "voice-en")
os.environ.setdefault("ELEVENLABS_VOICE_ID_ES", "voice-es")
os.environ.setdefault("GHL_CALENDAR_ID", "cal-test")
os.environ.setdefault("GOOGLE_CALENDAR_ID", "gcal-test")

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.crm.contact_manager import (
    _build_call_notes,
    _build_tags,
    _intake_to_custom,
    lookup_caller,
    normalise_phone,
    sync_call_to_crm,
)
from app.voice.conversation_state import CallState, UrgencyLabel


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_state(**kwargs) -> CallState:
    state = CallState(call_sid="CA_TEST_001")
    state.language = kwargs.get("language", "en")
    state.urgency_label = kwargs.get("urgency_label", UrgencyLabel.LOW)
    state.urgency_score = kwargs.get("urgency_score", 1)
    state.intake = kwargs.get("intake", {})
    state.scheduled_at = kwargs.get("scheduled_at", None)
    state.appointment_id = kwargs.get("appointment_id", None)
    state.transferred_at = kwargs.get("transferred_at", None)
    return state


def _make_redis(cached=None):
    redis = MagicMock()
    redis.get = AsyncMock(return_value=cached)
    redis.setex = AsyncMock()
    redis.rpush = AsyncMock()
    return redis


def _make_ghl(contact=None):
    ghl = MagicMock()
    ghl.search_contact_by_phone = AsyncMock(return_value=contact)
    ghl.update_contact = AsyncMock()
    ghl.add_tags = AsyncMock()
    ghl.add_note = AsyncMock()
    ghl.create_contact = AsyncMock(return_value={"id": "new-ghl-id"})
    return ghl


# ---------------------------------------------------------------------------
# normalise_phone
# ---------------------------------------------------------------------------

class TestNormalisePhone:
    def test_10_digit_us_adds_country_code(self):
        assert normalise_phone("5550001234") == "+15550001234"

    def test_11_digit_starting_1(self):
        assert normalise_phone("15550001234") == "+15550001234"

    def test_e164_unchanged(self):
        assert normalise_phone("+15550001234") == "+15550001234"

    def test_strips_dashes(self):
        assert normalise_phone("555-000-1234") == "+15550001234"

    def test_strips_spaces_and_parens(self):
        assert normalise_phone("(555) 000-1234") == "+15550001234"

    def test_strips_dots(self):
        assert normalise_phone("555.000.1234") == "+15550001234"

    def test_international_format_preserved(self):
        result = normalise_phone("+521234567890")
        assert result == "+521234567890"

    def test_empty_string_returns_empty(self):
        result = normalise_phone("")
        assert result == ""


# ---------------------------------------------------------------------------
# lookup_caller — Redis cache HIT (Test 23)
# ---------------------------------------------------------------------------

class TestLookupCallerCacheHit:
    async def test_returns_name_and_id_from_cache(self):
        cached_data = json.dumps({"name": "Ana Rivera", "contact_id": "ghl-ana"})
        redis = _make_redis(cached=cached_data)

        name, cid = await lookup_caller("+15550001234", redis)

        assert name == "Ana Rivera"
        assert cid == "ghl-ana"

    async def test_cache_hit_does_not_call_ghl(self):
        cached_data = json.dumps({"name": "Bob", "contact_id": "ghl-bob"})
        redis = _make_redis(cached=cached_data)

        with patch("app.crm.contact_manager.get_ghl_client") as mock_factory:
            await lookup_caller("+15550001234", redis)

        mock_factory.assert_not_called()

    async def test_cache_key_uses_normalised_phone(self):
        redis = _make_redis(cached=None)
        ghl = _make_ghl(contact=None)

        with patch("app.crm.contact_manager.get_ghl_client", return_value=ghl):
            await lookup_caller("5550001234", redis)

        called_key = redis.get.call_args[0][0]
        assert called_key == "ghl:phone:+15550001234"


# ---------------------------------------------------------------------------
# lookup_caller — Redis cache MISS, GHL hit (Test 14, 20)
# ---------------------------------------------------------------------------

class TestLookupCallerGhlHit:
    async def test_returns_name_and_id_from_ghl(self):
        redis = _make_redis(cached=None)
        ghl = _make_ghl(contact={
            "id": "ghl-999",
            "firstName": "Maria",
            "lastName": "Lopez",
        })

        with patch("app.crm.contact_manager.get_ghl_client", return_value=ghl):
            name, cid = await lookup_caller("+15550009999", redis)

        assert name == "Maria Lopez"
        assert cid == "ghl-999"

    async def test_caches_ghl_result_in_redis(self):
        redis = _make_redis(cached=None)
        ghl = _make_ghl(contact={"id": "ghl-777", "firstName": "Juan", "lastName": ""})

        with patch("app.crm.contact_manager.get_ghl_client", return_value=ghl):
            await lookup_caller("+15550007777", redis)

        redis.setex.assert_awaited_once()
        key_arg = redis.setex.call_args[0][0]
        assert key_arg == "ghl:phone:+15550007777"

    async def test_no_contact_found_returns_none_tuple(self):
        redis = _make_redis(cached=None)
        ghl = _make_ghl(contact=None)

        with patch("app.crm.contact_manager.get_ghl_client", return_value=ghl):
            name, cid = await lookup_caller("+15550008888", redis)

        assert name is None
        assert cid is None

    async def test_name_built_from_first_last(self):
        redis = _make_redis(cached=None)
        ghl = _make_ghl(contact={"id": "x", "firstName": "Carlos", "lastName": "Mendez"})

        with patch("app.crm.contact_manager.get_ghl_client", return_value=ghl):
            name, _ = await lookup_caller("+15550000001", redis)

        assert name == "Carlos Mendez"

    async def test_name_none_when_no_name_fields(self):
        redis = _make_redis(cached=None)
        ghl = _make_ghl(contact={"id": "x"})

        with patch("app.crm.contact_manager.get_ghl_client", return_value=ghl):
            name, _ = await lookup_caller("+15550000002", redis)

        assert name is None


# ---------------------------------------------------------------------------
# lookup_caller — error handling
# ---------------------------------------------------------------------------

class TestLookupCallerErrors:
    async def test_redis_error_swallowed_falls_back_to_ghl(self):
        redis = _make_redis()
        redis.get = AsyncMock(side_effect=ConnectionError("redis down"))
        ghl = _make_ghl(contact={"id": "ghl-fb", "firstName": "Fallback", "lastName": ""})

        with patch("app.crm.contact_manager.get_ghl_client", return_value=ghl):
            name, cid = await lookup_caller("+15550000099", redis)

        assert cid == "ghl-fb"

    async def test_ghl_error_swallowed_returns_none_tuple(self):
        redis = _make_redis(cached=None)
        ghl = MagicMock()
        ghl.search_contact_by_phone = AsyncMock(side_effect=RuntimeError("GHL down"))

        with patch("app.crm.contact_manager.get_ghl_client", return_value=ghl):
            name, cid = await lookup_caller("+15550000199", redis)

        assert name is None
        assert cid is None


# ---------------------------------------------------------------------------
# _build_tags
# ---------------------------------------------------------------------------

class TestBuildTags:
    def test_always_contains_ivr_lead(self):
        state = _make_state()
        tags = _build_tags(state, lead_score=10)
        assert "ivr-lead" in tags

    def test_hot_lead_score(self):
        state = _make_state()
        tags = _build_tags(state, lead_score=75)
        assert "lead-hot" in tags
        assert "lead-warm" not in tags

    def test_warm_lead_score(self):
        state = _make_state()
        tags = _build_tags(state, lead_score=50)
        assert "lead-warm" in tags

    def test_cold_lead_score(self):
        state = _make_state()
        tags = _build_tags(state, lead_score=49)
        assert "lead-cold" in tags

    def test_emergency_urgency_tag(self):
        state = _make_state(urgency_label=UrgencyLabel.EMERGENCY)
        tags = _build_tags(state, lead_score=0)
        assert "urgency-emergency" in tags

    def test_high_urgency_tag(self):
        state = _make_state(urgency_label=UrgencyLabel.HIGH)
        tags = _build_tags(state, lead_score=0)
        assert "urgency-high" in tags

    def test_no_urgency_tag_for_low(self):
        state = _make_state(urgency_label=UrgencyLabel.LOW)
        tags = _build_tags(state, lead_score=0)
        assert "urgency-low" not in tags
        assert "urgency-emergency" not in tags

    def test_case_type_tag_added(self):
        state = _make_state(intake={"case_type": "Asylum Application"})
        tags = _build_tags(state, lead_score=0)
        assert any(t.startswith("case-") for t in tags)

    def test_case_type_tag_sanitised(self):
        state = _make_state(intake={"case_type": "H-1B Visa"})
        tags = _build_tags(state, lead_score=0)
        case_tags = [t for t in tags if t.startswith("case-")]
        assert len(case_tags) == 1
        # only lowercase letters, digits and dashes
        import re
        assert re.match(r'^case-[a-z0-9-]+$', case_tags[0])

    def test_spanish_speaker_tag_when_es(self):
        state = _make_state(language="es")
        tags = _build_tags(state, lead_score=0)
        assert "spanish-speaker" in tags

    def test_no_spanish_tag_when_en(self):
        state = _make_state(language="en")
        tags = _build_tags(state, lead_score=0)
        assert "spanish-speaker" not in tags


# ---------------------------------------------------------------------------
# _build_call_notes
# ---------------------------------------------------------------------------

class TestBuildCallNotes:
    def test_contains_call_sid(self):
        state = _make_state()
        notes = _build_call_notes(state, lead_score=55)
        assert "CA_TEST_001" in notes

    def test_contains_lead_score(self):
        state = _make_state()
        notes = _build_call_notes(state, lead_score=72)
        assert "72" in notes

    def test_contains_urgency_label(self):
        state = _make_state(urgency_label=UrgencyLabel.HIGH)
        notes = _build_call_notes(state, lead_score=0)
        assert "high" in notes.lower()

    def test_intake_fields_listed(self):
        state = _make_state(intake={"full_name": "Test User", "case_type": "asylum"})
        notes = _build_call_notes(state, lead_score=0)
        assert "Test User" in notes
        assert "asylum" in notes

    def test_scheduled_at_shown_when_set(self):
        state = _make_state(scheduled_at="2025-09-01T10:00:00Z")
        notes = _build_call_notes(state, lead_score=0)
        assert "2025-09-01" in notes


# ---------------------------------------------------------------------------
# _intake_to_custom
# ---------------------------------------------------------------------------

class TestIntakeToCustom:
    def test_maps_known_field(self):
        out: dict = {}
        _intake_to_custom({"case_type": "asylum"}, out)
        assert out["case_type"] == "asylum"

    def test_maps_immigration_status(self):
        out: dict = {}
        _intake_to_custom({"current_immigration_status": "undocumented"}, out)
        assert out["immigration_status"] == "undocumented"

    def test_skips_none_values(self):
        out: dict = {}
        _intake_to_custom({"case_type": None}, out)
        assert "case_type" not in out

    def test_skips_unknown_intake_keys(self):
        out: dict = {}
        _intake_to_custom({"totally_unknown_field": "value"}, out)
        assert out == {}

    def test_multiple_fields_mapped(self):
        out: dict = {}
        _intake_to_custom({
            "case_type": "h1b",
            "country_of_birth": "Mexico",
            "entry_date_us": "2020-01-15",
        }, out)
        assert out["case_type"] == "h1b"
        assert out["country_of_birth"] == "Mexico"
        assert out["us_entry_date"] == "2020-01-15"


# ---------------------------------------------------------------------------
# sync_call_to_crm — existing contact path
# ---------------------------------------------------------------------------

class TestSyncCallToCrmExistingContact:
    async def test_calls_update_contact(self):
        state = _make_state(intake={"email": "test@example.com", "full_name": "Jane Doe"})
        redis = _make_redis()
        ghl = _make_ghl()

        with patch("app.crm.contact_manager.get_ghl_client", return_value=ghl):
            await sync_call_to_crm(state, ghl_contact_id="ghl-existing", lead_score=60, redis=redis)

        ghl.update_contact.assert_awaited_once()
        call_args = ghl.update_contact.call_args[0]
        assert call_args[0] == "ghl-existing"

    async def test_calls_add_tags(self):
        state = _make_state()
        redis = _make_redis()
        ghl = _make_ghl()

        with patch("app.crm.contact_manager.get_ghl_client", return_value=ghl):
            await sync_call_to_crm(state, ghl_contact_id="ghl-existing", lead_score=80, redis=redis)

        ghl.add_tags.assert_awaited_once()

    async def test_calls_add_note(self):
        state = _make_state()
        redis = _make_redis()
        ghl = _make_ghl()

        with patch("app.crm.contact_manager.get_ghl_client", return_value=ghl):
            await sync_call_to_crm(state, ghl_contact_id="ghl-existing", lead_score=0, redis=redis)

        ghl.add_note.assert_awaited_once()

    async def test_returns_existing_contact_id(self):
        state = _make_state()
        redis = _make_redis()
        ghl = _make_ghl()

        with patch("app.crm.contact_manager.get_ghl_client", return_value=ghl):
            result = await sync_call_to_crm(state, ghl_contact_id="ghl-existing", lead_score=0, redis=redis)

        assert result == "ghl-existing"

    async def test_queues_db_sync(self):
        state = _make_state()
        redis = _make_redis()
        ghl = _make_ghl()

        with patch("app.crm.contact_manager.get_ghl_client", return_value=ghl):
            await sync_call_to_crm(state, ghl_contact_id="ghl-existing", lead_score=50, redis=redis)

        redis.rpush.assert_awaited_once()
        queue_key = redis.rpush.call_args[0][0]
        assert queue_key == "db_sync_queue"


# ---------------------------------------------------------------------------
# sync_call_to_crm — new contact path (Test 20)
# ---------------------------------------------------------------------------

class TestSyncCallToCrmNewContact:
    async def test_calls_create_contact_when_no_existing_id(self):
        state = _make_state(intake={"full_name": "New Caller", "email": "new@example.com"})
        redis = _make_redis()
        ghl = _make_ghl()

        with patch("app.crm.contact_manager.get_ghl_client", return_value=ghl):
            result = await sync_call_to_crm(state, ghl_contact_id=None, lead_score=30, redis=redis)

        ghl.create_contact.assert_awaited_once()

    async def test_returns_new_ghl_id(self):
        state = _make_state()
        redis = _make_redis()
        ghl = _make_ghl()

        with patch("app.crm.contact_manager.get_ghl_client", return_value=ghl):
            result = await sync_call_to_crm(state, ghl_contact_id=None, lead_score=0, redis=redis)

        assert result == "new-ghl-id"
