"""Add kb_chunks table — heading-aware chunk index (KB_WIKI_CURATION_RAG_PLAN Phase 3).

Revision ID: 0038_kb_chunks
Revises: 0037_kb_curation
Create Date: 2026-08-18

Mirrors migration 0005_kb.py's pattern for kb_articles.embedding: create the
embedding column as TEXT, then ALTER to vector(1536) + HNSW index only if the
pgvector extension is available (already enabled by 0005, but the try/except
guard is kept for consistency with that migration's defensive style).

RLS: applies the tenant_isolation policy like kb_articles (0014), but with
the ORIGINAL fail-open predicate including "OR tenant_id IS NULL" — note that
0032's hardened rewrite of the *existing* kb_articles/kb_spaces/kb_tags
policies DROPPED that branch (only kept for tables it still has it on), which
means those tables' RLS backstop, as currently written, does not admit global
(tenant_id NULL) rows for a real tenant session — an app-layer-masked latent
inconsistency, not introduced or fixed here (out of scope for this migration;
flagged separately). kb_chunks gets the correct predicate since it's new.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ENUM as PGEnum
from sqlalchemy.sql import text

revision = "0038_kb_chunks"
down_revision = "0037_kb_curation"
branch_labels = None
depends_on = None

_RLS_PREDICATE = """(
    current_setting('app.tenant_id', true) IS NULL
    OR current_setting('app.tenant_id', true) = ''
    OR current_setting('app.bypass_rls', true) = 'on'
    OR tenant_id IS NULL
    OR tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
)"""


def upgrade() -> None:
    op.create_table(
        "kb_chunks",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "article_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("kb_articles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("space_id", sa.UUID(as_uuid=True), nullable=False),
        # Denormalized from kb_articles.visibility — reuses the existing enum type.
        # Uses the dialect-specific postgresql.ENUM rather than the generic
        # sa.Enum: within op.create_table(), a plain sa.Enum(..., create_type=False)
        # was observed still emitting CREATE TYPE (and failing with
        # DuplicateObjectError since kbarticlevisibility already exists from
        # migration 0005) — postgresql.ENUM(create_type=False) honors the flag.
        sa.Column(
            "visibility",
            PGEnum("public", "internal", "agents_only", name="kbarticlevisibility", create_type=False),
            nullable=False,
        ),
        sa.Column("chunk_index", sa.Integer, nullable=False),
        sa.Column("heading", sa.VARCHAR(500), nullable=True),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("embedding", sa.Text, nullable=True),  # placeholder; altered below
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_kb_chunks_article_id", "kb_chunks", ["article_id"])
    op.create_index("ix_kb_chunks_tenant_id", "kb_chunks", ["tenant_id"])

    try:
        op.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        op.execute(text(
            "ALTER TABLE kb_chunks ALTER COLUMN embedding TYPE vector(1536) USING NULL"
        ))
        op.execute(text(
            "CREATE INDEX ix_kb_chunks_embedding_hnsw "
            "ON kb_chunks USING hnsw (embedding vector_cosine_ops)"
        ))
    except Exception:
        # pgvector not installed — embedding stays as TEXT, no HNSW index
        pass

    op.execute(text("ALTER TABLE kb_chunks ENABLE ROW LEVEL SECURITY"))
    op.execute(text("ALTER TABLE kb_chunks FORCE ROW LEVEL SECURITY"))
    op.execute(text(
        f"CREATE POLICY tenant_isolation ON kb_chunks "
        f"USING {_RLS_PREDICATE} WITH CHECK {_RLS_PREDICATE}"
    ))


def downgrade() -> None:
    op.execute(text("DROP POLICY IF EXISTS tenant_isolation ON kb_chunks"))
    op.execute(text("ALTER TABLE kb_chunks NO FORCE ROW LEVEL SECURITY"))
    op.execute(text("ALTER TABLE kb_chunks DISABLE ROW LEVEL SECURITY"))
    op.execute(text("DROP INDEX IF EXISTS ix_kb_chunks_embedding_hnsw"))
    op.drop_index("ix_kb_chunks_tenant_id", table_name="kb_chunks")
    op.drop_index("ix_kb_chunks_article_id", table_name="kb_chunks")
    op.drop_table("kb_chunks")
