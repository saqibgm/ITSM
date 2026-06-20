"""Reference-data global scoping — nullable tenant_id on reference tables.

Revision ID: 0012_reference_global_scoping
Revises: 0011_kb_global_scoping
Create Date: 2026-06-08

Lets reference data be either tenant-specific (tenant_id set) or global /
shared across all tenants (tenant_id NULL), mirroring the KB scoping in 0011.
Global rows are managed by platform users; tenants consume them read-only and
may additionally define their own tenant-specific rows.

Applies to: departments, ticket_categories, asset_categories, asset_types,
vendors. (products stays IAM-managed; itsm_roles already nullable.)
"""

from alembic import op
from sqlalchemy import text

revision = "0012_reference_global_scoping"
down_revision = "0011_kb_global_scoping"
branch_labels = None
depends_on = None

_TABLES = [
    "departments",
    "ticket_categories",
    "asset_categories",
    "asset_types",
    "vendors",
]


def upgrade() -> None:
    for tbl in _TABLES:
        op.alter_column(tbl, "tenant_id", nullable=True)


def downgrade() -> None:
    # Remove global rows before restoring the NOT NULL constraint.
    for tbl in _TABLES:
        op.execute(text(f"DELETE FROM {tbl} WHERE tenant_id IS NULL"))
        op.alter_column(tbl, "tenant_id", nullable=False)
