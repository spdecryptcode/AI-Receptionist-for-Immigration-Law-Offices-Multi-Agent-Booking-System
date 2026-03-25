"""Add compliance tables: failed_tasks, audit_log; add columns to call_logs + voicemails

Revision ID: 0002_compliance_tables
Revises: 0001_initial
Create Date: 2026-01-01 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision: str = "0002_compliance_tables"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # failed_tasks — dead-letter store for db_worker items
    # ------------------------------------------------------------------
    op.create_table(
        "failed_tasks",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("queue_name", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("abandoned", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("last_tried", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index("ix_failed_tasks_queue", "failed_tasks", ["queue_name"])
    op.create_index(
        "ix_failed_tasks_pending",
        "failed_tasks",
        ["abandoned", "retry_count"],
    )

    # ------------------------------------------------------------------
    # audit_log — HTTP request audit trail for compliance
    # ------------------------------------------------------------------
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("method", sa.String(10), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("query", sa.Text(), nullable=True),
        sa.Column("status_code", sa.SmallInteger(), nullable=False),
        sa.Column("ip", sa.String(45), nullable=True),       # IPv6 max 45 chars
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    # Only keep 90 days of audit logs — prune via cron
    op.create_index("ix_audit_log_created_at", "audit_log", ["created_at"])
    op.create_index("ix_audit_log_path", "audit_log", ["path"])

    # ------------------------------------------------------------------
    # Add new columns to call_logs (post-call analytics)
    # Use IF NOT EXISTS so this migration is safe to run even if 0001
    # already created some of these columns.
    # ------------------------------------------------------------------
    conn = op.get_bind()
    conn.execute(sa.text("ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS ai_summary TEXT"))
    conn.execute(sa.text("ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS sentiment_score FLOAT"))
    conn.execute(sa.text("ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS sentiment_label VARCHAR(20)"))
    conn.execute(sa.text("ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS frustration_detected BOOLEAN"))
    conn.execute(sa.text("ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS cost_usd NUMERIC(10,6)"))

    # ------------------------------------------------------------------
    # Add new columns to voicemails (processing results)
    # ------------------------------------------------------------------
    conn.execute(sa.text("ALTER TABLE voicemails ADD COLUMN IF NOT EXISTS is_emergency BOOLEAN NOT NULL DEFAULT false"))
    conn.execute(sa.text("ALTER TABLE voicemails ADD COLUMN IF NOT EXISTS ghl_task_id TEXT"))
    conn.execute(sa.text("ALTER TABLE voicemails ADD COLUMN IF NOT EXISTS summary TEXT"))
    conn.execute(sa.text("ALTER TABLE voicemails ADD COLUMN IF NOT EXISTS status VARCHAR(30) NOT NULL DEFAULT 'pending'"))

def downgrade() -> None:
    with op.batch_alter_table("voicemails") as batch_op:
        batch_op.drop_column("status")
        batch_op.drop_column("summary")
        batch_op.drop_column("ghl_task_id")
        batch_op.drop_column("is_emergency")

    with op.batch_alter_table("call_logs") as batch_op:
        batch_op.drop_column("cost_usd")
        batch_op.drop_column("frustration_detected")
        batch_op.drop_column("sentiment_label")
        batch_op.drop_column("sentiment_score")
        batch_op.drop_column("ai_summary")

    op.drop_index("ix_audit_log_path", "audit_log")
    op.drop_index("ix_audit_log_created_at", "audit_log")
    op.drop_table("audit_log")

    op.drop_index("ix_failed_tasks_pending", "failed_tasks")
    op.drop_index("ix_failed_tasks_queue", "failed_tasks")
    op.drop_table("failed_tasks")
