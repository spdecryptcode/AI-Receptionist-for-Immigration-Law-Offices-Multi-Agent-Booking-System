"""
Deepgram Nova-3 / Flux real-time speech-to-text client.

Design:
  - Uses Deepgram SDK v3 async WebSocket live transcription
  - Accepts linear16 16kHz audio chunks (after mulaw conversion)
  - Fires on_transcript callback with final utterances (is_final=True)
  - Fires on_utterance_end callback when Deepgram's EndOfTurn fires
  - Detects caller language for Spanish hand-off (language detection param)
  - Exposes a simple async context-manager interface: `async with DeepgramSTT() as stt:`

Key Deepgram parameters:
  model="nova-3"       — lowest latency, best for phone audio
  language="multi"     — auto-detect EN/ES
  encoding="linear16"  — we upsample mulaw→linear16 before sending
  sample_rate=16000
  channels=1
  smart_format=True    — punctuation + paragraphs
  endpointing=200      — ms of silence = end of utterance (balanced: not too fast, not too jumpy)
  vad_events=True      — get SpeechStarted events for barge-in detection
  utterance_end_ms=1000 — fire UtteranceEnd after 1s silence
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any

from deepgram import (
    DeepgramClient,
    DeepgramClientOptions,
    LiveOptions,
    LiveTranscriptionEvents,
)
from deepgram.clients.live.v1 import LiveClient

from app.config import settings

logger = logging.getLogger(__name__)

# Type aliases for callback signatures
TranscriptCallback = Callable[[str, float, str], Coroutine[Any, Any, None]]
# args: (transcript_text, confidence, detected_language)

SpeechStartCallback = Callable[[], Coroutine[Any, Any, None]]
UtteranceEndCallback = Callable[[], Coroutine[Any, Any, None]]


class DeepgramSTT:
    """
    Async wrapper around Deepgram live transcription.

    Usage:
        stt = DeepgramSTT(
            on_transcript=my_handler,
            on_speech_start=barge_in_handler,
            on_utterance_end=end_of_turn_handler,
        )
        async with stt:
            stt.send_audio(linear16_bytes)
    """

    def __init__(
        self,
        on_transcript: TranscriptCallback | None = None,
        on_speech_start: SpeechStartCallback | None = None,
        on_utterance_end: UtteranceEndCallback | None = None,
        call_sid: str = "",
    ):
        self._on_transcript = on_transcript
        self._on_speech_start = on_speech_start
        self._on_utterance_end = on_utterance_end
        self._call_sid = call_sid
        self._connection: LiveClient | None = None
        self._client: DeepgramClient | None = None
        self._loop = asyncio.get_event_loop()
        # Buffer is_final segments until UtteranceEnd fires (true end-of-turn)
        self._transcript_buffer: list[str] = []
        self._last_confidence: float = 1.0
        self._last_language: str = "en"

    async def __aenter__(self) -> "DeepgramSTT":
        await self._connect()
        return self

    async def __aexit__(self, *_) -> None:
        await self.close()

    async def _connect(self) -> None:
        """Open the Deepgram live WebSocket connection."""
        self._client = DeepgramClient(
            api_key=settings.deepgram_api_key,
            config=DeepgramClientOptions(
                verbose=False,
            ),
        )
        self._connection = self._client.listen.asynclive.v("1")

        # Register event handlers
        self._connection.on(LiveTranscriptionEvents.Transcript, self._handle_transcript)
        self._connection.on(LiveTranscriptionEvents.SpeechStarted, self._handle_speech_started)
        self._connection.on(LiveTranscriptionEvents.UtteranceEnd, self._handle_utterance_end)
        self._connection.on(LiveTranscriptionEvents.Error, self._handle_error)
        self._connection.on(LiveTranscriptionEvents.Close, self._handle_close)

        options = LiveOptions(
            model="nova-3",
            language="multi",       # auto-detect EN/ES
            encoding="linear16",
            sample_rate=16000,
            channels=1,
            smart_format=True,
            punctuate=True,
            endpointing=500,        # ms silence → segment boundary (increased from 200 to avoid mid-sentence cuts)
            vad_events=True,        # SpeechStarted events for barge-in
            utterance_end_ms=1500,  # fire UtteranceEnd after 1500ms silence (true end-of-turn)
            interim_results=True,   # partial results for barge-in detection
        )

        started = await self._connection.start(options)
        if not started:
            raise RuntimeError(f"Failed to start Deepgram connection for call {self._call_sid}")

        logger.info(f"Deepgram STT connected for call {self._call_sid}")

    async def send_audio(self, linear16_bytes: bytes) -> None:
        """Send a chunk of linear16 16kHz audio to Deepgram."""
        if self._connection:
            await self._connection.send(linear16_bytes)

    async def close(self) -> None:
        """Gracefully close the Deepgram connection."""
        # Flush any buffered transcript that didn't get an UtteranceEnd
        await self._flush_buffer()
        if self._connection:
            await self._connection.finish()
            self._connection = None
            logger.info(f"Deepgram STT closed for call {self._call_sid}")

    async def _flush_buffer(self) -> None:
        """Emit buffered is_final segments as a single on_transcript call."""
        if self._transcript_buffer and self._on_transcript:
            full_text = " ".join(self._transcript_buffer).strip()
            self._transcript_buffer.clear()
            if full_text:
                await self._on_transcript(full_text, self._last_confidence, self._last_language)

    # -------------------------------------------------------------------------
    # Event handlers (called by Deepgram SDK on its internal loop)
    # -------------------------------------------------------------------------

    async def _handle_transcript(self, *args, **kwargs) -> None:
        """
        Called on every transcript event. Fires the on_transcript callback
        only for final (non-interim) utterances with actual content.
        """
        result = kwargs.get("result") or (args[1] if len(args) > 1 else None)
        if result is None:
            return

        try:
            channel = result.channel
            alternatives = channel.alternatives
            if not alternatives:
                return

            alt = alternatives[0]
            transcript = alt.transcript.strip()
            confidence = alt.confidence if hasattr(alt, "confidence") else 0.0
            is_final = result.is_final

            if not transcript or not is_final:
                return

            # Extract detected language
            detected_language = "en"
            if hasattr(result, "metadata") and result.metadata:
                detected_language = getattr(result.metadata, "language", "en") or "en"

            logger.debug(
                f"[{self._call_sid}] STT [{detected_language}] ({confidence:.2f}): {transcript!r}"
            )

            # Accumulate into buffer — UtteranceEnd will flush as a complete turn
            self._transcript_buffer.append(transcript)
            self._last_confidence = confidence
            self._last_language = detected_language

        except Exception as exc:
            logger.error(f"[{self._call_sid}] Error processing transcript: {exc}", exc_info=True)

    async def _handle_speech_started(self, *args, **kwargs) -> None:
        """Called when Deepgram detects the caller started speaking (barge-in signal)."""
        logger.debug(f"[{self._call_sid}] SpeechStarted — potential barge-in")
        if self._on_speech_start:
            try:
                await self._on_speech_start()
            except Exception as exc:
                logger.error(f"[{self._call_sid}] Error in speech_start callback: {exc}")

    async def _handle_utterance_end(self, *args, **kwargs) -> None:
        """
        Called when Deepgram fires UtteranceEnd (1.5s of silence after speech).
        This is the true end-of-turn signal — flush the buffer now.
        """
        logger.debug(f"[{self._call_sid}] UtteranceEnd — flushing transcript buffer")
        try:
            await self._flush_buffer()
        except Exception as exc:
            logger.error(f"[{self._call_sid}] Error flushing transcript buffer: {exc}")
        if self._on_utterance_end:
            try:
                await self._on_utterance_end()
            except Exception as exc:
                logger.error(f"[{self._call_sid}] Error in utterance_end callback: {exc}")

    async def _handle_error(self, *args, **kwargs) -> None:
        error = kwargs.get("error") or (args[1] if len(args) > 1 else None)
        logger.error(f"[{self._call_sid}] Deepgram error: {error}")

    async def _handle_close(self, *args, **kwargs) -> None:
        logger.info(f"[{self._call_sid}] Deepgram connection closed")
        self._connection = None
