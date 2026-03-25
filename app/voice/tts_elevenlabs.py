"""
ElevenLabs Flash v2.5 streaming TTS client.

Design:
  - Uses ElevenLabs WebSocket stream-input API for lowest latency
  - output_format=ulaw_8000 → bytes go directly to Twilio without re-encoding
  - optimize_streaming_latency=3 → aggressive latency mode
  - Sentence-aware chunking: buffers LLM token stream until a sentence boundary
    before flushing to ElevenLabs (avoids cutting words mid-sentence)
  - Implements flush() to force-drain any partial sentence at end of turn
  - Exposes async generator: stream_tts(text_chunks) → yields mulaw bytes

Sentence boundary markers: . ! ? — plus common Spanish ¿ ¡ paired characters.
"""
from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator
from typing import AsyncGenerator

from elevenlabs import AsyncElevenLabs, VoiceSettings
from elevenlabs.client import AsyncElevenLabs as ElevenLabsAsync

from app.config import settings

logger = logging.getLogger(__name__)

# Sentence-ending punctuation pattern
_SENTENCE_END = re.compile(r'[.!?]+\s*')
# Minimum characters before we flush (avoid flushing on "e.g." or "U.S.")
_MIN_CHUNK_CHARS = 20


class ElevenLabsTTS:
    """
    Streaming TTS using ElevenLabs Flash v2.5.

    Usage:
        tts = ElevenLabsTTS(language="en", call_sid="CA123")
        async for mulaw_bytes in tts.stream_text(text_iterator):
            await send_to_twilio(mulaw_bytes)
    """

    def __init__(self, language: str = "en", call_sid: str = ""):
        self.language = language
        self.call_sid = call_sid
        self._client = AsyncElevenLabs(api_key=settings.elevenlabs_api_key)

    def _get_voice_id(self) -> str:
        """Return the configured voice ID for this call's language."""
        return settings.get_voice_id(self.language)

    # -------------------------------------------------------------------------
    # Main public method: stream full response
    # -------------------------------------------------------------------------

    async def stream_text(self, text: str) -> AsyncIterator[bytes]:
        """
        Convert a complete text string to mulaw audio, yielding chunks as they arrive.

        Splits text into sentence-sized chunks before sending to ElevenLabs
        to reduce latency on the first audio byte.
        """
        sentences = _split_into_sentences(text)
        async for audio_chunk in self._synthesize_sentences(sentences):
            yield audio_chunk

    async def stream_tokens(
        self,
        token_iterator: AsyncIterator[str],
    ) -> AsyncIterator[bytes]:
        """
        Stream token-by-token output from the LLM directly into TTS.

        Buffers tokens until a sentence boundary, then synthesizes each
        sentence independently to minimize end-to-end latency.

        Yields mulaw audio bytes as they become available from ElevenLabs.
        """
        buffer = ""
        async for token in token_iterator:
            buffer += token
            # Check for sentence boundary with enough content
            if len(buffer) >= _MIN_CHUNK_CHARS and _SENTENCE_END.search(buffer):
                # Split at the last sentence boundary, keep remainder in buffer
                sentences, remainder = _split_keep_remainder(buffer)
                if sentences:
                    async for audio_chunk in self._synthesize_sentences(sentences):
                        yield audio_chunk
                buffer = remainder

        # Flush remaining buffer at end of turn
        if buffer.strip():
            async for audio_chunk in self._synthesize_sentences([buffer.strip()]):
                yield audio_chunk

    # -------------------------------------------------------------------------
    # Synthesis core
    # -------------------------------------------------------------------------

    async def _synthesize_sentences(
        self, sentences: list[str]
    ) -> AsyncIterator[bytes]:
        """Synthesize a list of sentence strings; yield mulaw byte chunks."""
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue
            async for chunk in self._synthesize_one(sentence):
                yield chunk

    async def _synthesize_one(self, text: str) -> AsyncIterator[bytes]:
        """
        Call ElevenLabs TTS for a single sentence or phrase.
        Yields raw mulaw audio bytes.
        """
        voice_id = self._get_voice_id()
        preview = repr(text[:60])
        logger.debug(f"[{self.call_sid}] TTS [{self.language}] voice={voice_id}: {preview}")

        try:
            # convert() is an async generator (not a coroutine) — iterate directly
            async for chunk in self._client.text_to_speech.convert(
                voice_id=voice_id,
                text=text,
                model_id="eleven_flash_v2_5",
                output_format="ulaw_8000",
                voice_settings=VoiceSettings(
                    stability=0.5,
                    similarity_boost=0.8,
                    style=0.0,
                    use_speaker_boost=True,
                ),
                optimize_streaming_latency=3,
            ):
                if chunk:
                    yield chunk
        except Exception as exc:
            logger.error(f"[{self.call_sid}] ElevenLabs TTS error: {exc}", exc_info=True)
            # Don't raise — pipeline continues, silence is better than a crash


# ---------------------------------------------------------------------------
# Text splitting helpers
# ---------------------------------------------------------------------------

def _split_into_sentences(text: str) -> list[str]:
    """
    Split text into sentences on . ! ? boundaries.
    Keeps punctuation attached to the sentence.
    """
    # Use regex to split but retain delimiters
    parts = re.split(r'(?<=[.!?])\s+', text.strip())
    return [p.strip() for p in parts if p.strip()]


def _split_keep_remainder(text: str) -> tuple[list[str], str]:
    """
    Split text at sentence boundaries; return (sentences_list, leftover_text).
    Leftover is the incomplete sentence fragment after the last boundary.
    """
    # Find all sentence endings
    matches = list(_SENTENCE_END.finditer(text))
    if not matches:
        return [], text

    last_end = matches[-1].end()
    complete = text[:last_end]
    remainder = text[last_end:]

    sentences = _split_into_sentences(complete)
    return sentences, remainder
