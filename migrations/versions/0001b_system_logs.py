"""Add system_logs table

Revision ID: 0001b
Revises: 0001
Create Date: 2026-06-05
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, ENUM

# revision identifiers, used by Alembic.
revision = "0001b"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "system_logs",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.UUID(as_uuid=True),
            nullable=True,
            comment="Nullable — tenant may be deleted but log row is retained",
        ),
        sa.Column("request_id", sa.VARCHAR(255), nullable=True),
        sa.Column(
            "level",
            sa.VARCHAR(20),
            nullable=False,
            comment="warning | error | critical",
        ),
        sa.Column("event", sa.VARCHAR(500), nullable=False),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("exception_type", sa.VARCHAR(255), nullable=True),
        sa.Column(
            "stack_trace",
            sa.Text,
            nullable=True,
            comment="CRITICAL / unhandled only — never forwarded in HTTP responses",
        ),
        sa.Column(
            "metadata",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    # Composite index: per-tenant queries filtered by level, newest first
    op.create_index(
        "ix_syslog_tenant_level_created",
        "system_logs",
        ["tenant_id", "level", sa.text("created_at DESC")],
    )

    # Global index: platform-level queries by level, newest first
    op.create_index(
        "ix_syslog_level_created",
        "system_logs",
        ["level", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_syslog_level_created", table_name="system_logs")
    op.drop_index("ix_syslog_tenant_level_created", table_name="system_logs")
    op.drop_table("system_logs")
