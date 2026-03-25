"""
Unit tests for app/webhooks/twilio_webhooks.py.

Covers:
  - _validate_twilio_signature: HMAC-SHA1 logic
  - /twilio/voice: inbound call entry point
  - /twilio/status: call status callback
  - /twilio/recording: recording status callback
  - /twilio/voicemail: voicemail task dispatch
  - /twilio/ivr-menu: DTMF routing
  - /twilio/callback-request: enqueue callback
  - /twilio/callback-connect: outbound callback TwiML
  - /twilio/transfer-fallback: attorney no-answer TwiML
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

import base64
import hashlib
import hmac
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.webhooks.twilio_webhooks import _validate_twilio_signature, router


# ---------------------------------------------------------------------------
# TestClient setup
# ---------------------------------------------------------------------------

def _make_app() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False)


_TEST_TOKEN = "testtoken"


def _sign_twilio(url: str, params: dict, token: str = _TEST_TOKEN) -> str:
    """Compute Twilio HMAC-SHA1 signature for form-encoded params."""
    sorted_params = "".join(f"{k}{v}" for k, v in sorted(params.items()))
    string_to_sign = url + sorted_params
    mac = hmac.new(token.encode(), string_to_sign.encode(), hashlib.sha1)
    return base64.b64encode(mac.digest()).decode()


# ===========================================================================
# _validate_twilio_signature
# ===========================================================================

class TestValidateTwilioSignature:
    def test_valid_signature_returns_true(self):
        url = "https://example.com/twilio/voice"
        params = {"CallSid": "CA123", "From": "+15551234567"}
        sig = _sign_twilio(url, params)
        with patch("app.webhooks.twilio_webhooks.settings") as mock_settings:
            mock_settings.twilio_auth_token = _TEST_TOKEN
            result = _validate_twilio_signature(url, params, sig)
        assert result is True

    def test_wrong_token_returns_false(self):
        url = "https://example.com/twilio/voice"
        params = {"CallSid": "CA123"}
        sig = _sign_twilio(url, params, token="wrong-token")
        with patch("app.webhooks.twilio_webhooks.settings") as mock_settings:
            mock_settings.twilio_auth_token = _TEST_TOKEN
            result = _validate_twilio_signature(url, params, sig)
        assert result is False

    def test_empty_params_still_validates(self):
        url = "https://example.com/twilio/status"
        params = {}
        sig = _sign_twilio(url, params)
        with patch("app.webhooks.twilio_webhooks.settings") as mock_settings:
            mock_settings.twilio_auth_token = _TEST_TOKEN
            result = _validate_twilio_signature(url, params, sig)
        assert result is True

    def test_tampered_url_returns_false(self):
        url = "https://example.com/twilio/voice"
        params = {"CallSid": "CA123"}
        sig = _sign_twilio(url, params)
        # Change the URL after signing
        with patch("app.webhooks.twilio_webhooks.settings") as mock_settings:
            mock_settings.twilio_auth_token = _TEST_TOKEN
            result = _validate_twilio_signature("https://attacker.com/twilio/voice", params, sig)
        assert result is False

    def test_tampered_params_returns_false(self):
        url = "https://example.com/twilio/voice"
        params = {"CallSid": "CA123"}
        sig = _sign_twilio(url, params)
        tampered = {"CallSid": "CA999"}
        with patch("app.webhooks.twilio_webhooks.settings") as mock_settings:
            mock_settings.twilio_auth_token = _TEST_TOKEN
            result = _validate_twilio_signature(url, tampered, sig)
        assert result is False


# ===========================================================================
# POST /twilio/voice — inbound call entry
# ===========================================================================

class TestInboundVoice:
    def test_returns_xml_response(self):
        client = _make_app()
        base_url = "http://testserver"
        params = {
            "CallSid": "CA123",
            "From": "+15551234567",
            "To": "+10000000000",
        }
        sig = _sign_twilio(f"{base_url}/twilio/voice", params)

        with (
            patch("app.webhooks.twilio_webhooks.route_inbound_call", return_value="<Response><Say>Hi</Say></Response>"),
            patch("app.webhooks.twilio_webhooks._accepting_connections", True, create=True),
        ):
            response = client.post(
                "/twilio/voice",
                data=params,
                headers={"X-Twilio-Signature": sig},
            )

        assert response.status_code == 200
        assert "xml" in response.headers["content-type"].lower() or "<Response>" in response.text or "<Say>" in response.text

    def test_rejects_bad_signature_with_403(self):
        client = _make_app()
        params = {"CallSid": "CA123", "From": "+15551234567"}

        with patch("app.webhooks.twilio_webhooks._accepting_connections", True, create=True):
            response = client.post(
                "/twilio/voice",
                data=params,
                headers={"X-Twilio-Signature": "invalid-sig"},
            )

        assert response.status_code == 403


# ===========================================================================
# POST /twilio/status — call status callback
# ===========================================================================

class TestCallStatusCallback:
    def _make_redis_mock(self):
        mock_pipe = MagicMock()
        mock_pipe.execute = AsyncMock(return_value=[])
        mock_pipe.__aenter__ = AsyncMock(return_value=mock_pipe)
        mock_pipe.__aexit__ = AsyncMock(return_value=False)
        mock_redis = AsyncMock()
        mock_redis.pipeline = MagicMock(return_value=mock_pipe)
        return mock_redis

    def test_returns_ok_true(self):
        client = _make_app()
        base_url = "http://testserver"
        params = {
            "CallSid": "CA123",
            "CallStatus": "completed",
            "CallDuration": "45",
            "From": "+15551234567",
        }
        sig = _sign_twilio(f"{base_url}/twilio/status", params)
        mock_redis = self._make_redis_mock()

        with patch("app.webhooks.twilio_webhooks.get_redis_client", return_value=mock_redis):
            response = client.post(
                "/twilio/status",
                data=params,
                headers={"X-Twilio-Signature": sig},
            )

        assert response.status_code == 200
        assert response.json() == {"ok": True}

    def test_queues_callback_for_no_answer(self):
        client = _make_app()
        base_url = "http://testserver"
        params = {
            "CallSid": "CA_NOANSWER",
            "CallStatus": "no-answer",
            "CallDuration": "0",
            "From": "+15559999999",
        }
        sig = _sign_twilio(f"{base_url}/twilio/status", params)
        mock_redis = self._make_redis_mock()

        with patch("app.webhooks.twilio_webhooks.get_redis_client", return_value=mock_redis):
            response = client.post(
                "/twilio/status",
                data=params,
                headers={"X-Twilio-Signature": sig},
            )

        assert response.status_code == 200
        # rpush to callback_queue was called
        mock_redis.rpush.assert_called_once()


# ===========================================================================
# POST /twilio/recording — recording available
# ===========================================================================

class TestRecordingStatusCallback:
    def test_stores_url_when_completed(self):
        client = _make_app()
        base_url = "http://testserver"
        params = {
            "CallSid": "CA123",
            "RecordingUrl": "https://api.twilio.com/recordings/RE123",
            "RecordingStatus": "completed",
        }
        sig = _sign_twilio(f"{base_url}/twilio/recording", params)

        mock_redis = AsyncMock()
        with patch("app.webhooks.twilio_webhooks.get_redis_client", return_value=mock_redis):
            response = client.post(
                "/twilio/recording",
                data=params,
                headers={"X-Twilio-Signature": sig},
            )

        assert response.status_code == 200
        mock_redis.hset.assert_called_once()

    def test_skips_storage_when_not_completed(self):
        client = _make_app()
        base_url = "http://testserver"
        params = {
            "CallSid": "CA123",
            "RecordingUrl": "https://api.twilio.com/recordings/RE123",
            "RecordingStatus": "in-progress",
        }
        sig = _sign_twilio(f"{base_url}/twilio/recording", params)

        mock_redis = AsyncMock()
        with patch("app.webhooks.twilio_webhooks.get_redis_client", return_value=mock_redis):
            response = client.post(
                "/twilio/recording",
                data=params,
                headers={"X-Twilio-Signature": sig},
            )

        assert response.status_code == 200
        mock_redis.hset.assert_not_called()

    def test_returns_ok_true(self):
        client = _make_app()
        base_url = "http://testserver"
        params = {
            "CallSid": "CA123",
            "RecordingUrl": "",
            "RecordingStatus": "failed",
        }
        sig = _sign_twilio(f"{base_url}/twilio/recording", params)

        mock_redis = AsyncMock()
        with patch("app.webhooks.twilio_webhooks.get_redis_client", return_value=mock_redis):
            response = client.post(
                "/twilio/recording",
                data=params,
                headers={"X-Twilio-Signature": sig},
            )

        assert response.json() == {"ok": True}


# ===========================================================================
# POST /twilio/voicemail — voicemail recording complete
# ===========================================================================

class TestVoicemailCallback:
    def test_returns_xml_thanks_message(self):
        client = _make_app()
        base_url = "http://testserver"
        params = {
            "CallSid": "CA123",
            "From": "+15551234567",
            "RecordingUrl": "https://api.twilio.com/recordings/RE123",
            "RecordingDuration": "15",
            "RecordingSid": "RE123",
        }
        sig = _sign_twilio(f"{base_url}/twilio/voicemail", params)

        with patch("asyncio.create_task"):
            response = client.post(
                "/twilio/voicemail",
                data=params,
                headers={"X-Twilio-Signature": sig},
            )

        assert response.status_code == 200

    def test_skips_task_when_zero_duration(self):
        client = _make_app()
        base_url = "http://testserver"
        params = {
            "CallSid": "CA123",
            "From": "+15551234567",
            "RecordingUrl": "https://api.twilio.com/recordings/RE123",
            "RecordingDuration": "0",
            "RecordingSid": "RE123",
        }
        sig = _sign_twilio(f"{base_url}/twilio/voicemail", params)

        with patch("asyncio.create_task") as mock_create_task:
            response = client.post(
                "/twilio/voicemail",
                data=params,
                headers={"X-Twilio-Signature": sig},
            )

        # Zero-duration recording should not trigger voicemail processing
        mock_create_task.assert_not_called()
        assert response.status_code == 200


# ===========================================================================
# POST /twilio/ivr-menu — DTMF routing
# ===========================================================================

class TestIvrMenu:
    def test_routes_to_digit_handler_when_digit_present(self):
        client = _make_app()
        base_url = "http://testserver"
        params = {"Digits": "1", "CallSid": "CA123"}
        sig = _sign_twilio(f"{base_url}/twilio/ivr-menu", params)

        with patch("app.telephony.twiml_responses.twiml_ivr_digit", return_value="<Response><Say>digit</Say></Response>") as mock_digit:
            response = client.post(
                "/twilio/ivr-menu",
                data=params,
                headers={"X-Twilio-Signature": sig},
            )

        assert response.status_code == 200
        mock_digit.assert_called_once()

    def test_returns_menu_when_no_digit(self):
        client = _make_app()
        base_url = "http://testserver"
        params = {"Digits": "", "CallSid": "CA123"}
        sig = _sign_twilio(f"{base_url}/twilio/ivr-menu", params)

        with patch("app.telephony.twiml_responses.twiml_ivr_menu", return_value="<Response><Gather></Gather></Response>") as mock_menu:
            response = client.post(
                "/twilio/ivr-menu",
                data=params,
                headers={"X-Twilio-Signature": sig},
            )

        assert response.status_code == 200
        mock_menu.assert_called_once()


# ===========================================================================
# POST /twilio/callback-request — caller wants callback
# ===========================================================================

class TestCallbackRequest:
    def test_enqueues_callback_for_digit_2(self):
        client = _make_app()
        base_url = "http://testserver"
        params = {
            "Digits": "2",
            "From": "+15551234567",
            "CallSid": "CA123",
        }
        sig = _sign_twilio(f"{base_url}/twilio/callback-request", params)

        mock_redis = AsyncMock()
        with (
            patch("app.webhooks.twilio_webhooks.get_redis_client", return_value=mock_redis),
            patch("app.telephony.outbound_callback.enqueue_callback", new_callable=AsyncMock) as mock_eq,
        ):
            response = client.post(
                "/twilio/callback-request",
                data=params,
                headers={"X-Twilio-Signature": sig},
            )

        assert response.status_code == 200
        mock_eq.assert_called_once()

    def test_returns_twiml_for_digit_1(self):
        client = _make_app()
        base_url = "http://testserver"
        params = {
            "Digits": "1",
            "From": "+15551234567",
            "CallSid": "CA123",
        }
        sig = _sign_twilio(f"{base_url}/twilio/callback-request", params)

        with patch("app.telephony.twiml_responses.twiml_voicemail", return_value="<Response/>" ):
            response = client.post(
                "/twilio/callback-request",
                data=params,
                headers={"X-Twilio-Signature": sig},
            )

        assert response.status_code == 200


# ===========================================================================
# POST /twilio/callback-connect — outbound callback answered
# ===========================================================================

class TestCallbackConnect:
    def test_returns_ai_stream_twiml(self):
        client = _make_app()
        base_url = "http://testserver"
        params = {"CallSid": "CA_CB_123"}
        sig = _sign_twilio(f"{base_url}/twilio/callback-connect", params)

        with patch("app.telephony.twiml_responses.twiml_ai_stream", return_value="<Response><Connect/></Response>") as mock_stream:
            response = client.post(
                "/twilio/callback-connect",
                data=params,
                headers={"X-Twilio-Signature": sig},
            )

        assert response.status_code == 200
        mock_stream.assert_called_once()


# ===========================================================================
# POST /twilio/transfer-fallback — attorney didn't answer
# ===========================================================================

class TestTransferFallback:
    def test_returns_transfer_no_answer_twiml(self):
        client = _make_app()
        base_url = "http://testserver"
        params = {"CallSid": "CA123"}
        sig = _sign_twilio(f"{base_url}/twilio/transfer-fallback", params)

        response = client.post(
            "/twilio/transfer-fallback",
            data=params,
            headers={"X-Twilio-Signature": sig},
        )

        assert response.status_code == 200
        assert "unavailable" in response.text.lower() or "<Record" in response.text

    def test_spanish_fallback_via_query_param(self):
        client = _make_app()
        base_url = "http://testserver"
        params = {"CallSid": "CA123"}
        # Query params are part of the URL for signature computation
        sig = _sign_twilio(f"{base_url}/twilio/transfer-fallback?lang=es", params)

        response = client.post(
            "/twilio/transfer-fallback?lang=es",
            data=params,
            headers={"X-Twilio-Signature": sig},
        )

        assert response.status_code == 200
        assert "abogado" in response.text.lower()
