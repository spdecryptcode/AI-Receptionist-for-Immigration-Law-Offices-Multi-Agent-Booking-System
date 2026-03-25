"""
Unit tests for GHL webhook signature validation — VERIFICATION.md Test 22.

Tests cover:
  - _validate_ghl_signature: valid signature → True
  - _validate_ghl_signature: wrong secret → False
  - _validate_ghl_signature: missing sha256= prefix → False
  - _validate_ghl_signature: empty header → False
  - _validate_ghl_signature: no secret configured → True (dev-mode bypass)
  - FastAPI endpoint: valid signature → 200
  - FastAPI endpoint: invalid signature → 403
  - FastAPI endpoint: missing signature header → 403
  - FastAPI endpoint: valid contact.created event syncs to Redis
  - FastAPI endpoint: valid appointment event enqueues to Redis

Note: VERIFICATION.md Test 22 says 401 but the code returns 403 FORBIDDEN
(correct HTTP semantics — 401 is for authentication, not HMAC integrity).
Tests match the implementation.
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

import hashlib
import hmac
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.webhooks.ghl_webhooks import _validate_ghl_signature, router


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TEST_SECRET = "super-secret-webhook-key"


def _sign(body: bytes, secret: str = _TEST_SECRET) -> str:
    """Produce a valid sha256= HMAC header value."""
    digest = hmac.new(
        secret.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()
    return f"sha256={digest}"


def _make_app() -> TestClient:
    """Minimal FastAPI app with just the GHL webhook router mounted."""
    app = FastAPI()
    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# _validate_ghl_signature unit tests
# ---------------------------------------------------------------------------

class TestValidateGhlSignature:
    def test_valid_signature_returns_true(self):
        body = b'{"type":"contact.created"}'
        sig = _sign(body)
        with patch("app.webhooks.ghl_webhooks.settings") as mock_settings:
            mock_settings.ghl_webhook_secret = _TEST_SECRET
            assert _validate_ghl_signature(body, sig) is True

    def test_wrong_secret_returns_false(self):
        body = b'{"type":"contact.created"}'
        sig = _sign(body, secret="wrong-secret")
        with patch("app.webhooks.ghl_webhooks.settings") as mock_settings:
            mock_settings.ghl_webhook_secret = _TEST_SECRET
            assert _validate_ghl_signature(body, sig) is False

    def test_missing_sha256_prefix_returns_false(self):
        body = b'{"type":"contact.created"}'
        bare_hex = hmac.new(
            _TEST_SECRET.encode(), body, hashlib.sha256
        ).hexdigest()
        with patch("app.webhooks.ghl_webhooks.settings") as mock_settings:
            mock_settings.ghl_webhook_secret = _TEST_SECRET
            assert _validate_ghl_signature(body, bare_hex) is False

    def test_empty_header_returns_false(self):
        body = b'{"type":"contact.created"}'
        with patch("app.webhooks.ghl_webhooks.settings") as mock_settings:
            mock_settings.ghl_webhook_secret = _TEST_SECRET
            assert _validate_ghl_signature(body, "") is False

    def test_no_secret_configured_bypasses_validation(self):
        """Dev mode: empty secret → skip validation → return True."""
        body = b'{"type":"contact.created"}'
        with patch("app.webhooks.ghl_webhooks.settings") as mock_settings:
            mock_settings.ghl_webhook_secret = ""
            result = _validate_ghl_signature(body, "sha256=anything")
        assert result is True

    def test_tampered_body_returns_false(self):
        body = b'{"type":"contact.created"}'
        sig = _sign(body)
        tampered = b'{"type":"contact.deleted"}'
        with patch("app.webhooks.ghl_webhooks.settings") as mock_settings:
            mock_settings.ghl_webhook_secret = _TEST_SECRET
            assert _validate_ghl_signature(tampered, sig) is False

    def test_constant_time_comparison_used(self):
        """Just verify hmac.compare_digest is called — guards against timing attacks."""
        body = b'test'
        sig = _sign(body)
        with patch("app.webhooks.ghl_webhooks.settings") as mock_settings:
            mock_settings.ghl_webhook_secret = _TEST_SECRET
            with patch("app.webhooks.ghl_webhooks.hmac.compare_digest",
                       wraps=hmac.compare_digest) as mock_compare:
                _validate_ghl_signature(body, sig)
            mock_compare.assert_called_once()


# ---------------------------------------------------------------------------
# FastAPI endpoint integration tests
# ---------------------------------------------------------------------------

class TestGhlWebhookEndpoint:
    def _client_with_redis_mock(self):
        """Return (TestClient, redis_mock) with get_redis_client patched."""
        redis_mock = AsyncMock()
        redis_mock.hset = AsyncMock()
        redis_mock.rpush = AsyncMock()
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app, raise_server_exceptions=False)
        return client, redis_mock

    def test_valid_signature_returns_200(self):
        client, redis_mock = self._client_with_redis_mock()
        body = json.dumps({"type": "unknown.event"}).encode()
        sig = _sign(body)
        with patch("app.webhooks.ghl_webhooks.settings") as ms:
            ms.ghl_webhook_secret = _TEST_SECRET
            with patch("app.webhooks.ghl_webhooks.get_redis_client",
                       return_value=redis_mock):
                resp = client.post(
                    "/ghl/webhook",
                    content=body,
                    headers={"X-GHL-Signature": sig,
                             "Content-Type": "application/json"},
                )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    def test_invalid_signature_returns_403(self):
        client, redis_mock = self._client_with_redis_mock()
        body = json.dumps({"type": "contact.created"}).encode()
        with patch("app.webhooks.ghl_webhooks.settings") as ms:
            ms.ghl_webhook_secret = _TEST_SECRET
            resp = client.post(
                "/ghl/webhook",
                content=body,
                headers={"X-GHL-Signature": "sha256=badhex",
                         "Content-Type": "application/json"},
            )
        assert resp.status_code == 403

    def test_missing_signature_header_returns_403(self):
        client, redis_mock = self._client_with_redis_mock()
        body = json.dumps({"type": "contact.created"}).encode()
        with patch("app.webhooks.ghl_webhooks.settings") as ms:
            ms.ghl_webhook_secret = _TEST_SECRET
            resp = client.post(
                "/ghl/webhook",
                content=body,
                headers={"Content-Type": "application/json"},
            )
        assert resp.status_code == 403

    def test_contact_created_event_syncs_to_redis(self):
        client, redis_mock = self._client_with_redis_mock()
        payload = {
            "type": "contact.created",
            "contact": {"id": "ghl-123", "phone": "+15550001234"},
        }
        body = json.dumps(payload).encode()
        sig = _sign(body)
        with patch("app.webhooks.ghl_webhooks.settings") as ms:
            ms.ghl_webhook_secret = _TEST_SECRET
            with patch("app.webhooks.ghl_webhooks.get_redis_client",
                       return_value=redis_mock):
                resp = client.post(
                    "/ghl/webhook",
                    content=body,
                    headers={"X-GHL-Signature": sig,
                             "Content-Type": "application/json"},
                )
        assert resp.status_code == 200
        redis_mock.hset.assert_called_once_with(
            "ghl:contacts", "+15550001234", "ghl-123"
        )

    def test_appointment_created_event_enqueues(self):
        client, redis_mock = self._client_with_redis_mock()
        payload = {
            "type": "appointment.created",
            "appointment": {"id": "appt-456", "title": "Consult"},
        }
        body = json.dumps(payload).encode()
        sig = _sign(body)
        with patch("app.webhooks.ghl_webhooks.settings") as ms:
            ms.ghl_webhook_secret = _TEST_SECRET
            with patch("app.webhooks.ghl_webhooks.get_redis_client",
                       return_value=redis_mock):
                resp = client.post(
                    "/ghl/webhook",
                    content=body,
                    headers={"X-GHL-Signature": sig,
                             "Content-Type": "application/json"},
                )
        assert resp.status_code == 200
        redis_mock.rpush.assert_called_once()
        queue_key = redis_mock.rpush.call_args[0][0]
        assert queue_key == "ghl:appointment_events"

    def test_invalid_json_returns_400(self):
        client, redis_mock = self._client_with_redis_mock()
        body = b"not-json"
        sig = _sign(body)
        with patch("app.webhooks.ghl_webhooks.settings") as ms:
            ms.ghl_webhook_secret = _TEST_SECRET
            with patch("app.webhooks.ghl_webhooks.get_redis_client",
                       return_value=redis_mock):
                resp = client.post(
                    "/ghl/webhook",
                    content=body,
                    headers={"X-GHL-Signature": sig,
                             "Content-Type": "application/json"},
                )
        assert resp.status_code == 400
