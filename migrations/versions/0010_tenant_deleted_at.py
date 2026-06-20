"""Add tenants.deleted_at column (soft-delete support).

Revision ID: 0010_tenant_deleted_at
Revises: 0009_schema_additions
Create Date: 2026-06-06
"""

from alembic import op
from sqlalchemy import text

revision = "0010_tenant_deleted_at"
down_revision = "0009_schema_additions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent: 0009_schema_additions already adds tenants.deleted_at, so on a
    # clean replay this column may already exist. IF NOT EXISTS keeps the chain
    # applyable from scratch without duplicating the column.
    op.execute(text(
        "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP WITH TIME ZONE"
    ))


def downgrade() -> None:
    op.execute(text("ALTER TABLE tenants DROP COLUMN IF EXISTS deleted_at"))
