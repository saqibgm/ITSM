"""Virtual Agent sessions and messages tables.

Revision ID: 0006_virtual_agent
Revises: 0005_kb
Create Date: 2026-06-05

Creates virtual_agent_sessions and virtual_agent_messages tables in FK order.
Indexes:
  idx_virtual_agent_sessions_tenant_id
  idx_virtual_agent_messages_session_created
Full downgrade() drops tables in reverse FK order.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB, ENUM

revision = "0006_virtual_agent"
down_revision = "0005_kb"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # virtual_agent_sessions
    # ------------------------------------------------------------------
    op.create_table(
        "virtual_agent_sessions",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("channel", sa.VARCHAR(50), nullable=False),
        sa.Column(
            "status",
            sa.VARCHAR(50),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.Column("intent", sa.VARCHAR(100), nullable=True),
        sa.Column("context_summary", sa.Text, nullable=True),
        sa.Column(
            "handed_off_to_user_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("ended_at", sa.TIMESTAMP(timezone=True), nullable=True),
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
        "idx_virtual_agent_sessions_tenant_id",
        "virtual_agent_sessions",
        ["tenant_id"],
    )

    # ------------------------------------------------------------------
    # virtual_agent_messages
    # ------------------------------------------------------------------
    op.create_table(
        "virtual_agent_messages",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "session_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("virtual_agent_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.VARCHAR(20), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("intent_detected", sa.VARCHAR(100), nullable=True),
        sa.Column("sources", JSONB, nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "idx_virtual_agent_messages_session_created",
        "virtual_agent_messages",
        ["session_id", "created_at"],
    )


def downgrade() -> None:
    # Drop messages first (FK dependency on sessions)
    op.drop_index(
        "idx_virtual_agent_messages_session_created",
        table_name="virtual_agent_messages",
    )
    op.drop_table("virtual_agent_messages")

    op.drop_index(
        "idx_virtual_agent_sessions_tenant_id",
        table_name="virtual_agent_sessions",
    )
    op.drop_table("virtual_agent_sessions")
