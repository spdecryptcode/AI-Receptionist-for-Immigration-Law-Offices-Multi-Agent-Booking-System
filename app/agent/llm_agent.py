"""
OpenAI GPT-4o LLM agent for the IVR immigration intake pipeline.

Design:
  - Maintains conversation history per call in memory (not Redis) since context
    is small and latency matters
  - Loads the static system prompt once per call (prompt caching kicks in
    after first call with identical prefix > 1024 tokens)
  - Supports streaming completions for low-latency sentence-by-sentence TTS
  - Extracts structured intake data from conversation via a separate extraction call
  - Detects intent signals: EMERGENCY_TRANSFER, SCHEDULE_NOW, LANGUAGE_SWITCH
  - Per-phase max_tokens: greeting (75), intake (150), pitch (250), booking (100)

Prompt caching note:
  The system prompt file must be > 1024 tokens and identical across all calls.
  Dynamic context (caller name, history recap) goes in the FIRST user message,
  not in the system prompt.
"""
from __future__ import annotations

import json
import logging
import os
import re
from enum import Enum
from pathlib import Path
from typing import AsyncIterator

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionChunk

from app.config import settings
from app.dependencies import get_openai_client

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Load system prompts once at module import (they're static files)
# ---------------------------------------------------------------------------

_PROMPT_DIR = Path(__file__).parent.parent.parent / "prompts"

def _load_prompt(filename: str) -> str:
    path = _PROMPT_DIR / filename
    if not path.exists():
        logger.warning(f"Prompt file not found: {path}")
        return ""
    return path.read_text(encoding="utf-8")


_SYSTEM_PROMPT_EN = _load_prompt("system_prompt_en.md")
_SYSTEM_PROMPT_ES = _load_prompt("system_prompt_es.md")


# ---------------------------------------------------------------------------
# Enums / data types
# ---------------------------------------------------------------------------

class ConversationPhase(str, Enum):
    GREETING = "greeting"
    IDENTIFICATION = "identification"
    URGENCY_TRIAGE = "urgency_triage"
    INTAKE = "intake"
    CONSULTATION_PITCH = "consultation_pitch"
    BOOKING = "booking"
    CONFIRMATION = "confirmation"
    CLOSING = "closing"


# Max tokens per phase — keeps responses short and natural for voice
_MAX_TOKENS: dict[ConversationPhase, int] = {
    ConversationPhase.GREETING: 75,
    ConversationPhase.IDENTIFICATION: 80,
    ConversationPhase.URGENCY_TRIAGE: 100,
    ConversationPhase.INTAKE: 150,
    ConversationPhase.CONSULTATION_PITCH: 250,
    ConversationPhase.BOOKING: 100,
    ConversationPhase.CONFIRMATION: 100,
    ConversationPhase.CLOSING: 75,
}

# Signals extracted from LLM output that trigger pipeline actions
EMERGENCY_SIGNAL = "EMERGENCY_TRANSFER"
SCHEDULE_SIGNAL = "SCHEDULE_NOW"
CONFIRM_SLOT_SIGNAL = "CONFIRM_SLOT:"  # followed immediately by ISO datetime, e.g. CONFIRM_SLOT:2026-03-25T09:00:00Z
LANGUAGE_SWITCH_ES = "LANGUAGE_SWITCH_ES"
LANGUAGE_SWITCH_EN = "LANGUAGE_SWITCH_EN"
END_CALL_SIGNAL = "END_CALL"

# Regex that matches a complete line containing only a signal token — these
# must never be forwarded to TTS / spoken aloud by ElevenLabs.
_SIGNAL_LINE_RE = re.compile(
    r"^\s*("
    r"CONFIRM_SLOT:\S+"
    r"|SCHEDULE_NOW"
    r"|EMERGENCY_TRANSFER"
    r"|PHASE:\w+"
    r"|LANGUAGE_SWITCH_ES"
    r"|LANGUAGE_SWITCH_EN"
    r"|END_CALL"
    r")\s*$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# LLM Agent
# ---------------------------------------------------------------------------

class ImmigrationAgent:
    """
    Manages the conversation with a single caller.

    Instantiated once per call. Not thread-safe — one instance per asyncio Task.
    """

    def __init__(
        self,
        call_sid: str,
        caller_phone: str,
        language: str = "en",
        caller_name: str | None = None,
        returning_client: bool = False,
    ):
        self.call_sid = call_sid
        self.caller_phone = caller_phone
        self.language = language  # "en" or "es"
        self.caller_name = caller_name
        self.returning_client = returning_client
        self.phase = ConversationPhase.GREETING
        self._history: list[dict[str, str]] = []
        self._client: AsyncOpenAI = get_openai_client()

        # Structured intake data collected during the call
        self.intake_data: dict = {}

        # Accumulated OpenAI token usage — read by cost_tracker at call end
        self._total_input_tokens: int = 0
        self._total_output_tokens: int = 0

        # Dynamic runtime context injected before each LLM call:
        # current date/time, available slots, intake hint, etc.
        self.runtime_context: str = ""

    # -------------------------------------------------------------------------
    # Main entry: get next assistant response (streaming)
    # -------------------------------------------------------------------------

    async def respond_stream(
        self, caller_transcript: str
    ) -> AsyncIterator[str]:
        """
        Given a caller utterance, stream the assistant's response sentence by sentence.

        Yields text chunks. Caller should accumulate until sentence boundary
        before sending to TTS (to avoid cutting words mid-sentence).

        Also fires side-effects: phase transitions, signal extraction.
        """
        self._history.append({"role": "user", "content": caller_transcript})

        messages = self._build_messages()
        max_tokens = _MAX_TOKENS.get(self.phase, 150)

        full_response = ""
        try:
            stream = await self._client.chat.completions.create(
                model=settings.openai_model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=0.3,   # low = more predictable, professional
                stream=True,
                stream_options={"include_usage": True},
            )

            # Buffer tokens until we have a full line so we can suppress signal
            # lines (CONFIRM_SLOT:…, SCHEDULE_NOW, etc.) before they reach TTS.
            line_buf = ""
            async for chunk in stream:
                if chunk.choices:
                    delta = chunk.choices[0].delta.content or ""
                    full_response += delta
                    line_buf += delta
                    # Flush complete lines, suppressing pure-signal lines
                    while "\n" in line_buf:
                        line, line_buf = line_buf.split("\n", 1)
                        if not _SIGNAL_LINE_RE.match(line):
                            yield line + "\n"
                if chunk.usage:
                    self._total_input_tokens += chunk.usage.prompt_tokens or 0
                    self._total_output_tokens += chunk.usage.completion_tokens or 0
            # Flush remainder (last line with no trailing newline)
            if line_buf and not _SIGNAL_LINE_RE.match(line_buf):
                yield line_buf

        except Exception as exc:
            logger.error(f"[{self.call_sid}] OpenAI stream error: {exc}", exc_info=True)
            yield "I apologize, I'm having a brief technical issue. Please hold for just a moment."
            full_response = ""

        if full_response:
            self._history.append({"role": "assistant", "content": full_response})
            # Process signals in background (don't block streaming)
            self._process_signals(full_response)
            # Optionally advance phase based on response content
            self._maybe_advance_phase(full_response)

    # -------------------------------------------------------------------------
    # Non-streaming version (for testing and simple call paths)
    # -------------------------------------------------------------------------

    async def respond(self, caller_transcript: str) -> str:
        """Non-streaming: return complete assistant response."""
        chunks = []
        async for chunk in self.respond_stream(caller_transcript):
            chunks.append(chunk)
        return "".join(chunks)

    # -------------------------------------------------------------------------
    # Initial greeting (no caller input yet)
    # -------------------------------------------------------------------------

    async def greeting_stream(self) -> AsyncIterator[str]:
        """
        Generate the opening greeting without any caller input.
        Called at the start of a call.
        """
        # Inject returning-client context
        context = ""
        if self.returning_client and self.caller_name:
            context = f"The caller is a returning client named {self.caller_name}. "
        elif self.returning_client:
            context = "The caller is a returning client. "

        opening_prompt = (
            f"{context}Begin Phase 1: deliver the opening greeting. "
            f"Firm name: {settings.law_firm_name}."
        )

        self._history.append({"role": "user", "content": opening_prompt})
        messages = self._build_messages()

        full_response = ""
        stream = await self._client.chat.completions.create(
            model=settings.openai_model,
            messages=messages,
            max_tokens=_MAX_TOKENS[ConversationPhase.GREETING],
            temperature=0.3,
            stream=True,
            stream_options={"include_usage": True},
        )
        async for chunk in stream:
            if chunk.choices:
                delta = chunk.choices[0].delta.content or ""
                full_response += delta
                yield delta
            if chunk.usage:
                self._total_input_tokens += chunk.usage.prompt_tokens or 0
                self._total_output_tokens += chunk.usage.completion_tokens or 0

        if full_response:
            self._history.append({"role": "assistant", "content": full_response})

    # -------------------------------------------------------------------------
    # Structured intake extraction
    # -------------------------------------------------------------------------

    async def extract_intake_data(self) -> dict:
        """
        Run a separate non-streaming extraction call to pull structured fields
        from the conversation so far. Returns a JSON dict matching immigration_intake columns.
        """
        transcript_summary = "\n".join(
            f"{m['role'].upper()}: {m['content']}"
            for m in self._history
            if m["role"] in ("user", "assistant")
        )

        extraction_prompt = f"""
You are a data extraction assistant. Extract immigration intake information from the following conversation transcript. 
Return ONLY a valid JSON object with the fields you can confidently extract. If a field is uncertain, omit it.

Fields to extract:
- full_name (string — caller's full name as spoken, or null)
- first_name (string or null)
- last_name (string or null)
- is_detained (boolean)
- has_court_date (boolean)
- court_date (ISO date string YYYY-MM-DD or null)
- court_location (string or null)
- visa_expiration_date (ISO date string or null)
- urgency_level: one of "critical", "high", "medium", "routine"
- case_type: one of "family_sponsorship", "employment_visa", "asylum", "removal_defense", "daca", "tps", "naturalization", "other"
- current_immigration_status (string or null)
- a_number (string, Alien Registration Number, or null)
- years_in_us (integer or null)
- has_criminal_record (boolean or null)
- has_prior_visa_denial (boolean or null)
- us_family_connections (boolean or null)
- entry_method: one of "legal_visa", "border_crossing_no_inspection", "visa_overstay", "unknown", or null
- preferred_language: "en" or "es"

TRANSCRIPT:
{transcript_summary}

Return only the JSON object, no explanation.
"""

        try:
            response = await self._client.chat.completions.create(
                model=settings.openai_model,
                messages=[{"role": "user", "content": extraction_prompt}],
                max_tokens=500,
                temperature=0,
                response_format={"type": "json_object"},
            )
            raw = response.choices[0].message.content or "{}"
            extracted = json.loads(raw)
            self.intake_data.update(extracted)
            if response.usage:
                self._total_input_tokens += response.usage.prompt_tokens or 0
                self._total_output_tokens += response.usage.completion_tokens or 0
            logger.info(f"[{self.call_sid}] Extracted intake: {list(extracted.keys())}")
            return extracted
        except Exception as exc:
            logger.error(f"[{self.call_sid}] Intake extraction failed: {exc}", exc_info=True)
            return {}

    # -------------------------------------------------------------------------
    # Language switch
    # -------------------------------------------------------------------------

    def switch_language(self, language: str) -> None:
        """Switch the conversation language. Replaces system prompt for the remainder of the call."""
        if language not in ("en", "es"):
            return
        self.language = language
        logger.info(f"[{self.call_sid}] Language switched to {language}")

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    def _build_messages(self) -> list[dict[str, str]]:
        """
        Build the messages list for the OpenAI API call.
        System prompt is always first (static prefix for prompt caching).
        """
        system_prompt = _SYSTEM_PROMPT_EN if self.language == "en" else _SYSTEM_PROMPT_ES
        # Substitute firm name placeholder
        system_prompt = system_prompt.replace("[LAW FIRM NAME]", settings.law_firm_name)

        messages = [{"role": "system", "content": system_prompt}]
        # Inject dynamic runtime context (current date/time, available slots, intake guidance)
        if self.runtime_context:
            messages.append({"role": "system", "content": self.runtime_context})
        messages.extend(self._history)
        return messages

    def _process_signals(self, response_text: str) -> None:
        """Check the assistant's response for action signals and update state."""
        text_upper = response_text.upper()

        if EMERGENCY_SIGNAL in text_upper:
            logger.warning(f"[{self.call_sid}] EMERGENCY_TRANSFER signal detected")
            # Pipeline picks this up via check_signals()

        if LANGUAGE_SWITCH_ES in text_upper:
            self.switch_language("es")
        elif LANGUAGE_SWITCH_EN in text_upper:
            self.switch_language("en")

    def _maybe_advance_phase(self, response_text: str) -> None:
        """
        Heuristically advance the conversation phase based on the latest response.
        The LLM prompt instructs Sofia to emit these tokens at phase transitions.
        """
        text_upper = response_text.upper()
        phase_map = {
            "PHASE:IDENTIFICATION": ConversationPhase.IDENTIFICATION,
            "PHASE:URGENCY_TRIAGE": ConversationPhase.URGENCY_TRIAGE,
            "PHASE:INTAKE": ConversationPhase.INTAKE,
            "PHASE:CONSULTATION_PITCH": ConversationPhase.CONSULTATION_PITCH,
            "PHASE:BOOKING": ConversationPhase.BOOKING,
            "PHASE:CONFIRMATION": ConversationPhase.CONFIRMATION,
            "PHASE:CLOSING": ConversationPhase.CLOSING,
        }
        for marker, phase in phase_map.items():
            if marker in text_upper:
                self.phase = phase
                logger.debug(f"[{self.call_sid}] Phase → {phase.value}")
                break

    def check_signals(self, response_text: str) -> dict:
        """
        Return a dict of action signals present in the response.
        Used by the pipeline to decide what to do after TTS.
        'confirm_slot' is an ISO datetime string if the LLM emitted CONFIRM_SLOT:ISO, else "".
        """
        text_upper = response_text.upper()
        # Extract CONFIRM_SLOT:{ISO} — case-insensitive, captures non-whitespace after colon
        m = re.search(r'CONFIRM_SLOT:(\S+)', response_text, re.IGNORECASE)
        confirm_slot_iso = m.group(1) if m else ""
        return {
            "emergency_transfer": EMERGENCY_SIGNAL in text_upper,
            "schedule_now": SCHEDULE_SIGNAL in text_upper,
            "confirm_slot": confirm_slot_iso,
            "language_switch_es": LANGUAGE_SWITCH_ES in text_upper,
            "language_switch_en": LANGUAGE_SWITCH_EN in text_upper,
            "end_call": END_CALL_SIGNAL in text_upper,
        }

    def get_history_for_db(self) -> list[dict]:
        """Return conversation history formatted for database storage."""
        return [
            {
                "role": m["role"],
                "content": m["content"],
            }
            for m in self._history
            if m["role"] in ("user", "assistant")
        ]
