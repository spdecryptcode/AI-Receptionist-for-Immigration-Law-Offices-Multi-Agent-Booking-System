"""
Top-level pytest conftest that stubs third-party packages unavailable in the
local dev environment so unit tests can import app modules without installing
every service dependency (supabase, twilio, deepgram, elevenlabs).

Only runs when those packages are NOT already installed — safe to keep when
running tests inside Docker / CI where the full requirements.txt is present.
"""
from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock


def _stub(name: str, **attrs) -> types.ModuleType:
    """Create a lightweight stub module and register it in sys.modules."""
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules.setdefault(name, mod)
    return sys.modules[name]


# ---------------------------------------------------------------------------
# supabase
# ---------------------------------------------------------------------------
if "supabase" not in sys.modules:
    _stub("supabase", create_client=MagicMock(), Client=MagicMock())

# ---------------------------------------------------------------------------
# twilio (full hierarchy used by twiml_responses, call_transfer, etc.)
# ---------------------------------------------------------------------------
if "twilio" not in sys.modules:
    _stub("twilio")
    _stub("twilio.twiml")
    _stub("twilio.twiml.voice_response",
          VoiceResponse=MagicMock, Dial=MagicMock, Stream=MagicMock,
          Connect=MagicMock)
    _stub("twilio.rest", Client=MagicMock)
    _stub("twilio.request_validator", RequestValidator=MagicMock)

# ---------------------------------------------------------------------------
# deepgram
# ---------------------------------------------------------------------------
if "deepgram" not in sys.modules:
    _stub("deepgram")
    _stub("deepgram.audio")
    _stub("deepgram.audio.microphone")
    _stub("deepgram.clients")
    _stub("deepgram.clients.live")
    _stub("deepgram.clients.live.v1", LiveClient=MagicMock)
    dg_types = _stub("deepgram.clients.live.v1.async_client",
                     AsyncLiveClient=MagicMock)
    _stub("deepgram.core")
    # Most usage: `from deepgram import DeepgramClient, LiveTranscriptionEvents, ...`
    for attr in (
        "DeepgramClient", "LiveTranscriptionEvents", "LiveOptions",
        "DeepgramClientOptions", "Encoding",
    ):
        sys.modules["deepgram"].__dict__.setdefault(attr, MagicMock())

# ---------------------------------------------------------------------------
# elevenlabs
# ---------------------------------------------------------------------------
if "elevenlabs" not in sys.modules:
    _stub("elevenlabs")
    _stub("elevenlabs.client", AsyncElevenLabs=MagicMock)
    for attr in ("AsyncElevenLabs", "VoiceSettings"):
        sys.modules["elevenlabs"].__dict__.setdefault(attr, MagicMock())

# ---------------------------------------------------------------------------
# httpx (used by dependencies.py — may be installed; only stub if missing)
# ---------------------------------------------------------------------------
try:
    import httpx  # noqa: F401
except ImportError:
    _stub("httpx", AsyncClient=MagicMock, Limits=MagicMock, Timeout=MagicMock)
