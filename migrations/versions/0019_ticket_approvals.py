"""Ticket approval workflow — ticket_approvals table.

Revision ID: 0019_ticket_approvals
Revises: 0018_user_iam_roles_sync
Create Date: 2026-06-23

Records approval requests/decisions on change & access-request tickets. Tenant
isolation is via the parent ticket (FK + tenant-scoped lookups), like
ticket_escalations — no own tenant_id / RLS policy.
"""

from alembic import op
import sqlalchemy as sa

revision = "0019_ticket_approvals"
down_revision = "0018_user_iam_roles_sync"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ticket_approvals",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "ticket_id", sa.UUID(as_uuid=True),
            sa.ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "requested_by", sa.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False,
        ),
        sa.Column(
            "approver_id", sa.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("status", sa.VARCHAR(20), nullable=False, server_default=sa.text("'pending'")),
        sa.Column(
            "decided_by", sa.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("decided_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("comment", sa.Text, nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_ticket_approvals_ticket_id", "ticket_approvals", ["ticket_id"])


def downgrade() -> None:
    op.drop_index("ix_ticket_approvals_ticket_id", table_name="ticket_approvals")
    op.drop_table("ticket_approvals")
