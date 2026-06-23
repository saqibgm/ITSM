"""Webhook delivery format + marketplace provenance.

Revision ID: 0020_webhook_format
Revises: 0019_ticket_approvals
Create Date: 2026-06-23

Adds two columns to webhook_endpoints for the integration marketplace (4b):
  format          — delivery payload format ('generic' | 'teams'); default
                    'generic' keeps every existing endpoint signed-raw-JSON.
  integration_key — catalog key an endpoint was created from (e.g.
                    'microsoft_teams'); NULL for hand-created webhooks.
Purely additive; no backfill needed (server_default covers existing rows).
"""

from alembic import op
import sqlalchemy as sa

revision = "0020_webhook_format"
down_revision = "0019_ticket_approvals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "webhook_endpoints",
        sa.Column(
            "format", sa.VARCHAR(20), nullable=False,
            server_default=sa.text("'generic'"),
        ),
    )
    op.add_column(
        "webhook_endpoints",
        sa.Column("integration_key", sa.VARCHAR(50), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("webhook_endpoints", "integration_key")
    op.drop_column("webhook_endpoints", "format")
