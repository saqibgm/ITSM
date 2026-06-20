"""Outbound webhook endpoints and delivery log tables.

Revision ID: 0008_webhooks
Revises: 0007_automation
Create Date: 2026-06-05

Creates:
  - webhook_endpoints
  - webhook_deliveries

Indexes:
  idx_webhook_endpoints_tenant     on (tenant_id, is_active)
  idx_webhook_deliveries_retry     on (status, next_retry_at) WHERE status = 'retrying'  (partial)

Full downgrade() drops tables in reverse FK order.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, ENUM

revision = "0008_webhooks"
down_revision = "0007_automation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # webhook_endpoints
    # ------------------------------------------------------------------
    op.create_table(
        "webhook_endpoints",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.VARCHAR(255), nullable=False),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("secret", sa.Text, nullable=False),
        sa.Column(
            "events",
            JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "is_active",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("last_success_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_failure_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "failure_count",
            sa.Integer,
            nullable=False,
            server_default=sa.text("0"),
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
        "idx_webhook_endpoints_tenant",
        "webhook_endpoints",
        ["tenant_id", "is_active"],
    )

    # ------------------------------------------------------------------
    # webhook_deliveries
    # ------------------------------------------------------------------
    op.create_table(
        "webhook_deliveries",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "endpoint_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("webhook_endpoints.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.VARCHAR(100), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column(
            "status",
            sa.VARCHAR(20),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column(
            "attempt_count",
            sa.Integer,
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("response_status_code", sa.Integer, nullable=True),
        sa.Column("response_body", sa.Text, nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("delivered_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("next_retry_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    # Partial index: only rows currently awaiting retry — keeps it tiny.
    op.create_index(
        "idx_webhook_deliveries_retry",
        "webhook_deliveries",
        ["status", "next_retry_at"],
        postgresql_where=sa.text("status = 'retrying'"),
    )


def downgrade() -> None:
    op.drop_index(
        "idx_webhook_deliveries_retry",
        table_name="webhook_deliveries",
    )
    op.drop_table("webhook_deliveries")

    op.drop_index(
        "idx_webhook_endpoints_tenant",
        table_name="webhook_endpoints",
    )
    op.drop_table("webhook_endpoints")
