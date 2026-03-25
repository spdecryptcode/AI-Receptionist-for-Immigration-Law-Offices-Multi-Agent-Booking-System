"""
Unit tests for voice layer modules:
  - app/voice/stt_deepgram.py   (DeepgramSTT context manager and callbacks)
  - app/voice/tts_elevenlabs.py (ElevenLabsTTS streaming + text helpers)
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

from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from app.voice.stt_deepgram import DeepgramSTT
from app.voice.tts_elevenlabs import (
    ElevenLabsTTS,
    _split_into_sentences,
    _split_keep_remainder,
)


# ===========================================================================
# Helpers
# ===========================================================================

def _make_mock_connection():
    """Return a mock Deepgram live connection with async start/finish."""
    conn = MagicMock()
    conn.on = MagicMock()
    conn.start = AsyncMock(return_value=True)
    conn.finish = AsyncMock()
    conn.send = AsyncMock()
    return conn


def _make_mock_client(conn):
    """Return a mock DeepgramClient whose asynclive.v() returns conn."""
    client = MagicMock()
    client.listen.asynclive.v.return_value = conn
    return client


# ===========================================================================
# DeepgramSTT — construction and context manager
# ===========================================================================

class TestDeepgramSTTInit:
    def test_stores_callbacks(self):
        on_tx = AsyncMock()
        on_speech = AsyncMock()
        on_end = AsyncMock()
        stt = DeepgramSTT(
            on_transcript=on_tx,
            on_speech_start=on_speech,
            on_utterance_end=on_end,
            call_sid="CA123",
        )
        assert stt._on_transcript is on_tx
        assert stt._on_speech_start is on_speech
        assert stt._on_utterance_end is on_end
        assert stt._call_sid == "CA123"

    def test_connection_initially_none(self):
        stt = DeepgramSTT()
        assert stt._connection is None


class TestDeepgramSTTContextManager:
    async def test_aenter_opens_connection(self):
        conn = _make_mock_connection()
        client = _make_mock_client(conn)

        with patch("app.voice.stt_deepgram.DeepgramClient", return_value=client):
            async with DeepgramSTT(call_sid="CA123") as stt:
                assert stt._connection is conn

    async def test_aexit_closes_connection(self):
        conn = _make_mock_connection()
        client = _make_mock_client(conn)

        with patch("app.voice.stt_deepgram.DeepgramClient", return_value=client):
            async with DeepgramSTT(call_sid="CA123") as stt:
                pass  # enter and exit

        conn.finish.assert_called_once()
        assert stt._connection is None

    async def test_connect_raises_when_start_returns_false(self):
        conn = _make_mock_connection()
        conn.start = AsyncMock(return_value=False)
        client = _make_mock_client(conn)

        with patch("app.voice.stt_deepgram.DeepgramClient", return_value=client):
            with pytest.raises(RuntimeError, match="Failed to start Deepgram"):
                stt = DeepgramSTT(call_sid="CA_FAIL")
                await stt._connect()

    async def test_send_audio_delegates_to_connection(self):
        conn = _make_mock_connection()
        client = _make_mock_client(conn)

        with patch("app.voice.stt_deepgram.DeepgramClient", return_value=client):
            async with DeepgramSTT(call_sid="CA123") as stt:
                await stt.send_audio(b"audio_bytes")

        conn.send.assert_called_once_with(b"audio_bytes")

    async def test_send_audio_noop_when_no_connection(self):
        stt = DeepgramSTT()
        # Should not raise even without a connection
        await stt.send_audio(b"audio")

    async def test_event_handlers_registered_on_connect(self):
        conn = _make_mock_connection()
        client = _make_mock_client(conn)

        with patch("app.voice.stt_deepgram.DeepgramClient", return_value=client):
            async with DeepgramSTT(call_sid="CA123"):
                pass

        # on() should have been called multiple times to register handlers
        assert conn.on.call_count >= 3


# ===========================================================================
# DeepgramSTT — event handler callbacks
# ===========================================================================

class TestDeepgramSTTHandlers:
    def _make_stt_with_callbacks(self):
        on_tx = AsyncMock()
        on_speech = AsyncMock()
        on_end = AsyncMock()
        stt = DeepgramSTT(
            on_transcript=on_tx,
            on_speech_start=on_speech,
            on_utterance_end=on_end,
            call_sid="CA123",
        )
        return stt, on_tx, on_speech, on_end

    def _make_transcript_result(self, text: str, is_final: bool = True):
        """Build a mock Deepgram transcript result object."""
        alt = MagicMock()
        alt.transcript = text
        alt.confidence = 0.95

        channel = MagicMock()
        channel.alternatives = [alt]

        result = MagicMock()
        result.channel = channel
        result.is_final = is_final
        result.metadata = None
        return result

    async def test_fires_on_transcript_for_final_result(self):
        stt, on_tx, _, _ = self._make_stt_with_callbacks()
        result = self._make_transcript_result("hello world", is_final=True)
        await stt._handle_transcript(None, result=result)
        on_tx.assert_awaited_once()
        args = on_tx.call_args[0]
        assert args[0] == "hello world"

    async def test_skips_on_transcript_for_non_final(self):
        stt, on_tx, _, _ = self._make_stt_with_callbacks()
        result = self._make_transcript_result("hello", is_final=False)
        await stt._handle_transcript(None, result=result)
        on_tx.assert_not_awaited()

    async def test_skips_on_transcript_for_empty_text(self):
        stt, on_tx, _, _ = self._make_stt_with_callbacks()
        result = self._make_transcript_result("", is_final=True)
        await stt._handle_transcript(None, result=result)
        on_tx.assert_not_awaited()

    async def test_fires_on_speech_start(self):
        stt, _, on_speech, _ = self._make_stt_with_callbacks()
        await stt._handle_speech_started()
        on_speech.assert_awaited_once()

    async def test_fires_on_utterance_end(self):
        stt, _, _, on_end = self._make_stt_with_callbacks()
        await stt._handle_utterance_end()
        on_end.assert_awaited_once()

    async def test_speech_start_no_callback_noop(self):
        stt = DeepgramSTT()
        # Should not raise
        await stt._handle_speech_started()

    async def test_utterance_end_no_callback_noop(self):
        stt = DeepgramSTT()
        await stt._handle_utterance_end()

    async def test_handle_transcript_skips_when_no_alternatives(self):
        stt, on_tx, _, _ = self._make_stt_with_callbacks()
        channel = MagicMock()
        channel.alternatives = []
        result = MagicMock()
        result.channel = channel
        result.is_final = True
        await stt._handle_transcript(None, result=result)
        on_tx.assert_not_awaited()

    async def test_handle_error_does_not_raise(self):
        stt = DeepgramSTT(call_sid="CA_ERR")
        # Should log but not raise
        await stt._handle_error(None, error="some error")


# ===========================================================================
# _split_into_sentences
# ===========================================================================

class TestSplitIntoSentences:
    def test_splits_on_period(self):
        result = _split_into_sentences("Hello world. How are you?")
        assert len(result) == 2
        assert result[0] == "Hello world."
        assert result[1] == "How are you?"

    def test_splits_on_exclamation(self):
        result = _split_into_sentences("Great news! We can help.")
        assert len(result) == 2

    def test_single_sentence_no_split(self):
        result = _split_into_sentences("No boundary here without punctuation")
        assert result == ["No boundary here without punctuation"]

    def test_empty_string_returns_empty_list(self):
        result = _split_into_sentences("")
        assert result == []

    def test_multiple_punctuation_marks(self):
        result = _split_into_sentences("First. Second! Third?")
        assert len(result) == 3

    def test_strips_whitespace_from_parts(self):
        result = _split_into_sentences("  Hello.   World.  ")
        for part in result:
            assert part == part.strip()


# ===========================================================================
# _split_keep_remainder
# ===========================================================================

class TestSplitKeepRemainder:
    def test_splits_complete_sentences_with_remainder(self):
        sentences, remainder = _split_keep_remainder("Hello. World! Left over")
        assert "Hello." in sentences or any("Hello" in s for s in sentences)
        assert "Left over" in remainder

    def test_no_boundary_returns_empty_list_and_full_text(self):
        sentences, remainder = _split_keep_remainder("no sentence end here")
        assert sentences == []
        assert remainder == "no sentence end here"

    def test_all_complete_sentences_leaves_no_remainder(self):
        sentences, remainder = _split_keep_remainder("First. Second.")
        assert len(sentences) >= 1
        assert remainder.strip() == ""

    def test_single_sentence_with_trailing_text(self):
        sentences, remainder = _split_keep_remainder("Done. partial text")
        assert len(sentences) == 1
        assert "partial text" in remainder


# ===========================================================================
# ElevenLabsTTS — voice ID selection
# ===========================================================================

class TestElevenLabsTTSVoiceId:
    def test_english_returns_en_voice_id(self):
        tts = ElevenLabsTTS(language="en")
        with patch("app.voice.tts_elevenlabs.settings") as mock_settings:
            mock_settings.get_voice_id = MagicMock(return_value="voice-en-test")
            result = tts._get_voice_id()
        assert result == "voice-en-test"
        mock_settings.get_voice_id.assert_called_once_with("en")

    def test_spanish_returns_es_voice_id(self):
        tts = ElevenLabsTTS(language="es")
        with patch("app.voice.tts_elevenlabs.settings") as mock_settings:
            mock_settings.get_voice_id = MagicMock(return_value="voice-es-test")
            result = tts._get_voice_id()
        assert result == "voice-es-test"
        mock_settings.get_voice_id.assert_called_once_with("es")


# ===========================================================================
# ElevenLabsTTS — stream_text
# ===========================================================================

class TestElevenLabsTTSStreamText:
    async def test_yields_audio_bytes(self):
        tts = ElevenLabsTTS(language="en", call_sid="CA123")

        async def mock_synthesize_one(text):
            yield b"audio_chunk_1"
            yield b"audio_chunk_2"

        tts._synthesize_one = mock_synthesize_one

        chunks = []
        async for chunk in tts.stream_text("Hello. World."):
            chunks.append(chunk)

        assert b"audio_chunk_1" in chunks
        assert b"audio_chunk_2" in chunks

    async def test_empty_text_yields_nothing(self):
        tts = ElevenLabsTTS(language="en")

        chunks = []
        async for chunk in tts.stream_text(""):
            chunks.append(chunk)

        assert chunks == []

    async def test_multiple_sentences_produce_multiple_chunks(self):
        call_count = []

        tts = ElevenLabsTTS(language="en", call_sid="CA123")

        async def mock_synthesize_one(text):
            call_count.append(text)
            yield b"chunk"

        tts._synthesize_one = mock_synthesize_one

        async for _ in tts.stream_text("First sentence. Second sentence."):
            pass

        # Two sentences → two calls to _synthesize_one
        assert len(call_count) == 2


# ===========================================================================
# ElevenLabsTTS — stream_tokens
# ===========================================================================

class TestElevenLabsTTSStreamTokens:
    async def _token_iter(self, *tokens):
        for t in tokens:
            yield t

    async def test_buffers_and_flushes_on_sentence_boundary(self):
        tts = ElevenLabsTTS(language="en", call_sid="CA123")
        synthesized = []

        async def mock_synthesize_one(text):
            synthesized.append(text)
            yield b"audio"

        tts._synthesize_one = mock_synthesize_one

        tokens = ["This is a complete sentence. ", "And another."]
        async for _ in tts.stream_tokens(self._token_iter(*tokens)):
            pass

        # At least one sentence should have been synthesized
        assert len(synthesized) >= 1

    async def test_flushes_remainder_at_end(self):
        tts = ElevenLabsTTS(language="en", call_sid="CA123")
        synthesized = []

        async def mock_synthesize_one(text):
            synthesized.append(text)
            yield b"audio"

        tts._synthesize_one = mock_synthesize_one

        # Short text below _MIN_CHUNK_CHARS threshold — won't flush mid-stream
        # but WILL flush at end of turn as remainder
        tokens = ["Short"]
        async for _ in tts.stream_tokens(self._token_iter(*tokens)):
            pass

        assert len(synthesized) == 1
        assert synthesized[0] == "Short"

    async def test_yields_audio_chunks(self):
        tts = ElevenLabsTTS(language="en", call_sid="CA123")

        async def mock_synthesize_one(text):
            yield b"byte_chunk"

        tts._synthesize_one = mock_synthesize_one

        chunks = []
        tokens = ["Hello world. "]
        async for chunk in tts.stream_tokens(self._token_iter(*tokens)):
            chunks.append(chunk)

        assert b"byte_chunk" in chunks
