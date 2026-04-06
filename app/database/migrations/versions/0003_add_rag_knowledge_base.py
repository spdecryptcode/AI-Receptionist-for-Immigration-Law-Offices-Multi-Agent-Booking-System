"""Add RAG knowledge base tables: knowledge_documents, knowledge_chunks, rag_query_logs

Revision ID: 0003_add_rag_knowledge_base
Revises: 0002_compliance_tables
Create Date: 2026-04-06 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision: str = "0003_add_rag_knowledge_base"
down_revision: Union[str, None] = "0002_compliance_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Use raw SQL throughout so every statement is idempotent (IF NOT EXISTS).
    # This guards against partial previous runs.

    op.execute("""
        DO $$ BEGIN
            CREATE TYPE knowledge_source_type_enum AS ENUM (
                'faq', 'case_guide', 'firm_policy', 'uscis_form',
                'conversation_transcript', 'policy_news'
            );
        EXCEPTION
            WHEN duplicate_object THEN null;
        END $$
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS knowledge_documents (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            title       TEXT NOT NULL,
            source_type knowledge_source_type_enum NOT NULL,
            language    VARCHAR(10) NOT NULL DEFAULT 'en',
            content_hash TEXT NOT NULL,
            metadata    JSONB,
            expires_at  TIMESTAMPTZ,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_knowledge_documents_content_hash UNIQUE (content_hash)
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS knowledge_chunks (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            document_id     UUID NOT NULL REFERENCES knowledge_documents(id) ON DELETE CASCADE,
            parent_chunk_id UUID REFERENCES knowledge_chunks(id) ON DELETE SET NULL,
            chunk_index     INTEGER NOT NULL,
            content         TEXT NOT NULL,
            context_prefix  TEXT,
            embedding       VECTOR(1536),
            language        VARCHAR(10) NOT NULL DEFAULT 'en',
            quality_score   FLOAT NOT NULL DEFAULT 0,
            tsvector_col    TSVECTOR,
            metadata        JSONB
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS rag_query_logs (
            id                        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            query_hash                TEXT NOT NULL,
            channel                   VARCHAR(20) NOT NULL,
            path                      VARCHAR(10) NOT NULL DEFAULT 'full',
            top_score                 FLOAT,
            chunks_returned           INTEGER NOT NULL DEFAULT 0,
            confidence_gate_triggered BOOLEAN NOT NULL DEFAULT FALSE,
            cross_language_fallback   BOOLEAN NOT NULL DEFAULT FALSE,
            cache_hit                 BOOLEAN NOT NULL DEFAULT FALSE,
            retrieval_ms              INTEGER,
            language                  VARCHAR(10) NOT NULL DEFAULT 'en',
            call_sid                  VARCHAR(255),
            session_id                TEXT,
            created_at                TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)

    # ------------------------------------------------------------------
    # Indexes (IF NOT EXISTS)
    # ------------------------------------------------------------------
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_knowledge_documents_expires "
        "ON knowledge_documents (expires_at) WHERE expires_at IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_knowledge_documents_source_lang "
        "ON knowledge_documents (source_type, language)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_document "
        "ON knowledge_chunks (document_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_parent "
        "ON knowledge_chunks (parent_chunk_id) WHERE parent_chunk_id IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_lang_quality "
        "ON knowledge_chunks (language, quality_score DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_tsvector "
        "ON knowledge_chunks USING gin (tsvector_col)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_embedding_hnsw "
        "ON knowledge_chunks USING hnsw (embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_rag_query_logs_created "
        "ON rag_query_logs (created_at DESC)"
    )

    # ------------------------------------------------------------------
    # tsvector auto-update trigger (language-aware)
    # ------------------------------------------------------------------
    op.execute("""
        CREATE OR REPLACE FUNCTION update_knowledge_chunk_tsvector()
        RETURNS trigger AS $$
        BEGIN
            NEW.tsvector_col := to_tsvector(
                CASE WHEN NEW.language = 'es' THEN 'spanish' ELSE 'english' END,
                COALESCE(NEW.context_prefix, '') || ' ' || COALESCE(NEW.content, '')
            );
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)

    op.execute("""
        DO $$ BEGIN
            CREATE TRIGGER trg_knowledge_chunks_tsvector
            BEFORE INSERT OR UPDATE OF content, context_prefix, language
            ON knowledge_chunks
            FOR EACH ROW EXECUTE FUNCTION update_knowledge_chunk_tsvector();
        EXCEPTION
            WHEN duplicate_object THEN null;
        END $$
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_knowledge_chunks_tsvector ON knowledge_chunks")
    op.execute("DROP FUNCTION IF EXISTS update_knowledge_chunk_tsvector()")
    op.drop_table("rag_query_logs")
    op.drop_table("knowledge_chunks")
    op.drop_table("knowledge_documents")
    op.execute("""
        DO $$ BEGIN
            DROP TYPE knowledge_source_type_enum;
        EXCEPTION
            WHEN undefined_object THEN null;
        END $$
    """)

