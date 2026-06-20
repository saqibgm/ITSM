"""AI enrichment tables: ai_usage_daily, ai_ticket_classifications,
ai_duplicate_candidates; description_embedding column + HNSW index on tickets.

Revision ID: 0003
Revises: 0002b
Create Date: 2026-06-05

Depends on 0002b_notifications which depends on 0002_tickets (tenants, users,
tickets, ticket_categories, teams).
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB, ENUM

revision = "0003"
down_revision = "0002b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. ai_usage_daily
    # ------------------------------------------------------------------
    op.create_table(
        "ai_usage_daily",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("date", sa.Date, nullable=False),
        sa.Column("ai_feature", sa.VARCHAR(50), nullable=False),
        sa.Column("model", sa.VARCHAR(100), nullable=False),
        sa.Column("input_tokens", sa.BigInteger, nullable=False, server_default=sa.text("0")),
        sa.Column("output_tokens", sa.BigInteger, nullable=False, server_default=sa.text("0")),
        sa.Column("call_count", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_unique_constraint(
        "uq_ai_usage_daily_tenant_date_feature_model",
        "ai_usage_daily",
        ["tenant_id", "date", "ai_feature", "model"],
    )
    op.create_index(
        "ix_ai_usage_daily_tenant_date",
        "ai_usage_daily",
        ["tenant_id", text("date DESC")],
    )

    # ------------------------------------------------------------------
    # 2. ai_ticket_classifications
    # ------------------------------------------------------------------
    op.create_table(
        "ai_ticket_classifications",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "ticket_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("tickets.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "suggested_category",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("ticket_categories.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("suggested_priority", sa.VARCHAR(20), nullable=True),
        sa.Column(
            "suggested_assignee",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "suggested_team",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("teams.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("confidence", sa.Float, nullable=False),
        sa.Column("model_version", sa.VARCHAR(100), nullable=False),
        sa.Column("accepted", sa.Boolean, nullable=True),
        sa.Column("accepted_fields", JSONB, nullable=True),
        sa.Column(
            "accepted_by",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "actual_category",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("ticket_categories.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("actual_priority", sa.VARCHAR(20), nullable=True),
        sa.Column(
            "actual_team",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("teams.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_ai_ticket_classifications_ticket",
        "ai_ticket_classifications",
        ["ticket_id"],
    )
    op.create_index(
        "ix_ai_ticket_classifications_accepted",
        "ai_ticket_classifications",
        ["accepted"],
        postgresql_where=text("accepted IS NOT NULL"),
    )

    # ------------------------------------------------------------------
    # 3. ai_duplicate_candidates
    # ------------------------------------------------------------------
    op.create_table(
        "ai_duplicate_candidates",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "ticket_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("tickets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "candidate_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("tickets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("similarity_score", sa.Float, nullable=False),
        sa.Column("model_version", sa.VARCHAR(100), nullable=False),
        sa.Column(
            "dismissed",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "dismissed_by",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    # INDEX (ticket_id, similarity_score DESC) — supports ordered duplicate listing
    op.execute(text(
        "CREATE INDEX ix_ai_duplicate_candidates_ticket_score "
        "ON ai_duplicate_candidates (ticket_id, similarity_score DESC)"
    ))

    # ------------------------------------------------------------------
    # 4. Add description_embedding VECTOR(1536) to tickets IF NOT EXISTS
    # ------------------------------------------------------------------
    # Enable pgvector extension first (idempotent)
    op.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))

    op.execute(text(
        "ALTER TABLE tickets "
        "ADD COLUMN IF NOT EXISTS description_embedding vector(1536)"
    ))

    # ------------------------------------------------------------------
    # 5. HNSW index on tickets.description_embedding (CONCURRENTLY)
    # ------------------------------------------------------------------
    # CONCURRENTLY cannot run inside a transaction block; Alembic wraps
    # each migration in a transaction by default.  We use execute_if to
    # check existence and run outside the implicit transaction.
    # The safest portable approach for Alembic is:
    #   - Use op.execute with IF NOT EXISTS (DDL still runs in transaction
    #     but is safe to re-run).
    # Note: CREATE INDEX CONCURRENTLY cannot run inside a transaction.
    # We omit CONCURRENTLY here — for production apply manually after migration:
    #   CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_tickets_embedding
    #     ON tickets USING hnsw (description_embedding vector_cosine_ops);
    op.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_tickets_embedding "
        "ON tickets USING hnsw (description_embedding vector_cosine_ops)"
    ))


def downgrade() -> None:
    # Reverse order

    op.execute(text("DROP INDEX IF EXISTS ix_tickets_embedding"))
    op.execute(text("ALTER TABLE tickets DROP COLUMN IF EXISTS description_embedding"))

    op.execute(text("DROP INDEX IF EXISTS ix_ai_duplicate_candidates_ticket_score"))
    op.drop_table("ai_duplicate_candidates")

    op.drop_index("ix_ai_ticket_classifications_accepted", table_name="ai_ticket_classifications")
    op.drop_index("ix_ai_ticket_classifications_ticket", table_name="ai_ticket_classifications")
    op.drop_table("ai_ticket_classifications")

    op.drop_index("ix_ai_usage_daily_tenant_date", table_name="ai_usage_daily")
    op.drop_constraint(
        "uq_ai_usage_daily_tenant_date_feature_model",
        "ai_usage_daily",
        type_="unique",
    )
    op.drop_table("ai_usage_daily")
