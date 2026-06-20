"""Schema hardening — ticket soft-delete column, reference timestamps,
reference-data uniqueness.

Revision ID: 0013_schema_hardening
Revises: 0012_reference_global_scoping
Create Date: 2026-06-08

1. tickets.deleted_at (real soft-delete column; was incorrectly written into
   the metadata JSONB blob and never filtered).
2. asset_categories / kb_tags get created_at + updated_at (TimestampMixin
   parity with sibling reference tables).
3. Partial unique indexes on reference tables so names are unique per scope
   (global vs tenant), mirroring the KB-spaces pattern from 0011 — prevents
   duplicate departments / categories / types / vendors.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

revision = "0013_schema_hardening"
down_revision = "0012_reference_global_scoping"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. tickets.deleted_at -------------------------------------------------
    op.add_column(
        "tickets",
        sa.Column("deleted_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_tickets_tenant_deleted_at", "tickets", ["tenant_id", "deleted_at"]
    )

    # 2. timestamps on asset_categories + kb_tags --------------------------
    for tbl in ("asset_categories", "kb_tags"):
        op.add_column(
            tbl,
            sa.Column(
                "created_at", sa.TIMESTAMP(timezone=True),
                server_default=sa.func.now(), nullable=False,
            ),
        )
        op.add_column(
            tbl,
            sa.Column(
                "updated_at", sa.TIMESTAMP(timezone=True),
                server_default=sa.func.now(), nullable=False,
            ),
        )

    # 3. per-scope uniqueness (global = tenant_id NULL, tenant = NOT NULL) --
    #    (tenant_id, key...) for tenant rows; (key...) for global rows.
    _UNIQUE = {
        "departments": ["name"],
        "ticket_categories": ["name"],
        "asset_categories": ["name"],
        "asset_types": ["category_id", "name"],
        "vendors": ["name"],
    }
    for tbl, keys in _UNIQUE.items():
        tenant_cols = ", ".join(["tenant_id", *keys])
        global_cols = ", ".join(keys)
        op.execute(text(
            f"CREATE UNIQUE INDEX uq_{tbl}_tenant_name ON {tbl} ({tenant_cols}) "
            f"WHERE tenant_id IS NOT NULL"
        ))
        op.execute(text(
            f"CREATE UNIQUE INDEX uq_{tbl}_global_name ON {tbl} ({global_cols}) "
            f"WHERE tenant_id IS NULL"
        ))


def downgrade() -> None:
    for tbl in ("departments", "ticket_categories", "asset_categories", "asset_types", "vendors"):
        op.execute(text(f"DROP INDEX IF EXISTS uq_{tbl}_tenant_name"))
        op.execute(text(f"DROP INDEX IF EXISTS uq_{tbl}_global_name"))

    for tbl in ("asset_categories", "kb_tags"):
        op.drop_column(tbl, "updated_at")
        op.drop_column(tbl, "created_at")

    op.drop_index("ix_tickets_tenant_deleted_at", table_name="tickets")
    op.drop_column("tickets", "deleted_at")
