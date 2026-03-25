"""Initial schema — all 12 tables with indexes

Revision ID: 0001_initial
Revises:
Create Date: 2024-01-01 00:00:00.000000
"""

from typing import Sequence, Union
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # Extensions
    # ------------------------------------------------------------------
    op.execute("CREATE EXTENSION IF NOT EXISTS \"uuid-ossp\"")
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # ------------------------------------------------------------------
    # Enum types
    # ------------------------------------------------------------------
    client_status = postgresql.ENUM(
        "new_lead", "intake_scheduled", "intake_complete",
        "active_client", "closed", "do_not_contact",
        name="client_status_enum",
    )
    client_status.create(op.get_bind())

    case_type = postgresql.ENUM(
        "family_sponsorship", "employment_visa", "asylum",
        "removal_defense", "daca", "tps", "naturalization", "other",
        name="case_type_enum",
    )
    case_type.create(op.get_bind())

    urgency_level = postgresql.ENUM(
        "critical", "high", "medium", "routine",
        name="urgency_level_enum",
    )
    urgency_level.create(op.get_bind())

    # shared alert_level uses urgency_level — create separate to avoid collision
    alert_level = postgresql.ENUM(
        "critical", "high", "medium", "routine",
        name="alert_level_enum",
    )
    alert_level.create(op.get_bind())

    voicemail_urgency = postgresql.ENUM(
        "critical", "high", "medium", "routine",
        name="voicemail_urgency_enum",
    )
    voicemail_urgency.create(op.get_bind())

    channel = postgresql.ENUM(
        "phone", "sms", "whatsapp", "facebook", "instagram", "web_chat",
        name="channel_enum",
    )
    channel.create(op.get_bind())

    direction = postgresql.ENUM(
        "inbound", "outbound",
        name="direction_enum",
    )
    direction.create(op.get_bind())

    call_direction = postgresql.ENUM(
        "inbound", "outbound",
        name="call_direction_enum",
    )
    call_direction.create(op.get_bind())

    call_outcome = postgresql.ENUM(
        "booking_made", "transferred_to_staff", "callback_requested",
        "info_only", "dropped", "voicemail",
        name="call_outcome_enum",
    )
    call_outcome.create(op.get_bind())

    message_role = postgresql.ENUM(
        "caller", "assistant", "system",
        name="message_role_enum",
    )
    message_role.create(op.get_bind())

    appointment_type = postgresql.ENUM(
        "initial_consultation", "follow_up", "document_review", "court_prep",
        name="appointment_type_enum",
    )
    appointment_type.create(op.get_bind())

    appointment_status = postgresql.ENUM(
        "scheduled", "confirmed", "completed", "cancelled", "no_show", "rescheduled",
        name="appointment_status_enum",
    )
    appointment_status.create(op.get_bind())

    booked_via = postgresql.ENUM(
        "ai_phone", "ai_social", "manual", "website",
        name="booked_via_enum",
    )
    booked_via.create(op.get_bind())

    call_status = postgresql.ENUM(
        "completed", "no_answer", "busy", "failed", "cancelled",
        name="call_status_enum",
    )
    call_status.create(op.get_bind())

    qualification_status = postgresql.ENUM(
        "hot", "warm", "cold",
        name="qualification_status_enum",
    )
    qualification_status.create(op.get_bind())

    routing_recommendation = postgresql.ENUM(
        "senior_attorney", "junior_attorney", "paralegal", "follow_up_only",
        name="routing_recommendation_enum",
    )
    routing_recommendation.create(op.get_bind())

    document_type = postgresql.ENUM(
        "passport", "birth_certificate", "visa_stamps", "i94",
        "employment_letter", "tax_returns", "marriage_certificate",
        "divorce_decree", "police_clearance", "medical_exam", "photos", "other",
        name="document_type_enum",
    )
    document_type.create(op.get_bind())

    alert_type = postgresql.ENUM(
        "ice_detention", "court_date_imminent", "visa_expiring",
        "asylum_deadline", "daca_expiring", "ice_raid",
        name="alert_type_enum",
    )
    alert_type.create(op.get_bind())

    alert_action = postgresql.ENUM(
        "transferred_to_attorney", "expedited_booking",
        "sms_sent_to_staff", "pending",
        name="alert_action_enum",
    )
    alert_action.create(op.get_bind())

    referral_source = postgresql.ENUM(
        "google_ads", "facebook_ads", "instagram", "referral_client",
        "referral_attorney", "website", "walk_in", "community_org", "other",
        name="referral_source_enum",
    )
    referral_source.create(op.get_bind())

    first_contact_channel = postgresql.ENUM(
        "phone", "sms", "whatsapp", "facebook", "instagram", "web_chat",
        name="first_contact_channel_enum",
    )
    first_contact_channel.create(op.get_bind())

    voicemail_follow_up_status = postgresql.ENUM(
        "pending", "assigned", "completed", "no_action",
        name="voicemail_follow_up_status_enum",
    )
    voicemail_follow_up_status.create(op.get_bind())

    callback_status = postgresql.ENUM(
        "pending", "in_progress", "completed", "failed", "cancelled",
        name="callback_status_enum",
    )
    callback_status.create(op.get_bind())

    petitioner_relationship = postgresql.ENUM(
        "spouse_usc", "spouse_lpr", "parent_usc", "child_usc", "sibling_usc", "other",
        name="petitioner_relationship_enum",
    )
    petitioner_relationship.create(op.get_bind())

    petitioner_status = postgresql.ENUM(
        "us_citizen", "lpr", "unknown",
        name="petitioner_status_enum",
    )
    petitioner_status.create(op.get_bind())

    marital_status = postgresql.ENUM(
        "single", "married", "divorced", "widowed", "separated",
        name="marital_status_enum",
    )
    marital_status.create(op.get_bind())

    education_level = postgresql.ENUM(
        "high_school", "bachelors", "masters", "doctorate", "other",
        name="education_level_enum",
    )
    education_level.create(op.get_bind())

    entry_method = postgresql.ENUM(
        "legal_visa", "border_crossing_no_inspection", "visa_overstay", "unknown",
        name="entry_method_enum",
    )
    entry_method.create(op.get_bind())

    preferred_contact = postgresql.ENUM(
        "phone", "email", "text",
        name="preferred_contact_enum",
    )
    preferred_contact.create(op.get_bind())

    # ------------------------------------------------------------------
    # 1. clients
    # ------------------------------------------------------------------
    op.create_table(
        "clients",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("ghl_contact_id", sa.String(255), unique=True, nullable=True),
        sa.Column("phone", sa.String(20), nullable=False),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column("first_name", sa.String(100), nullable=True),
        sa.Column("last_name", sa.String(100), nullable=True),
        sa.Column("aliases", sa.String(255), nullable=True),
        sa.Column("date_of_birth", sa.String(255), nullable=True),
        sa.Column("country_of_origin", sa.String(100), nullable=True),
        sa.Column("preferred_language", sa.String(20), nullable=False, server_default="en"),
        sa.Column("current_address_city", sa.String(100), nullable=True),
        sa.Column("current_address_state", sa.String(100), nullable=True),
        sa.Column("client_status", postgresql.ENUM(name="client_status_enum", create_type=False), nullable=False, server_default="new_lead"),
        sa.Column("referral_source", sa.String(100), nullable=True),
        sa.Column("total_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sms_consent", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("last_contact_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("idx_clients_phone", "clients", ["phone"], unique=True)
    op.create_index("idx_clients_ghl", "clients", ["ghl_contact_id"], unique=True, postgresql_where=sa.text("ghl_contact_id IS NOT NULL"))

    # ------------------------------------------------------------------
    # 2. conversations
    # ------------------------------------------------------------------
    op.create_table(
        "conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("client_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("clients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("channel", postgresql.ENUM(name="channel_enum", create_type=False), nullable=False, server_default="phone"),
        sa.Column("direction", postgresql.ENUM(name="direction_enum", create_type=False), nullable=False, server_default="inbound"),
        sa.Column("twilio_call_sid", sa.String(255), unique=True, nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("recording_consent", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("recording_url", sa.String(500), nullable=True),
        sa.Column("ai_model_used", sa.String(50), nullable=True),
        sa.Column("stt_service", sa.String(50), nullable=True),
        sa.Column("tts_service", sa.String(50), nullable=True),
        sa.Column("call_outcome", postgresql.ENUM(name="call_outcome_enum", create_type=False), nullable=True),
        sa.Column("transferred_to", sa.String(100), nullable=True),
        sa.Column("ai_confidence_avg", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
    )
    op.create_index("idx_conversations_client", "conversations", ["client_id"])
    op.create_index("idx_conversations_call_sid", "conversations", ["twilio_call_sid"], unique=True, postgresql_where=sa.text("twilio_call_sid IS NOT NULL"))

    # ------------------------------------------------------------------
    # 3. immigration_intake
    # ------------------------------------------------------------------
    op.create_table(
        "immigration_intake",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("client_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("clients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True),
        # Tier 1
        sa.Column("is_detained", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("has_court_date", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("court_date", sa.Date(), nullable=True),
        sa.Column("court_location", sa.String(255), nullable=True),
        sa.Column("visa_expiration_date", sa.Date(), nullable=True),
        sa.Column("urgency_level", postgresql.ENUM(name="urgency_level_enum", create_type=False), nullable=True),
        sa.Column("urgency_reason", sa.Text(), nullable=True),
        sa.Column("case_type", postgresql.ENUM(name="case_type_enum", create_type=False), nullable=True),
        sa.Column("current_immigration_status", sa.String(100), nullable=True),
        sa.Column("a_number", sa.String(50), nullable=True),
        # Tier 2 — Family
        sa.Column("petitioner_relationship", postgresql.ENUM(name="petitioner_relationship_enum", create_type=False), nullable=True),
        sa.Column("petitioner_status", postgresql.ENUM(name="petitioner_status_enum", create_type=False), nullable=True),
        sa.Column("marital_status", postgresql.ENUM(name="marital_status_enum", create_type=False), nullable=True),
        sa.Column("num_dependents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("dependents_in_us", sa.Boolean(), nullable=True),
        # Tier 2 — Employment
        sa.Column("employer_name", sa.String(255), nullable=True),
        sa.Column("job_title", sa.String(255), nullable=True),
        sa.Column("salary_range", sa.String(50), nullable=True),
        sa.Column("employer_willing_to_sponsor", sa.Boolean(), nullable=True),
        sa.Column("education_level", postgresql.ENUM(name="education_level_enum", create_type=False), nullable=True),
        sa.Column("years_experience", sa.Integer(), nullable=True),
        # Tier 2 — Asylum
        sa.Column("country_of_persecution", sa.String(100), nullable=True),
        sa.Column("arrival_date_us", sa.Date(), nullable=True),
        sa.Column("asylum_filing_deadline", sa.Date(), nullable=True),
        sa.Column("has_filed_asylum", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("persecution_type", sa.String(100), nullable=True),
        # Tier 2 — Removal
        sa.Column("has_nta", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("removal_hearing_date", sa.Date(), nullable=True),
        sa.Column("has_bond_hearing", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("bond_hearing_date", sa.Date(), nullable=True),
        sa.Column("prior_deportation", sa.Boolean(), nullable=False, server_default="false"),
        # Tier 2 — General
        sa.Column("entry_method", postgresql.ENUM(name="entry_method_enum", create_type=False), nullable=True),
        sa.Column("years_in_us", sa.Integer(), nullable=True),
        sa.Column("has_criminal_record", sa.Boolean(), nullable=True),
        sa.Column("has_prior_visa_denial", sa.Boolean(), nullable=True),
        sa.Column("has_prior_immigration_case", sa.Boolean(), nullable=True),
        sa.Column("us_family_connections", sa.Boolean(), nullable=True),
        # Tier 3
        sa.Column("passport_number", sa.String(100), nullable=True),
        sa.Column("passport_country", sa.String(100), nullable=True),
        sa.Column("preferred_contact_method", postgresql.ENUM(name="preferred_contact_enum", create_type=False), nullable=True),
        sa.Column("best_callback_time", sa.String(50), nullable=True),
        sa.Column("budget_discussed", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("timeline_expectation", sa.String(100), nullable=True),
        # Overflow
        sa.Column("extra_data", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("intake_completeness_pct", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("fields_deferred_to_attorney", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
    )
    op.create_index("idx_intake_client", "immigration_intake", ["client_id"])
    op.create_index(
        "idx_intake_urgency", "immigration_intake", ["urgency_level"],
        postgresql_where=sa.text("urgency_level IN ('critical','high')")
    )

    # ------------------------------------------------------------------
    # 4. conversation_messages
    # ------------------------------------------------------------------
    op.create_table(
        "conversation_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sequence_number", sa.Integer(), nullable=False),
        sa.Column("role", postgresql.ENUM(name="message_role_enum", create_type=False), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("audio_duration_ms", sa.Integer(), nullable=True),
        sa.Column("stt_confidence", sa.Float(), nullable=True),
        sa.Column("ai_confidence", sa.Float(), nullable=True),
        sa.Column("tokens_used", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
    )
    # Add vector column separately (requires pgvector extension, already created above)
    op.execute("ALTER TABLE conversation_messages ADD COLUMN IF NOT EXISTS embedding vector(1536)")
    op.create_index("idx_messages_conversation", "conversation_messages", ["conversation_id", "sequence_number"])
    op.execute(
        "CREATE INDEX idx_messages_embedding ON conversation_messages "
        "USING ivfflat(embedding vector_cosine_ops) WITH (lists = 100)"
    )

    # ------------------------------------------------------------------
    # 5. appointments
    # ------------------------------------------------------------------
    op.create_table(
        "appointments",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("client_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("clients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("intake_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("immigration_intake.id", ondelete="SET NULL"), nullable=True),
        sa.Column("ghl_appointment_id", sa.String(255), unique=True, nullable=True),
        sa.Column("google_calendar_event_id", sa.String(500), unique=True, nullable=True),
        sa.Column("appointment_type", postgresql.ENUM(name="appointment_type_enum", create_type=False), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_minutes", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("attorney_name", sa.String(100), nullable=True),
        sa.Column("status", postgresql.ENUM(name="appointment_status_enum", create_type=False), nullable=False, server_default="scheduled"),
        sa.Column("reminder_24h_sent", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("reminder_1h_sent", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("reminder_15m_sent", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("meeting_notes", sa.Text(), nullable=True),
        sa.Column("booked_via", postgresql.ENUM(name="booked_via_enum", create_type=False), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
    )
    op.create_index("idx_appointments_client", "appointments", ["client_id"])
    op.create_index(
        "idx_appointments_scheduled", "appointments", ["scheduled_at"],
        postgresql_where=sa.text("status = 'scheduled'")
    )

    # ------------------------------------------------------------------
    # 6. call_logs
    # ------------------------------------------------------------------
    op.create_table(
        "call_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("client_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("clients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("twilio_call_sid", sa.String(255), unique=True, nullable=False),
        sa.Column("from_number", sa.String(20), nullable=True),
        sa.Column("to_number", sa.String(20), nullable=True),
        sa.Column("call_direction", postgresql.ENUM(name="call_direction_enum", create_type=False), nullable=False),
        sa.Column("call_status", postgresql.ENUM(name="call_status_enum", create_type=False), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("hit_soft_limit", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("hit_hard_limit", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("ivr_path", sa.String(255), nullable=True),
        sa.Column("was_transferred", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("transfer_destination", sa.String(100), nullable=True),
        sa.Column("ai_summary", sa.Text(), nullable=True),
        sa.Column("ai_action_items", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("sentiment_score", sa.Float(), nullable=True),
        sa.Column("caller_frustration_detected", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("fields_collected_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("fields_missing", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("cost_deepgram", sa.Numeric(10, 6), nullable=True),
        sa.Column("cost_openai", sa.Numeric(10, 6), nullable=True),
        sa.Column("cost_elevenlabs", sa.Numeric(10, 6), nullable=True),
        sa.Column("cost_twilio", sa.Numeric(10, 6), nullable=True),
        sa.Column("cost_total", sa.Numeric(10, 6), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
    )

    # ------------------------------------------------------------------
    # 7. lead_scores
    # ------------------------------------------------------------------
    op.create_table(
        "lead_scores",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("client_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("clients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("intake_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("immigration_intake.id", ondelete="SET NULL"), nullable=True),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("qualification_status", postgresql.ENUM(name="qualification_status_enum", create_type=False), nullable=False),
        sa.Column("score_breakdown", postgresql.JSONB(), nullable=True),
        sa.Column("routing_recommendation", postgresql.ENUM(name="routing_recommendation_enum", create_type=False), nullable=True),
        sa.Column("reasoning", sa.Text(), nullable=True),
        sa.Column("calculated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
    )
    op.create_index("idx_lead_scores_status", "lead_scores", ["qualification_status"])

    # ------------------------------------------------------------------
    # 8. document_checklist
    # ------------------------------------------------------------------
    op.create_table(
        "document_checklist",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("client_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("clients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("intake_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("immigration_intake.id", ondelete="CASCADE"), nullable=False),
        sa.Column("document_type", postgresql.ENUM(name="document_type_enum", create_type=False), nullable=False),
        sa.Column("has_document", sa.Boolean(), nullable=True),
        sa.Column("notes", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
    )

    # ------------------------------------------------------------------
    # 9. urgency_alerts
    # ------------------------------------------------------------------
    op.create_table(
        "urgency_alerts",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("client_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("clients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("alert_type", postgresql.ENUM(name="alert_type_enum", create_type=False), nullable=False),
        sa.Column("alert_level", postgresql.ENUM(name="alert_level_enum", create_type=False), nullable=False),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("deadline_date", sa.Date(), nullable=True),
        sa.Column("action_taken", postgresql.ENUM(name="alert_action_enum", create_type=False), nullable=False, server_default="pending"),
        sa.Column("resolved", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("resolved_by", sa.String(100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "idx_alerts_unresolved", "urgency_alerts", ["alert_level"],
        postgresql_where=sa.text("resolved = FALSE")
    )

    # ------------------------------------------------------------------
    # 10. referral_tracking
    # ------------------------------------------------------------------
    op.create_table(
        "referral_tracking",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("client_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("clients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source", postgresql.ENUM(name="referral_source_enum", create_type=False), nullable=True),
        sa.Column("source_detail", sa.String(255), nullable=True),
        sa.Column("first_contact_channel", postgresql.ENUM(name="first_contact_channel_enum", create_type=False), nullable=True),
        sa.Column("converted_to_booking", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("converted_to_client", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
    )

    # ------------------------------------------------------------------
    # 11. voicemails
    # ------------------------------------------------------------------
    op.create_table(
        "voicemails",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("call_sid", sa.String(255), nullable=False),
        sa.Column("client_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("clients.id", ondelete="SET NULL"), nullable=True),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True),
        sa.Column("caller_phone", sa.String(20), nullable=False),
        sa.Column("recording_url", sa.String(500), nullable=False),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("transcript", sa.Text(), nullable=True),
        sa.Column("ai_summary", sa.Text(), nullable=True),
        sa.Column("urgency", postgresql.ENUM(name="voicemail_urgency_enum", create_type=False), nullable=False, server_default="routine"),
        sa.Column("follow_up_status", postgresql.ENUM(name="voicemail_follow_up_status_enum", create_type=False), nullable=False, server_default="pending"),
        sa.Column("assigned_to", sa.String(100), nullable=True),
        sa.Column("ghl_task_id", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
    )
    op.create_index(
        "idx_voicemails_pending", "voicemails", ["follow_up_status"],
        postgresql_where=sa.text("follow_up_status = 'pending'")
    )

    # ------------------------------------------------------------------
    # 12. callback_queue
    # ------------------------------------------------------------------
    op.create_table(
        "callback_queue",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("client_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("clients.id", ondelete="SET NULL"), nullable=True),
        sa.Column("caller_phone", sa.String(20), nullable=False),
        sa.Column("previous_conversation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True),
        sa.Column("preferred_callback_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.String(255), nullable=True),
        sa.Column("status", postgresql.ENUM(name="callback_status_enum", create_type=False), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outbound_call_sid", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
    )


def downgrade() -> None:
    # Drop tables in reverse FK order
    op.drop_table("callback_queue")
    op.drop_table("voicemails")
    op.drop_table("referral_tracking")
    op.drop_table("urgency_alerts")
    op.drop_table("document_checklist")
    op.drop_table("lead_scores")
    op.drop_table("call_logs")
    op.drop_table("appointments")
    op.drop_table("conversation_messages")
    op.drop_table("immigration_intake")
    op.drop_table("conversations")
    op.drop_table("clients")

    # Drop enum types
    for enum_name in [
        "callback_status_enum", "voicemail_follow_up_status_enum",
        "voicemail_urgency_enum", "first_contact_channel_enum",
        "referral_source_enum", "alert_action_enum", "alert_type_enum",
        "alert_level_enum", "document_type_enum", "routing_recommendation_enum",
        "qualification_status_enum", "call_status_enum", "booked_via_enum",
        "appointment_status_enum", "appointment_type_enum", "message_role_enum",
        "call_outcome_enum", "call_direction_enum", "direction_enum",
        "channel_enum", "urgency_level_enum", "case_type_enum",
        "client_status_enum", "petitioner_relationship_enum",
        "petitioner_status_enum", "marital_status_enum", "education_level_enum",
        "entry_method_enum", "preferred_contact_enum",
    ]:
        op.execute(f"DROP TYPE IF EXISTS {enum_name}")

    op.execute("DROP EXTENSION IF EXISTS vector")
    op.execute("DROP EXTENSION IF EXISTS pgcrypto")
    op.execute("DROP EXTENSION IF EXISTS \"uuid-ossp\"")
