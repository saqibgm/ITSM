"""AI reply suggestions table: ai_response_suggestions.

Revision ID: 0003b
Revises: 0003
Create Date: 2026-06-05

Depends on 0003_ticket_ai which creates tickets, ticket_comments, users.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import ENUM

revision = "0003b"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_response_suggestions",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "ticket_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("tickets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "comment_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("ticket_comments.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("model_version", sa.VARCHAR(100), nullable=False),
        sa.Column(
            "used",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "used_by",
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

    # Composite index: ticket_id + created_at DESC — supports fast
    # "latest suggestion for a ticket" queries.
    op.execute(text(
        "CREATE INDEX ix_ai_response_suggestions_ticket_created "
        "ON ai_response_suggestions (ticket_id, created_at DESC)"
    ))


def downgrade() -> None:
    op.execute(text(
        "DROP INDEX IF EXISTS ix_ai_response_suggestions_ticket_created"
    ))
    op.drop_table("ai_response_suggestions")
