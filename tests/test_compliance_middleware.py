"""
Unit tests for compliance & consent enforcement — VERIFICATION.md Tests 28-29.

Covers:
  - _get_client_ip: X-Forwarded-For leftmost IP, fallback to request.client.host
  - twiml_recording_consent: returns valid TwiML with <Say> tag (call_router.py)
  - _check_sms_consent: yes/true/1/opt-in → True; no/missing → False; no contact → False
  - send_confirmation_sms: short-circuits when no SMS consent (no API call made)
  - AuditLogMiddleware.dispatch: skips GET requests, skips _SKIP_PATHS, logs POST
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

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.datastructures import Headers
from starlette.requests import Request


# ---------------------------------------------------------------------------
# _get_client_ip unit tests
# ---------------------------------------------------------------------------

class TestGetClientIp:
    """Tests for the _get_client_ip helper in compliance/middleware.py."""

    def _make_request(self, headers: dict, client_host: str | None = "10.0.0.1"):
        """Minimal mock of a Starlette Request with given headers and client."""
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/test",
            "query_string": b"",
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        }
        if client_host:
            scope["client"] = (client_host, 9999)
        request = Request(scope)
        return request

    def test_x_forwarded_for_single_ip(self):
        from app.compliance.middleware import _get_client_ip
        req = self._make_request({"x-forwarded-for": "1.2.3.4"})
        assert _get_client_ip(req) == "1.2.3.4"

    def test_x_forwarded_for_chain_returns_leftmost(self):
        from app.compliance.middleware import _get_client_ip
        req = self._make_request({"x-forwarded-for": "1.2.3.4, 10.0.0.1, 192.168.1.1"})
        assert _get_client_ip(req) == "1.2.3.4"

    def test_x_forwarded_for_with_spaces(self):
        from app.compliance.middleware import _get_client_ip
        req = self._make_request({"x-forwarded-for": "  5.6.7.8 , 9.10.11.12"})
        assert _get_client_ip(req) == "5.6.7.8"

    def test_no_forwarded_header_uses_client_host(self):
        from app.compliance.middleware import _get_client_ip
        req = self._make_request({}, client_host="203.0.113.42")
        assert _get_client_ip(req) == "203.0.113.42"

    def test_no_client_and_no_forwarded_returns_unknown(self):
        from app.compliance.middleware import _get_client_ip
        req = self._make_request({}, client_host=None)
        assert _get_client_ip(req) == "unknown"


# ---------------------------------------------------------------------------
# twiml_recording_consent unit tests
# ---------------------------------------------------------------------------

class TestTwimlRecordingConsent:
    """twiml_recording_consent() is a pure string builder — no mocking needed."""

    def test_returns_string(self):
        from app.telephony.call_router import twiml_recording_consent
        result = twiml_recording_consent()
        assert isinstance(result, str)

    def test_contains_xml_declaration(self):
        from app.telephony.call_router import twiml_recording_consent
        result = twiml_recording_consent()
        assert '<?xml version="1.0"' in result

    def test_contains_say_tag(self):
        from app.telephony.call_router import twiml_recording_consent
        result = twiml_recording_consent()
        assert "<Say" in result
        assert "</Say>" in result

    def test_contains_recording_notice_text(self):
        from app.telephony.call_router import twiml_recording_consent
        result = twiml_recording_consent()
        # Must inform caller that the call may be recorded
        assert "recorded" in result.lower()

    def test_contains_response_root_element(self):
        from app.telephony.call_router import twiml_recording_consent
        result = twiml_recording_consent()
        assert "<Response>" in result
        assert "</Response>" in result


# ---------------------------------------------------------------------------
# _check_sms_consent unit tests
# ---------------------------------------------------------------------------

class TestCheckSmsConsent:
    """Tests that _check_sms_consent correctly reads GHL customField."""

    def _mock_ghl_with_consent(self, value: str):
        mock = MagicMock()
        mock.get_contact = AsyncMock(return_value={
            "customField": {"sms_consent": value}
        })
        return mock

    async def test_consent_yes_returns_true(self):
        from app.scheduling.reminders import _check_sms_consent
        mock_ghl = self._mock_ghl_with_consent("yes")
        with patch("app.scheduling.reminders.get_ghl_client", return_value=mock_ghl):
            result = await _check_sms_consent("contact-001")
        assert result is True

    async def test_consent_true_returns_true(self):
        from app.scheduling.reminders import _check_sms_consent
        mock_ghl = self._mock_ghl_with_consent("true")
        with patch("app.scheduling.reminders.get_ghl_client", return_value=mock_ghl):
            result = await _check_sms_consent("contact-002")
        assert result is True

    async def test_consent_one_returns_true(self):
        from app.scheduling.reminders import _check_sms_consent
        mock_ghl = self._mock_ghl_with_consent("1")
        with patch("app.scheduling.reminders.get_ghl_client", return_value=mock_ghl):
            result = await _check_sms_consent("contact-003")
        assert result is True

    async def test_consent_opt_in_returns_true(self):
        from app.scheduling.reminders import _check_sms_consent
        mock_ghl = self._mock_ghl_with_consent("opt-in")
        with patch("app.scheduling.reminders.get_ghl_client", return_value=mock_ghl):
            result = await _check_sms_consent("contact-004")
        assert result is True

    async def test_consent_case_insensitive_YES(self):
        from app.scheduling.reminders import _check_sms_consent
        mock_ghl = self._mock_ghl_with_consent("YES")
        with patch("app.scheduling.reminders.get_ghl_client", return_value=mock_ghl):
            result = await _check_sms_consent("contact-005")
        assert result is True

    async def test_consent_no_returns_false(self):
        from app.scheduling.reminders import _check_sms_consent
        mock_ghl = self._mock_ghl_with_consent("no")
        with patch("app.scheduling.reminders.get_ghl_client", return_value=mock_ghl):
            result = await _check_sms_consent("contact-006")
        assert result is False

    async def test_consent_missing_field_returns_false(self):
        from app.scheduling.reminders import _check_sms_consent
        mock_ghl = MagicMock()
        mock_ghl.get_contact = AsyncMock(return_value={"customField": {}})
        with patch("app.scheduling.reminders.get_ghl_client", return_value=mock_ghl):
            result = await _check_sms_consent("contact-007")
        assert result is False

    async def test_contact_not_found_returns_false(self):
        from app.scheduling.reminders import _check_sms_consent
        mock_ghl = MagicMock()
        mock_ghl.get_contact = AsyncMock(return_value=None)
        with patch("app.scheduling.reminders.get_ghl_client", return_value=mock_ghl):
            result = await _check_sms_consent("contact-not-found")
        assert result is False

    async def test_empty_customField_returns_false(self):
        from app.scheduling.reminders import _check_sms_consent
        mock_ghl = MagicMock()
        mock_ghl.get_contact = AsyncMock(return_value={})
        with patch("app.scheduling.reminders.get_ghl_client", return_value=mock_ghl):
            result = await _check_sms_consent("contact-008")
        assert result is False


# ---------------------------------------------------------------------------
# send_confirmation_sms — consent gate unit tests
# ---------------------------------------------------------------------------

class TestSendConfirmationSmsConsentGate:
    """verify send_confirmation_sms short-circuits and returns False when no consent."""

    async def test_returns_false_when_no_consent(self):
        from app.scheduling.reminders import send_confirmation_sms
        with patch(
            "app.scheduling.reminders._check_sms_consent",
            new_callable=AsyncMock,
            return_value=False,
        ):
            with patch(
                "app.scheduling.reminders.get_ghl_client"
            ) as mock_ghl_factory:
                result = await send_confirmation_sms(
                    contact_id="contact-009",
                    appointment_datetime_iso="2025-08-01T14:00:00Z",
                )
        assert result is False
        # GHL send_sms must NOT have been called
        mock_ghl_factory.assert_not_called()

    async def test_consent_check_called_with_contact_id(self):
        from app.scheduling.reminders import send_confirmation_sms
        with patch(
            "app.scheduling.reminders._check_sms_consent",
            new_callable=AsyncMock,
            return_value=False,
        ) as mock_check:
            await send_confirmation_sms(
                contact_id="contact-010",
                appointment_datetime_iso="2025-08-01T14:00:00Z",
            )
        mock_check.assert_awaited_once_with("contact-010")


# ---------------------------------------------------------------------------
# AuditLogMiddleware dispatch logic
# ---------------------------------------------------------------------------

class TestAuditLogMiddlewareDispatch:
    """Whitebox tests that _enqueue is called only for mutating non-skip requests."""

    def _make_app_with_middleware(self):
        app = FastAPI()

        @app.get("/api/status")
        def status():
            return {"status": "ok"}

        @app.post("/api/calls")
        def calls():
            return {"status": "ok"}

        @app.post("/health")
        def health():
            return {"status": "ok"}

        @app.post("/metrics")
        def metrics():
            return {"status": "ok"}

        from app.compliance.middleware import AuditLogMiddleware
        app.add_middleware(AuditLogMiddleware)
        return app

    def test_get_request_does_not_enqueue(self):
        from app.compliance.middleware import AuditLogMiddleware
        app = self._make_app_with_middleware()
        with patch.object(
            AuditLogMiddleware, "_enqueue", new_callable=AsyncMock
        ) as mock_enqueue:
            client = TestClient(app)
            client.get("/api/status")
        mock_enqueue.assert_not_called()

    def test_health_path_post_does_not_enqueue(self):
        from app.compliance.middleware import AuditLogMiddleware
        app = self._make_app_with_middleware()
        with patch.object(
            AuditLogMiddleware, "_enqueue", new_callable=AsyncMock
        ) as mock_enqueue:
            client = TestClient(app)
            client.post("/health")
        mock_enqueue.assert_not_called()

    def test_metrics_path_post_does_not_enqueue(self):
        from app.compliance.middleware import AuditLogMiddleware
        app = self._make_app_with_middleware()
        with patch.object(
            AuditLogMiddleware, "_enqueue", new_callable=AsyncMock
        ) as mock_enqueue:
            client = TestClient(app)
            client.post("/metrics")
        mock_enqueue.assert_not_called()

    def test_regular_post_does_enqueue(self):
        from app.compliance.middleware import AuditLogMiddleware
        app = self._make_app_with_middleware()
        with patch.object(
            AuditLogMiddleware, "_enqueue", new_callable=AsyncMock
        ) as mock_enqueue:
            client = TestClient(app)
            client.post("/api/calls")
        mock_enqueue.assert_called_once()

    def test_enqueue_called_with_correct_status_code(self):
        from app.compliance.middleware import AuditLogMiddleware
        app = self._make_app_with_middleware()
        with patch.object(
            AuditLogMiddleware, "_enqueue", new_callable=AsyncMock
        ) as mock_enqueue:
            client = TestClient(app)
            client.post("/api/calls")
        # dispatch(request, response.status_code, duration_ms)
        call_kwargs = mock_enqueue.call_args
        status_arg = call_kwargs[0][1]  # positional arg index 1 = status_code
        assert status_arg == 200
