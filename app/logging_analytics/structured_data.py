"""
Post-call structured data extraction.

Converts the raw conversation + slot-filled intake dict into clean, validated
data ready for the `immigration_intake` table and GHL custom fields.

Uses GPT-4o function-calling for fields not already captured by the slot-filler
(e.g., nuanced answers, multi-sentence descriptions that need normalisation).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from app.config import settings

logger = logging.getLogger(__name__)


# ─── Extraction tool definition ───────────────────────────────────────────────

_INTAKE_EXTRACTION_TOOL: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "save_intake_fields",
            "description": (
                "Extract and normalise all relevant immigration intake fields "
                "from the conversation transcript."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    # Identity
                    "full_name": {"type": "string"},
                    "preferred_name": {"type": "string"},
                    "date_of_birth": {
                        "type": "string",
                        "description": "ISO 8601 date or 'unknown'",
                    },
                    "country_of_origin": {"type": "string"},
                    "primary_language": {"type": "string"},
                    # Contact
                    "phone_number": {"type": "string"},
                    "email": {"type": "string"},
                    "city": {"type": "string"},
                    "state": {"type": "string"},
                    # Immigration
                    "current_immigration_status": {
                        "type": "string",
                        "description": (
                            "E.g. 'undocumented', 'DACA', 'TPS', 'H-1B', 'F-1', "
                            "'green card holder', 'US citizen', 'asylum seeker', 'pending case'"
                        ),
                    },
                    "case_type": {
                        "type": "string",
                        "description": (
                            "Primary legal matter: 'family-based', 'asylum', 'DACA', 'TPS', "
                            "'removal defense', 'U visa', 'VAWA', 'employment', 'naturalization', "
                            "'consular processing', 'other'"
                        ),
                    },
                    "time_in_us_years": {
                        "type": "number",
                        "description": "Years in the US (approximate OK)",
                    },
                    "has_prior_attorney": {"type": "boolean"},
                    "prior_attorney_notes": {"type": "string"},
                    "has_upcoming_hearing": {"type": "boolean"},
                    "hearing_date": {
                        "type": "string",
                        "description": "ISO 8601 date or descriptive text",
                    },
                    "has_prior_deportation": {"type": "boolean"},
                    "has_criminal_history": {"type": "boolean"},
                    "case_description": {
                        "type": "string",
                        "description": "2-3 sentence summary of the caller's situation in their own words.",
                    },
                    # Family
                    "family_members_count": {"type": "integer"},
                    "us_citizen_family_member": {"type": "boolean"},
                    # Urgency
                    "urgency_reason": {"type": "string"},
                    "immigration_emergency": {"type": "boolean"},
                    # Appointment
                    "appointment_booked": {"type": "boolean"},
                    "appointment_datetime": {
                        "type": "string",
                        "description": "ISO 8601 datetime or empty string",
                    },
                    "consultation_type": {
                        "type": "string",
                        "enum": ["phone", "video", "in-person", "unknown"],
                    },
                },
                "required": [
                    "full_name",
                    "current_immigration_status",
                    "case_type",
                    "appointment_booked",
                ],
            },
        },
    }
]


# ─── Public API ───────────────────────────────────────────────────────────────

async def extract_structured_intake(
    call_sid: str,
    conversation: list[dict],
    existing_intake: dict,
    language: str = "en",
) -> dict:
    """
    Run GPT-4o extraction over the full conversation.

    Merges with `existing_intake` — GPT output takes precedence for fields
    that were vague/missing in the slot-filler but is overridden by confirmed
    slot values for key identity fields (phone, email, name).

    Returns the merged dict ready for DB insertion.
    """
    turns_text = "\n".join(
        f"{m['role'].upper()}: {m['content']}" for m in conversation[-40:]
    )
    # Include existing partial intake as context so GPT knows what's already confirmed
    existing_summary = json.dumps(
        {k: v for k, v in existing_intake.items() if v}, indent=2
    ) if existing_intake else "{}"

    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=settings.openai_api_key)
        resp = await client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a legal intake data specialist. Extract and normalise "
                        "all available intake fields from this immigration consultation call. "
                        "For fields not mentioned, omit them (do not invent data). "
                        f"Already confirmed fields:\n{existing_summary}"
                    ),
                },
                {"role": "user", "content": turns_text},
            ],
            tools=_INTAKE_EXTRACTION_TOOL,
            tool_choice={"type": "function", "function": {"name": "save_intake_fields"}},
            max_tokens=600,
            temperature=0.1,
        )
        tool_calls = resp.choices[0].message.tool_calls
        if tool_calls:
            extracted = json.loads(tool_calls[0].function.arguments)
            merged = _merge_intake(existing_intake, extracted)
            logger.info(
                f"[{call_sid}] Structured extraction: "
                f"{len(extracted)} fields extracted, "
                f"{len(merged)} total after merge"
            )
            return merged
    except Exception as exc:
        logger.error(f"[{call_sid}] Structured extraction error: {exc}")

    return existing_intake


# ─── GHL custom fields formatter ──────────────────────────────────────────────

def to_ghl_custom_fields(intake: dict) -> list[dict]:
    """
    Convert the structured intake dict into GHL custom field update format.

    Returns a list of `{"key": ..., "field_value": ...}` pairs
    suitable for the GHL contact update endpoint.
    """
    _FIELD_MAP = {
        "current_immigration_status": "immigration_status",
        "case_type": "case_type",
        "country_of_origin": "country_of_origin",
        "date_of_birth": "date_of_birth",
        "time_in_us_years": "time_in_us",
        "has_upcoming_hearing": "has_upcoming_hearing",
        "hearing_date": "hearing_date",
        "has_prior_attorney": "has_prior_attorney",
        "has_criminal_history": "has_criminal_history",
        "immigration_emergency": "immigration_emergency",
        "case_description": "case_description",
        "consultation_type": "preferred_consultation_type",
    }
    fields = []
    for intake_key, ghl_key in _FIELD_MAP.items():
        value = intake.get(intake_key)
        if value is None:
            continue
        # Booleans → "Yes" / "No" for GHL display
        if isinstance(value, bool):
            value = "Yes" if value else "No"
        fields.append({"key": ghl_key, "field_value": str(value)})
    return fields


# ─── Utility ──────────────────────────────────────────────────────────────────

def _merge_intake(base: dict, extracted: dict) -> dict:
    """
    Merge GPT-extracted fields with already-confirmed slot-filled values.
    Confirmed base values are preserved for identity fields.
    GPT fills in gaps for all other fields.
    """
    # These were confirmed interactively — trust the slot-filler
    _PREFER_BASE = {
        "full_name", "phone_number", "email",
        "appointment_booked", "appointment_datetime",
    }
    merged = {**extracted}  # start with GPT output
    for key in _PREFER_BASE:
        base_val = base.get(key)
        if base_val:  # existing confirmed value wins
            merged[key] = base_val
    # Fill any field present in base but not in extracted
    for key, val in base.items():
        if key not in merged and val:
            merged[key] = val
    return merged
