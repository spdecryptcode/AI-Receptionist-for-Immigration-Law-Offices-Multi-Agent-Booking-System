"""
SQLAlchemy ORM models for all 12 database tables.

Extensions required (created in Alembic initial migration):
  - pgvector   → VECTOR type for semantic embeddings
  - pgcrypto   → PGP encryption for PII fields (handled at query layer)
  - uuid-ossp  → gen_random_uuid() default

Naming conventions:
  - All PKs:  UUID, server_default=gen_random_uuid()
  - All FKs:  UUID, with explicit ondelete behaviour
  - Timestamps: DateTime(timezone=True) → stored as UTC
  - Enums: native PostgreSQL ENUM via SQLAlchemy Enum type
"""

import enum
from datetime import datetime, date
from typing import Optional

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, relationship
from pgvector.sqlalchemy import Vector


# ---------------------------------------------------------------------------
# Base & shared helpers
# ---------------------------------------------------------------------------


class Base(DeclarativeBase):
    pass


def _uuid_pk():
    return Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )


def _now():
    return Column(DateTime(timezone=True), server_default=text("NOW()"), nullable=False)


def _updated_at():
    return Column(
        DateTime(timezone=True),
        server_default=text("NOW()"),
        onupdate=datetime.utcnow,
        nullable=False,
    )


# ---------------------------------------------------------------------------
# Enum definitions  (mirror DATABASE.md exactly)
# ---------------------------------------------------------------------------


class ClientStatus(str, enum.Enum):
    new_lead = "new_lead"
    intake_scheduled = "intake_scheduled"
    intake_complete = "intake_complete"
    active_client = "active_client"
    closed = "closed"
    do_not_contact = "do_not_contact"


class CaseType(str, enum.Enum):
    family_sponsorship = "family_sponsorship"
    employment_visa = "employment_visa"
    asylum = "asylum"
    removal_defense = "removal_defense"
    daca = "daca"
    tps = "tps"
    naturalization = "naturalization"
    other = "other"


class UrgencyLevel(str, enum.Enum):
    critical = "critical"
    high = "high"
    medium = "medium"
    routine = "routine"


class PetitionerRelationship(str, enum.Enum):
    spouse_usc = "spouse_usc"
    spouse_lpr = "spouse_lpr"
    parent_usc = "parent_usc"
    child_usc = "child_usc"
    sibling_usc = "sibling_usc"
    other = "other"


class PetitionerStatus(str, enum.Enum):
    us_citizen = "us_citizen"
    lpr = "lpr"
    unknown = "unknown"


class MaritalStatus(str, enum.Enum):
    single = "single"
    married = "married"
    divorced = "divorced"
    widowed = "widowed"
    separated = "separated"


class EducationLevel(str, enum.Enum):
    high_school = "high_school"
    bachelors = "bachelors"
    masters = "masters"
    doctorate = "doctorate"
    other = "other"


class EntryMethod(str, enum.Enum):
    legal_visa = "legal_visa"
    border_crossing_no_inspection = "border_crossing_no_inspection"
    visa_overstay = "visa_overstay"
    unknown = "unknown"


class PreferredContact(str, enum.Enum):
    phone = "phone"
    email = "email"
    text = "text"


class Channel(str, enum.Enum):
    phone = "phone"
    sms = "sms"
    whatsapp = "whatsapp"
    facebook = "facebook"
    instagram = "instagram"
    web_chat = "web_chat"


class Direction(str, enum.Enum):
    inbound = "inbound"
    outbound = "outbound"


class CallOutcome(str, enum.Enum):
    booking_made = "booking_made"
    transferred_to_staff = "transferred_to_staff"
    callback_requested = "callback_requested"
    info_only = "info_only"
    dropped = "dropped"
    voicemail = "voicemail"


class MessageRole(str, enum.Enum):
    caller = "caller"
    assistant = "assistant"
    system = "system"


class AppointmentType(str, enum.Enum):
    initial_consultation = "initial_consultation"
    follow_up = "follow_up"
    document_review = "document_review"
    court_prep = "court_prep"


class AppointmentStatus(str, enum.Enum):
    scheduled = "scheduled"
    confirmed = "confirmed"
    completed = "completed"
    cancelled = "cancelled"
    no_show = "no_show"
    rescheduled = "rescheduled"


class BookedVia(str, enum.Enum):
    ai_phone = "ai_phone"
    ai_social = "ai_social"
    manual = "manual"
    website = "website"


class CallStatus(str, enum.Enum):
    completed = "completed"
    no_answer = "no_answer"
    busy = "busy"
    failed = "failed"
    cancelled = "cancelled"


class QualificationStatus(str, enum.Enum):
    hot = "hot"
    warm = "warm"
    cold = "cold"


class RoutingRecommendation(str, enum.Enum):
    senior_attorney = "senior_attorney"
    junior_attorney = "junior_attorney"
    paralegal = "paralegal"
    follow_up_only = "follow_up_only"


class DocumentType(str, enum.Enum):
    passport = "passport"
    birth_certificate = "birth_certificate"
    visa_stamps = "visa_stamps"
    i94 = "i94"
    employment_letter = "employment_letter"
    tax_returns = "tax_returns"
    marriage_certificate = "marriage_certificate"
    divorce_decree = "divorce_decree"
    police_clearance = "police_clearance"
    medical_exam = "medical_exam"
    photos = "photos"
    other = "other"


class AlertType(str, enum.Enum):
    ice_detention = "ice_detention"
    court_date_imminent = "court_date_imminent"
    visa_expiring = "visa_expiring"
    asylum_deadline = "asylum_deadline"
    daca_expiring = "daca_expiring"
    ice_raid = "ice_raid"


class AlertAction(str, enum.Enum):
    transferred_to_attorney = "transferred_to_attorney"
    expedited_booking = "expedited_booking"
    sms_sent_to_staff = "sms_sent_to_staff"
    pending = "pending"


class ReferralSource(str, enum.Enum):
    google_ads = "google_ads"
    facebook_ads = "facebook_ads"
    instagram = "instagram"
    referral_client = "referral_client"
    referral_attorney = "referral_attorney"
    website = "website"
    walk_in = "walk_in"
    community_org = "community_org"
    other = "other"


class VoicemailFollowUpStatus(str, enum.Enum):
    pending = "pending"
    assigned = "assigned"
    completed = "completed"
    no_action = "no_action"


class CallbackStatus(str, enum.Enum):
    pending = "pending"
    in_progress = "in_progress"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


# ---------------------------------------------------------------------------
# 1. clients
# ---------------------------------------------------------------------------


class Client(Base):
    __tablename__ = "clients"

    id = _uuid_pk()
    ghl_contact_id = Column(String(255), unique=True, nullable=True)
    phone = Column(String(20), nullable=False, unique=True, index=True)
    email = Column(String(255), nullable=True)
    first_name = Column(String(100), nullable=True)
    last_name = Column(String(100), nullable=True)
    aliases = Column(String(255), nullable=True)
    # date_of_birth stored encrypted — application-layer encryption via pgcrypto
    date_of_birth = Column(String(255), nullable=True)  # encrypted ciphertext
    country_of_origin = Column(String(100), nullable=True)
    preferred_language = Column(String(20), nullable=False, server_default="en")
    current_address_city = Column(String(100), nullable=True)
    current_address_state = Column(String(100), nullable=True)
    client_status = Column(
        Enum(ClientStatus, name="client_status_enum"),
        nullable=False,
        server_default=ClientStatus.new_lead.value,
    )
    referral_source = Column(String(100), nullable=True)
    total_calls = Column(Integer, nullable=False, server_default="0")
    sms_consent = Column(Boolean, nullable=False, server_default="false")
    last_contact_at = Column(DateTime(timezone=True), nullable=True)
    created_at = _now()
    updated_at = _updated_at()
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    conversations = relationship("Conversation", back_populates="client")
    intakes = relationship("ImmigrationIntake", back_populates="client")
    appointments = relationship("Appointment", back_populates="client")
    call_logs = relationship("CallLog", back_populates="client")
    lead_scores = relationship("LeadScore", back_populates="client")
    document_checklists = relationship("DocumentChecklist", back_populates="client")
    urgency_alerts = relationship("UrgencyAlert", back_populates="client")
    referral_tracking = relationship("ReferralTracking", back_populates="client")
    voicemails = relationship("Voicemail", back_populates="client")
    callbacks = relationship("CallbackQueue", back_populates="client")


# ---------------------------------------------------------------------------
# 2. conversations
# ---------------------------------------------------------------------------


class Conversation(Base):
    __tablename__ = "conversations"

    id = _uuid_pk()
    client_id = Column(
        UUID(as_uuid=True),
        ForeignKey("clients.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    channel = Column(Enum(Channel, name="channel_enum"), nullable=False, server_default=Channel.phone.value)
    direction = Column(Enum(Direction, name="direction_enum"), nullable=False, server_default=Direction.inbound.value)
    twilio_call_sid = Column(String(255), unique=True, nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    ended_at = Column(DateTime(timezone=True), nullable=True)
    duration_seconds = Column(Integer, nullable=True)
    recording_consent = Column(Boolean, nullable=False, server_default="false")
    recording_url = Column(String(500), nullable=True)
    ai_model_used = Column(String(50), nullable=True)
    stt_service = Column(String(50), nullable=True)
    tts_service = Column(String(50), nullable=True)
    call_outcome = Column(Enum(CallOutcome, name="call_outcome_enum"), nullable=True)
    transferred_to = Column(String(100), nullable=True)
    ai_confidence_avg = Column(Float, nullable=True)
    created_at = _now()

    # Relationships
    client = relationship("Client", back_populates="conversations")
    messages = relationship(
        "ConversationMessage",
        back_populates="conversation",
        order_by="ConversationMessage.sequence_number",
    )
    intakes = relationship("ImmigrationIntake", back_populates="conversation")
    call_logs = relationship("CallLog", back_populates="conversation")
    urgency_alerts = relationship("UrgencyAlert", back_populates="conversation")
    voicemails = relationship("Voicemail", back_populates="conversation")
    callbacks = relationship("CallbackQueue", back_populates="previous_conversation")


# ---------------------------------------------------------------------------
# 3. immigration_intake
# ---------------------------------------------------------------------------


class ImmigrationIntake(Base):
    __tablename__ = "immigration_intake"

    id = _uuid_pk()
    client_id = Column(
        UUID(as_uuid=True),
        ForeignKey("clients.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    conversation_id = Column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="SET NULL"),
        nullable=True,
    )

    # --- Tier 1: Urgency triage ---
    is_detained = Column(Boolean, nullable=False, server_default="false")
    has_court_date = Column(Boolean, nullable=False, server_default="false")
    court_date = Column(Date, nullable=True)
    court_location = Column(String(255), nullable=True)
    visa_expiration_date = Column(Date, nullable=True)
    urgency_level = Column(
        Enum(UrgencyLevel, name="urgency_level_enum"),
        nullable=True,
    )
    urgency_reason = Column(Text, nullable=True)

    # --- Tier 1: Case classification ---
    case_type = Column(Enum(CaseType, name="case_type_enum"), nullable=True)
    current_immigration_status = Column(String(100), nullable=True)
    a_number = Column(String(50), nullable=True)  # encrypted ciphertext

    # --- Tier 2: Family-based ---
    petitioner_relationship = Column(
        Enum(PetitionerRelationship, name="petitioner_relationship_enum"), nullable=True
    )
    petitioner_status = Column(
        Enum(PetitionerStatus, name="petitioner_status_enum"), nullable=True
    )
    marital_status = Column(Enum(MaritalStatus, name="marital_status_enum"), nullable=True)
    num_dependents = Column(Integer, nullable=False, server_default="0")
    dependents_in_us = Column(Boolean, nullable=True)

    # --- Tier 2: Employment-based ---
    employer_name = Column(String(255), nullable=True)
    job_title = Column(String(255), nullable=True)
    salary_range = Column(String(50), nullable=True)
    employer_willing_to_sponsor = Column(Boolean, nullable=True)
    education_level = Column(Enum(EducationLevel, name="education_level_enum"), nullable=True)
    years_experience = Column(Integer, nullable=True)

    # --- Tier 2: Asylum ---
    country_of_persecution = Column(String(100), nullable=True)
    arrival_date_us = Column(Date, nullable=True)
    asylum_filing_deadline = Column(Date, nullable=True)
    has_filed_asylum = Column(Boolean, nullable=False, server_default="false")
    persecution_type = Column(String(100), nullable=True)  # general category only

    # --- Tier 2: Removal/deportation ---
    has_nta = Column(Boolean, nullable=False, server_default="false")
    removal_hearing_date = Column(Date, nullable=True)
    has_bond_hearing = Column(Boolean, nullable=False, server_default="false")
    bond_hearing_date = Column(Date, nullable=True)
    prior_deportation = Column(Boolean, nullable=False, server_default="false")

    # --- Tier 2: General qualifying fields ---
    entry_method = Column(Enum(EntryMethod, name="entry_method_enum"), nullable=True)
    years_in_us = Column(Integer, nullable=True)
    has_criminal_record = Column(Boolean, nullable=True)
    has_prior_visa_denial = Column(Boolean, nullable=True)
    has_prior_immigration_case = Column(Boolean, nullable=True)
    us_family_connections = Column(Boolean, nullable=True)

    # --- Tier 3: Nice-to-have ---
    passport_number = Column(String(100), nullable=True)  # encrypted ciphertext
    passport_country = Column(String(100), nullable=True)
    preferred_contact_method = Column(
        Enum(PreferredContact, name="preferred_contact_enum"), nullable=True
    )
    best_callback_time = Column(String(50), nullable=True)
    budget_discussed = Column(Boolean, nullable=False, server_default="false")
    timeline_expectation = Column(String(100), nullable=True)

    # --- Flexible overflow ---
    extra_data = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    intake_completeness_pct = Column(Integer, nullable=False, server_default="0")
    fields_deferred_to_attorney = Column(ARRAY(Text), nullable=True)
    created_at = _now()
    updated_at = _updated_at()

    # Relationships
    client = relationship("Client", back_populates="intakes")
    conversation = relationship("Conversation", back_populates="intakes")
    appointments = relationship("Appointment", back_populates="intake")
    lead_scores = relationship("LeadScore", back_populates="intake")
    document_checklists = relationship("DocumentChecklist", back_populates="intake")


# ---------------------------------------------------------------------------
# 4. conversation_messages
# ---------------------------------------------------------------------------


class ConversationMessage(Base):
    __tablename__ = "conversation_messages"

    id = _uuid_pk()
    conversation_id = Column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    sequence_number = Column(Integer, nullable=False)
    role = Column(Enum(MessageRole, name="message_role_enum"), nullable=False)
    content = Column(Text, nullable=False)
    audio_duration_ms = Column(Integer, nullable=True)
    stt_confidence = Column(Float, nullable=True)
    ai_confidence = Column(Float, nullable=True)
    tokens_used = Column(Integer, nullable=True)
    latency_ms = Column(Integer, nullable=True)
    # pgvector column — requires pgvector extension
    embedding = Column(Vector(1536), nullable=True)
    timestamp = Column(DateTime(timezone=True), server_default=text("NOW()"), nullable=False)

    # Relationships
    conversation = relationship("Conversation", back_populates="messages")


# ---------------------------------------------------------------------------
# 5. appointments
# ---------------------------------------------------------------------------


class Appointment(Base):
    __tablename__ = "appointments"

    id = _uuid_pk()
    client_id = Column(
        UUID(as_uuid=True),
        ForeignKey("clients.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    intake_id = Column(
        UUID(as_uuid=True),
        ForeignKey("immigration_intake.id", ondelete="SET NULL"),
        nullable=True,
    )
    ghl_appointment_id = Column(String(255), unique=True, nullable=True)
    google_calendar_event_id = Column(String(500), unique=True, nullable=True)
    appointment_type = Column(
        Enum(AppointmentType, name="appointment_type_enum"), nullable=False
    )
    scheduled_at = Column(DateTime(timezone=True), nullable=False)
    duration_minutes = Column(Integer, nullable=False, server_default="60")
    attorney_name = Column(String(100), nullable=True)
    status = Column(
        Enum(AppointmentStatus, name="appointment_status_enum"),
        nullable=False,
        server_default=AppointmentStatus.scheduled.value,
    )
    reminder_24h_sent = Column(Boolean, nullable=False, server_default="false")
    reminder_1h_sent = Column(Boolean, nullable=False, server_default="false")
    reminder_15m_sent = Column(Boolean, nullable=False, server_default="false")
    meeting_notes = Column(Text, nullable=True)
    booked_via = Column(Enum(BookedVia, name="booked_via_enum"), nullable=True)
    created_at = _now()
    updated_at = _updated_at()

    # Relationships
    client = relationship("Client", back_populates="appointments")
    intake = relationship("ImmigrationIntake", back_populates="appointments")


# ---------------------------------------------------------------------------
# 6. call_logs
# ---------------------------------------------------------------------------


class CallLog(Base):
    __tablename__ = "call_logs"

    id = _uuid_pk()
    conversation_id = Column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    client_id = Column(
        UUID(as_uuid=True),
        ForeignKey("clients.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    twilio_call_sid = Column(String(255), unique=True, nullable=False)
    from_number = Column(String(20), nullable=True)
    to_number = Column(String(20), nullable=True)
    call_direction = Column(Enum(Direction, name="call_direction_enum"), nullable=False)
    call_status = Column(Enum(CallStatus, name="call_status_enum"), nullable=True)
    duration_seconds = Column(Integer, nullable=True)
    hit_soft_limit = Column(Boolean, nullable=False, server_default="false")
    hit_hard_limit = Column(Boolean, nullable=False, server_default="false")
    ivr_path = Column(String(255), nullable=True)
    was_transferred = Column(Boolean, nullable=False, server_default="false")
    transfer_destination = Column(String(100), nullable=True)
    ai_summary = Column(Text, nullable=True)
    ai_action_items = Column(ARRAY(Text), nullable=True)
    sentiment_score = Column(Float, nullable=True)
    caller_frustration_detected = Column(Boolean, nullable=False, server_default="false")
    fields_collected_count = Column(Integer, nullable=False, server_default="0")
    fields_missing = Column(ARRAY(Text), nullable=True)
    cost_deepgram = Column(Numeric(10, 6), nullable=True)
    cost_openai = Column(Numeric(10, 6), nullable=True)
    cost_elevenlabs = Column(Numeric(10, 6), nullable=True)
    cost_twilio = Column(Numeric(10, 6), nullable=True)
    cost_total = Column(Numeric(10, 6), nullable=True)
    created_at = _now()

    # Relationships
    conversation = relationship("Conversation", back_populates="call_logs")
    client = relationship("Client", back_populates="call_logs")


# ---------------------------------------------------------------------------
# 7. lead_scores
# ---------------------------------------------------------------------------


class LeadScore(Base):
    __tablename__ = "lead_scores"

    id = _uuid_pk()
    client_id = Column(
        UUID(as_uuid=True),
        ForeignKey("clients.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    intake_id = Column(
        UUID(as_uuid=True),
        ForeignKey("immigration_intake.id", ondelete="SET NULL"),
        nullable=True,
    )
    score = Column(Integer, nullable=False)
    qualification_status = Column(
        Enum(QualificationStatus, name="qualification_status_enum"), nullable=False
    )
    score_breakdown = Column(JSONB, nullable=True)
    routing_recommendation = Column(
        Enum(RoutingRecommendation, name="routing_recommendation_enum"), nullable=True
    )
    reasoning = Column(Text, nullable=True)
    calculated_at = _now()

    # Relationships
    client = relationship("Client", back_populates="lead_scores")
    intake = relationship("ImmigrationIntake", back_populates="lead_scores")


# ---------------------------------------------------------------------------
# 8. document_checklist
# ---------------------------------------------------------------------------


class DocumentChecklist(Base):
    __tablename__ = "document_checklist"

    id = _uuid_pk()
    client_id = Column(
        UUID(as_uuid=True),
        ForeignKey("clients.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    intake_id = Column(
        UUID(as_uuid=True),
        ForeignKey("immigration_intake.id", ondelete="CASCADE"),
        nullable=False,
    )
    document_type = Column(
        Enum(DocumentType, name="document_type_enum"), nullable=False
    )
    has_document = Column(Boolean, nullable=True)  # NULL = not asked
    notes = Column(String(255), nullable=True)
    created_at = _now()

    # Relationships
    client = relationship("Client", back_populates="document_checklists")
    intake = relationship("ImmigrationIntake", back_populates="document_checklists")


# ---------------------------------------------------------------------------
# 9. urgency_alerts
# ---------------------------------------------------------------------------


class UrgencyAlert(Base):
    __tablename__ = "urgency_alerts"

    id = _uuid_pk()
    client_id = Column(
        UUID(as_uuid=True),
        ForeignKey("clients.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    conversation_id = Column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    alert_type = Column(Enum(AlertType, name="alert_type_enum"), nullable=False)
    alert_level = Column(Enum(UrgencyLevel, name="alert_level_enum"), nullable=False)
    details = Column(Text, nullable=True)
    deadline_date = Column(Date, nullable=True)
    action_taken = Column(
        Enum(AlertAction, name="alert_action_enum"),
        nullable=False,
        server_default=AlertAction.pending.value,
    )
    resolved = Column(Boolean, nullable=False, server_default="false")
    resolved_by = Column(String(100), nullable=True)
    created_at = _now()
    resolved_at = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    client = relationship("Client", back_populates="urgency_alerts")
    conversation = relationship("Conversation", back_populates="urgency_alerts")


# ---------------------------------------------------------------------------
# 10. referral_tracking
# ---------------------------------------------------------------------------


class ReferralTracking(Base):
    __tablename__ = "referral_tracking"

    id = _uuid_pk()
    client_id = Column(
        UUID(as_uuid=True),
        ForeignKey("clients.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source = Column(Enum(ReferralSource, name="referral_source_enum"), nullable=True)
    source_detail = Column(String(255), nullable=True)
    first_contact_channel = Column(Enum(Channel, name="first_contact_channel_enum"), nullable=True)
    converted_to_booking = Column(Boolean, nullable=False, server_default="false")
    converted_to_client = Column(Boolean, nullable=False, server_default="false")
    created_at = _now()

    # Relationships
    client = relationship("Client", back_populates="referral_tracking")


# ---------------------------------------------------------------------------
# 11. voicemails
# ---------------------------------------------------------------------------


class Voicemail(Base):
    __tablename__ = "voicemails"

    id = _uuid_pk()
    call_sid = Column(String(255), nullable=False)
    client_id = Column(
        UUID(as_uuid=True),
        ForeignKey("clients.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    conversation_id = Column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="SET NULL"),
        nullable=True,
    )
    caller_phone = Column(String(20), nullable=False)
    recording_url = Column(String(500), nullable=False)
    duration_seconds = Column(Integer, nullable=True)
    transcript = Column(Text, nullable=True)
    ai_summary = Column(Text, nullable=True)
    urgency = Column(
        Enum(UrgencyLevel, name="voicemail_urgency_enum"),
        nullable=False,
        server_default=UrgencyLevel.routine.value,
    )
    follow_up_status = Column(
        Enum(VoicemailFollowUpStatus, name="voicemail_follow_up_status_enum"),
        nullable=False,
        server_default=VoicemailFollowUpStatus.pending.value,
    )
    assigned_to = Column(String(100), nullable=True)
    ghl_task_id = Column(String(255), nullable=True)
    created_at = _now()

    # Relationships
    client = relationship("Client", back_populates="voicemails")
    conversation = relationship("Conversation", back_populates="voicemails")


# ---------------------------------------------------------------------------
# 12. callback_queue
# ---------------------------------------------------------------------------


class CallbackQueue(Base):
    __tablename__ = "callback_queue"

    id = _uuid_pk()
    client_id = Column(
        UUID(as_uuid=True),
        ForeignKey("clients.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    caller_phone = Column(String(20), nullable=False)
    previous_conversation_id = Column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="SET NULL"),
        nullable=True,
    )
    preferred_callback_time = Column(DateTime(timezone=True), nullable=True)
    reason = Column(String(255), nullable=True)
    status = Column(
        Enum(CallbackStatus, name="callback_status_enum"),
        nullable=False,
        server_default=CallbackStatus.pending.value,
    )
    attempts = Column(Integer, nullable=False, server_default="0")
    max_attempts = Column(Integer, nullable=False, server_default="3")
    last_attempt_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    outbound_call_sid = Column(String(255), nullable=True)
    created_at = _now()

    # Relationships
    client = relationship("Client", back_populates="callbacks")
    previous_conversation = relationship("Conversation", back_populates="callbacks")
