"""
Intake question flow — decides which question to ask next.

This module maintains the ordered question script and implements smart
skipping: if a field has already been collected (either from CRM lookup
or from an earlier turn in this call), that question is skipped.

Question branching:
  - Criminal history questions only asked if urgency_label is not LOW
  - Employer sponsor only asked if case_type suggests employment-based
  - Prior deportation only asked if urgency_label >= MEDIUM

The module does NOT drive the LLM directly; it produces a "next question"
hint string that is injected into the ContextManager's extra_context slot,
which the LLM uses as a prompt directive.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from app.voice.conversation_state import CallState, INTAKE_FIELDS, UrgencyLabel

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Question definitions
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class IntakeQuestion:
    """A single intake question with its target field and ask conditions."""
    field: str
    prompt_en: str
    prompt_es: str
    # Optional predicate — if provided, question is only asked when it returns True
    condition: Callable[[CallState], bool] | None = None


def _always(_state: CallState) -> bool:
    return True


def _urgency_medium_plus(state: CallState) -> bool:
    return state.urgency_label in (UrgencyLabel.MEDIUM, UrgencyLabel.HIGH, UrgencyLabel.EMERGENCY)


def _case_employment(state: CallState) -> bool:
    ct = str(state.intake.get("case_type", "")).lower()
    return any(k in ct for k in ("employment", "work", "h1b", "l1", "eb", "perm"))


def _urgency_high_plus(state: CallState) -> bool:
    return state.urgency_label in (UrgencyLabel.HIGH, UrgencyLabel.EMERGENCY)


INTAKE_QUESTIONS: list[IntakeQuestion] = [
    IntakeQuestion(
        field="full_name",
        prompt_en="May I have your full legal name, please?",
        prompt_es="¿Me puede dar su nombre completo, por favor?",
    ),
    IntakeQuestion(
        field="date_of_birth",
        prompt_en="And your date of birth?",
        prompt_es="¿Y su fecha de nacimiento?",
    ),
    IntakeQuestion(
        field="country_of_birth",
        prompt_en="What country were you born in?",
        prompt_es="¿En qué país nació?",
    ),
    IntakeQuestion(
        field="nationality",
        prompt_en="What is your current nationality or citizenship?",
        prompt_es="¿Cuál es su nacionalidad o ciudadanía actual?",
    ),
    IntakeQuestion(
        field="current_immigration_status",
        prompt_en="What is your current immigration status in the United States?",
        prompt_es="¿Cuál es su estatus migratorio actual en los Estados Unidos?",
    ),
    IntakeQuestion(
        field="case_type",
        prompt_en=(
            "Can you briefly describe what type of immigration help you need — "
            "for example, green card, work visa, asylum, citizenship, or something else?"
        ),
        prompt_es=(
            "¿Puede describirme brevemente qué tipo de ayuda migratoria necesita? "
            "Por ejemplo, tarjeta verde, visa de trabajo, asilo, ciudadanía u otro."
        ),
    ),
    IntakeQuestion(
        field="entry_date_us",
        prompt_en="When did you first enter the United States?",
        prompt_es="¿Cuándo ingresó por primera vez a los Estados Unidos?",
    ),
    IntakeQuestion(
        field="employer_sponsor",
        prompt_en="Is an employer sponsoring your visa or green card application?",
        prompt_es="¿Un empleador está patrocinando su visa o solicitud de tarjeta verde?",
        condition=_case_employment,
    ),
    IntakeQuestion(
        field="prior_applications",
        prompt_en=(
            "Have you previously filed any immigration applications, "
            "petitions, or cases with USCIS or an immigration court?"
        ),
        prompt_es=(
            "¿Ha presentado anteriormente alguna solicitud, petición o caso de "
            "inmigración ante USCIS o un tribunal de inmigración?"
        ),
    ),
    IntakeQuestion(
        field="prior_deportation",
        prompt_en="Have you ever been detained, deported, or received an order of removal?",
        prompt_es="¿Alguna vez ha sido detenido, deportado o recibido una orden de deportación?",
        condition=_urgency_medium_plus,
    ),
    IntakeQuestion(
        field="criminal_history",
        prompt_en=(
            "Have you ever been arrested or convicted of any crime? "
            "(This is a strict legal question — it does not affect your right to call us.)"
        ),
        prompt_es=(
            "¿Alguna vez ha sido arrestado o condenado por algún delito? "
            "(Es una pregunta legal estricta — no afecta su derecho a llamarnos.)"
        ),
        condition=_urgency_high_plus,
    ),
    IntakeQuestion(
        field="has_attorney",
        prompt_en="Are you currently working with any other immigration attorney or representative?",
        prompt_es="¿Actualmente está trabajando con algún otro abogado o representante de inmigración?",
    ),
    IntakeQuestion(
        field="family_in_us",
        prompt_en=(
            "Do you have any immediate family members who are US citizens or "
            "lawful permanent residents?"
        ),
        prompt_es=(
            "¿Tiene algún familiar directo que sea ciudadano estadounidense o "
            "residente permanente legal?"
        ),
    ),
    IntakeQuestion(
        field="email",
        prompt_en="What is the best email address to send you information and follow-ups?",
        prompt_es="¿Cuál es el mejor correo electrónico para enviarle información y seguimiento?",
    ),
    IntakeQuestion(
        field="preferred_contact_time",
        prompt_en="What is the best time of day to reach you if we need to follow up?",
        prompt_es="¿Cuál es el mejor horario para contactarle si necesitamos hacer seguimiento?",
    ),
]

# Build a field→question lookup for direct access
_FIELD_TO_QUESTION: dict[str, IntakeQuestion] = {q.field: q for q in INTAKE_QUESTIONS}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def next_question(state: CallState) -> IntakeQuestion | None:
    """
    Return the next unanswered question that passes its condition check.
    Returns None when all applicable questions have been answered.
    """
    for question in INTAKE_QUESTIONS:
        if state.intake.get(question.field):
            # Already answered
            continue
        if question.condition is not None and not question.condition(state):
            # Condition not met — skip for now
            continue
        return question
    return None


def build_next_question_hint(state: CallState) -> str:
    """
    Build a short directive for the LLM's extra_context, e.g.:
      "Next intake question to ask (EN): What is your current immigration status?"
    Returns empty string if all questions answered.
    """
    q = next_question(state)
    if q is None:
        if not state.intake_complete():
            return "All intake questions have been asked. Summarize what was collected and transition to the consultation pitch."
        return ""

    lang = state.language
    prompt = q.prompt_es if lang == "es" else q.prompt_en
    label = "ES" if lang == "es" else "EN"
    return (
        f"[Next intake question to ask ({label})]\n"
        f"Field: {q.field}\n"
        f"Ask: {prompt}\n"
        f"Weave this question naturally into your response — do not read it verbatim if it "
        f"sounds awkward after what the caller just said."
    )


def extract_field_from_response(field: str, text: str) -> str | None:
    """
    Lightweight heuristic extractor for simple fields from the caller's response.
    Used as a fast-path before running the full GPT extraction.
    Returns the extracted value or None.

    Note: This is intentionally simple — the authoritative extraction happens
    in ImmigrationAgent.extract_intake_data() (GPT-4o JSON mode) at call end.
    """
    text = text.strip()
    if not text or len(text) < 2:
        return None

    import re as _re

    # For yes/no fields, normalise to "yes" / "no"
    yn_fields = {"prior_applications", "prior_deportation", "criminal_history",
                 "has_attorney", "employer_sponsor", "family_in_us"}
    if field in yn_fields:
        lower = text.lower()
        if any(w in lower for w in ("yes", "sí", "si ", "correct", "affirmative", "yeah")):
            return "yes"
        if any(w in lower for w in ("no", "nope", "negative", "never", "no ")):
            return "no"
        return None  # Don't store ambiguous text for boolean fields

    # For name fields, strip common preambles then validate it looks like a name.
    if field in ("full_name", "first_name", "last_name"):
        _NON_NAMES = {
            "yes", "no", "hi", "hello", "hey", "okay", "ok", "sure", "yeah",
            "yep", "correct", "right", "uh", "um", "hmm", "uhh", "hm", "yea",
            "speaking",
        }
        # Strip preambles in a loop — handles "Yes. This is John" → "John"
        # Compound phrases must come before single words to match greedily.
        _PREAMBLES = _re.compile(
            r"^("
            r"(my name is|this is|i am|i'm|it'?s|the name is|speaking[,]?\s*(this is)?)\s*|"
            r"(yes|yeah|sure|okay|ok|hi|hello|hey)[,.]?\s*"
            r")",
            _re.IGNORECASE,
        )
        prev = None
        while prev != text:
            prev = text
            text = _PREAMBLES.sub("", text).strip().strip(".,!?").strip()

        clean = text.lower()
        if clean in _NON_NAMES or not clean:
            return None
        # Must have at least one alphabetic run of 2+ chars (a real word)
        if not _re.search(r"[a-zA-ZÀ-ú]{2,}", text):
            return None
        # Must be at least 2 chars (rejects "I", "A", etc.)
        if len(text) < 2:
            return None

    # For date fields, only accept strings that look like actual dates
    if field in ("entry_date_us", "date_of_birth"):
        if not _re.search(r'\d{4}[-/]\d{2}[-/]\d{2}|\d{1,2}[-/]\d{1,2}[-/]\d{2,4}', text):
            return None

    # For email field, only accept strings that look like an email address
    if field == "email":
        if not _re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', text.strip()):
            return None

    # For short free-text fields, return as-is if reasonable length
    if len(text) <= 200:
        return text

    return None
