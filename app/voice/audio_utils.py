"""
Audio format conversion utilities for Twilio Media Streams.

Twilio sends/receives: mulaw (G.711 μ-law), 8kHz, mono, 8-bit
Deepgram expects:      linear16, 16kHz, mono, 16-bit PCM
ElevenLabs returns:    mulaw, 8kHz (ulaw_8000 output format)

All functions are pure (no I/O) and synchronous — called from async context
with asyncio.to_thread where needed for CPU-bound resampling.
"""

import base64
import audioop  # provided by audioop-lts on Python 3.12+
import struct
from typing import Union

import numpy as np
from scipy.signal import resample_poly


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TWILIO_SAMPLE_RATE = 8_000   # Hz — inbound mulaw from Twilio
DEEPGRAM_SAMPLE_RATE = 16_000  # Hz — linear16 expected by Deepgram
MULAW_SAMPLE_WIDTH = 1        # bytes — mulaw is 8-bit
LINEAR16_SAMPLE_WIDTH = 2     # bytes — linear16 is 16-bit signed


# ---------------------------------------------------------------------------
# Core converters
# ---------------------------------------------------------------------------


def mulaw_to_linear16(mulaw_bytes: bytes) -> bytes:
    """
    Convert mulaw 8kHz bytes → linear16 16kHz bytes.

    1. audioop.ulaw2lin: mulaw → 16-bit PCM at 8kHz
    2. resample_poly(2, 1): 8kHz → 16kHz (upsample by 2)

    Returns raw bytes suitable for Deepgram streaming input.
    """
    # Step 1: decode mulaw → linear16 at 8kHz
    linear8k = audioop.ulaw2lin(mulaw_bytes, LINEAR16_SAMPLE_WIDTH)

    # Step 2: unpack to numpy int16 array for high-quality resampling
    num_samples = len(linear8k) // LINEAR16_SAMPLE_WIDTH
    samples = np.frombuffer(linear8k, dtype=np.int16)

    # Step 3: upsample 8kHz → 16kHz using polyphase filter (up=2, down=1)
    upsampled = resample_poly(samples, up=2, down=1).astype(np.int16)

    return upsampled.tobytes()


def linear16_to_mulaw(linear16_bytes: bytes, input_sample_rate: int = DEEPGRAM_SAMPLE_RATE) -> bytes:
    """
    Convert linear16 (at input_sample_rate) → mulaw 8kHz bytes.

    Used when we receive audio from a source at a non-8kHz rate and need
    to put it back onto the Twilio stream.

    1. Downsample to 8kHz if needed
    2. audioop.lin2ulaw: linear16 → mulaw

    Returns raw mulaw bytes suitable for sending to Twilio.
    """
    samples = np.frombuffer(linear16_bytes, dtype=np.int16)

    if input_sample_rate != TWILIO_SAMPLE_RATE:
        # Compute GCD-reduced up/down ratio
        from math import gcd
        g = gcd(TWILIO_SAMPLE_RATE, input_sample_rate)
        up = TWILIO_SAMPLE_RATE // g
        down = input_sample_rate // g
        samples = resample_poly(samples, up=up, down=down).astype(np.int16)

    linear8k = samples.tobytes()
    return audioop.lin2ulaw(linear8k, LINEAR16_SAMPLE_WIDTH)


# ---------------------------------------------------------------------------
# Base64 helpers  (Twilio Media Streams sends audio as base64)
# ---------------------------------------------------------------------------


def base64_decode_audio(b64_payload: str) -> bytes:
    """Decode a base64-encoded audio payload from a Twilio media event."""
    return base64.b64decode(b64_payload)


def base64_encode_audio(audio_bytes: bytes) -> str:
    """Encode raw audio bytes to base64 for sending back over Twilio WebSocket."""
    return base64.b64encode(audio_bytes).decode("utf-8")


# ---------------------------------------------------------------------------
# Convenience combo functions used directly in the pipeline
# ---------------------------------------------------------------------------


def twilio_payload_to_deepgram(b64_payload: str) -> bytes:
    """
    Full pipeline: Twilio base64 mulaw 8kHz → raw linear16 16kHz bytes.
    Ready to stream into Deepgram.
    """
    mulaw_bytes = base64_decode_audio(b64_payload)
    return mulaw_to_linear16(mulaw_bytes)


def elevenlabs_to_twilio_payload(mulaw_bytes: bytes) -> str:
    """
    ElevenLabs returns ulaw_8000 bytes directly — just base64-encode for Twilio.
    """
    return base64_encode_audio(mulaw_bytes)


def calculate_audio_duration_ms(pcm_bytes: bytes, sample_rate: int, sample_width: int = 2) -> int:
    """Return duration in milliseconds for a raw PCM byte buffer."""
    num_samples = len(pcm_bytes) // sample_width
    return int((num_samples / sample_rate) * 1000)


def chunk_audio(audio_bytes: bytes, chunk_size_bytes: int = 3200) -> list[bytes]:
    """
    Split audio_bytes into fixed-size chunks.

    Default chunk_size_bytes=3200 = 100ms of linear16 at 16kHz mono
    (16000 samples/s × 0.1s × 2 bytes = 3200 bytes).
    Useful for streaming to Deepgram in evenly-sized frames.
    """
    return [
        audio_bytes[i : i + chunk_size_bytes]
        for i in range(0, len(audio_bytes), chunk_size_bytes)
    ]
