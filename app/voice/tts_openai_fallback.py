"""
OpenAI TTS fallback — used when ElevenLabs quota is exhausted.

Uses OpenAI tts-1 (PCM output at 24000Hz) downsampled to mulaw 8000Hz
for Twilio, using the existing linear16_to_mulaw helper.

Drop-in replacement for ElevenLabsTTS: exposes stream_text() and stream_tokens()
with identical async-generator signatures.
"""
from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator

from openai import AsyncOpenAI

from app.config import settings
from app.voice.audio_utils import linear16_to_mulaw

logger = logging.getLogger(__name__)

# OpenAI PCM output is 24000 Hz, 16-bit signed mono
_OPENAI_SAMPLE_RATE = 24000

# Sentence-boundary detector (same logic as ElevenLabs TTS)
_SENTENCE_END = re.compile(r'[.!?]+\s*')
_MIN_CHUNK_CHARS = 20

# Per-request PCM read chunk: ~0.1 s at 24kHz 16-bit = 4800 bytes
_READ_CHUNK = 4800


class OpenAIFallbackTTS:
    """
    Temporary TTS using OpenAI tts-1.
    Sentences are synthesized individually for lower latency.
    """

    def __init__(self, language: str = "en", call_sid: str = ""):
        self.language = language
        self.call_sid = call_sid
        self._client = AsyncOpenAI(api_key=settings.openai_api_key)

    def _voice(self) -> str:
        # nova = warm female (EN), shimmer = softer female (ES)
        return "nova" if self.language == "en" else "shimmer"

    # -------------------------------------------------------------------------
    # Public interface (same as ElevenLabsTTS)
    # -------------------------------------------------------------------------

    async def stream_text(self, text: str) -> AsyncIterator[bytes]:
        """Synthesize a complete string → yield mulaw chunks."""
        if not text.strip():
            return
        async for chunk in self._synthesize(text.strip()):
            yield chunk

    async def stream_tokens(
        self,
        token_iterator: AsyncIterator[str],
    ) -> AsyncIterator[bytes]:
        """
        Buffer streaming LLM tokens into sentences, synthesize each sentence
        independently to minimise first-audio latency.
        """
        buffer = ""
        async for token in token_iterator:
            buffer += token
            if len(buffer) >= _MIN_CHUNK_CHARS and _SENTENCE_END.search(buffer):
                sentences, remainder = _split_keep_remainder(buffer)
                for sentence in sentences:
                    async for chunk in self._synthesize(sentence):
                        yield chunk
                buffer = remainder

        if buffer.strip():
            async for chunk in self._synthesize(buffer.strip()):
                yield chunk

    # -------------------------------------------------------------------------
    # Core synthesis
    # -------------------------------------------------------------------------

    async def _synthesize(self, text: str) -> AsyncIterator[bytes]:
        """Call OpenAI TTS, stream PCM response, convert to mulaw 8kHz."""
        try:
            async with self._client.audio.speech.with_streaming_response.create(
                model="tts-1",
                voice=self._voice(),
                input=text,
                response_format="pcm",   # 16-bit PCM at 24000 Hz, no header
            ) as response:
                pcm_buf = b""
                async for raw in response.iter_bytes(chunk_size=_READ_CHUNK):
                    pcm_buf += raw
                    # Process whole samples only (2 bytes each)
                    usable = len(pcm_buf) - (len(pcm_buf) % 2)
                    if usable >= _READ_CHUNK:
                        mulaw = linear16_to_mulaw(pcm_buf[:usable], _OPENAI_SAMPLE_RATE)
                        pcm_buf = pcm_buf[usable:]
                        yield mulaw

                # Flush remainder
                if len(pcm_buf) >= 2:
                    safe = len(pcm_buf) - (len(pcm_buf) % 2)
                    if safe:
                        yield linear16_to_mulaw(pcm_buf[:safe], _OPENAI_SAMPLE_RATE)

        except Exception as exc:
            logger.error(f"[{self.call_sid}] OpenAI TTS error: {exc}", exc_info=True)


# ---------------------------------------------------------------------------
# Helpers (mirrors ElevenLabs TTS internals)
# ---------------------------------------------------------------------------

def _split_keep_remainder(text: str) -> tuple[list[str], str]:
    """Split text at sentence boundaries; return (sentences, leftover)."""
    parts = _SENTENCE_END.split(text)
    ends = _SENTENCE_END.findall(text)

    sentences = []
    for i, part in enumerate(parts[:-1]):
        sentence = part.strip() + (ends[i] if i < len(ends) else "")
        if sentence.strip():
            sentences.append(sentence.strip())

    return sentences, parts[-1]
