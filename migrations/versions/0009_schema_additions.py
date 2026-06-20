"""Add missing columns: platform_audit_log.action/details and tenants columns.

Revision ID: 0009_schema_additions
Revises: 0008_webhooks
Create Date: 2026-06-06

Changes:
  - platform_audit_log: ADD COLUMN action VARCHAR(255) NULL
  - platform_audit_log: ADD COLUMN details JSONB NULL
  - tenants: ADD COLUMN ai_budget_monthly_tokens BIGINT NULL
  - tenants: ADD COLUMN deleted_at TIMESTAMPTZ NULL
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0009_schema_additions"
down_revision = "0008_webhooks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "platform_audit_log",
        sa.Column("action", sa.VARCHAR(255), nullable=True),
    )
    op.add_column(
        "platform_audit_log",
        sa.Column("details", JSONB, nullable=True),
    )
    op.add_column(
        "tenants",
        sa.Column("ai_budget_monthly_tokens", sa.BigInteger, nullable=True),
    )
    op.add_column(
        "tenants",
        sa.Column("deleted_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tenants", "deleted_at")
    op.drop_column("tenants", "ai_budget_monthly_tokens")
    op.drop_column("platform_audit_log", "details")
    op.drop_column("platform_audit_log", "action")
