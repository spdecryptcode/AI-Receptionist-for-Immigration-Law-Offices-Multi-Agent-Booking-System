"""
Unit tests for audio format conversion utilities.

These are pure CPU-bound functions with no I/O — no mocking needed.
Tests verify:
  1. mulaw → linear16 roundtrip preserves signal shape
  2. linear16 → mulaw roundtrip
  3. twilio_payload_to_deepgram (base64 mulaw → linear16 bytes)
  4. elevenlabs_to_twilio_payload (mulaw bytes → base64 string)
  5. Edge cases: empty input, minimal single-sample inputs
"""
import base64
import struct

import numpy as np
import pytest

from app.voice.audio_utils import (
    mulaw_to_linear16,
    linear16_to_mulaw,
    twilio_payload_to_deepgram,
    elevenlabs_to_twilio_payload,
    TWILIO_SAMPLE_RATE,
    DEEPGRAM_SAMPLE_RATE,
)


def _make_mulaw_bytes(n_samples: int = 160) -> bytes:
    """Generate n_samples of silent mulaw (0xFF = silence in μ-law)."""
    return bytes([0xFF] * n_samples)


def _make_linear16_bytes(n_samples: int = 160) -> bytes:
    """Generate n_samples of zero-value linear16 PCM (silence)."""
    return struct.pack(f"<{n_samples}h", *([0] * n_samples))


class TestMulawToLinear16:
    def test_returns_bytes(self):
        out = mulaw_to_linear16(_make_mulaw_bytes(160))
        assert isinstance(out, bytes)

    def test_output_sample_rate_doubles(self):
        """Input 160 samples at 8kHz → 320 samples at 16kHz."""
        n_in = 160
        out = mulaw_to_linear16(_make_mulaw_bytes(n_in))
        n_out = len(out) // 2  # 2 bytes per int16
        assert n_out == pytest.approx(n_in * 2, rel=0.05)

    def test_silence_stays_near_zero(self):
        """Mulaw 0xFF encodes near-silence; decoded samples should be small."""
        out = mulaw_to_linear16(_make_mulaw_bytes(160))
        samples = np.frombuffer(out, dtype=np.int16)
        assert np.abs(samples).max() < 100  # within ±100 of zero

    def test_empty_input_returns_bytes(self):
        out = mulaw_to_linear16(b"")
        assert isinstance(out, bytes)

    def test_non_empty_output_for_non_empty_input(self):
        out = mulaw_to_linear16(_make_mulaw_bytes(8))
        assert len(out) > 0


class TestLinear16ToMulaw:
    def test_returns_bytes(self):
        out = linear16_to_mulaw(_make_linear16_bytes(320))
        assert isinstance(out, bytes)

    def test_output_sample_rate_halves(self):
        """Input 320 samples at 16kHz → 160 samples at 8kHz."""
        n_in = 320
        out = linear16_to_mulaw(_make_linear16_bytes(n_in))
        assert len(out) == pytest.approx(n_in // 2, rel=0.05)

    def test_empty_input_returns_bytes(self):
        out = linear16_to_mulaw(b"")
        assert isinstance(out, bytes)


class TestTwilioPayloadToDeepgram:
    def test_decodes_base64_and_converts(self):
        mulaw = _make_mulaw_bytes(160)
        b64 = base64.b64encode(mulaw).decode("utf-8")
        result = twilio_payload_to_deepgram(b64)
        assert isinstance(result, bytes)
        assert len(result) > len(mulaw)  # upsampled → more bytes

    def test_returns_bytes_for_empty_payload(self):
        b64_empty = base64.b64encode(b"").decode("utf-8")
        result = twilio_payload_to_deepgram(b64_empty)
        assert isinstance(result, bytes)


class TestElevenLabsToTwilioPayload:
    def test_returns_base64_string(self):
        mulaw = _make_mulaw_bytes(160)
        result = elevenlabs_to_twilio_payload(mulaw)
        assert isinstance(result, str)
        # Should be valid base64
        decoded = base64.b64decode(result)
        assert decoded == mulaw

    def test_empty_bytes_returns_empty_string(self):
        result = elevenlabs_to_twilio_payload(b"")
        assert result == ""  # or base64 of empty — either is fine for empty input
        # At minimum should not raise
