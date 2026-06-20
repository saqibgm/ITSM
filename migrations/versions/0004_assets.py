"""Asset management tables

Revision ID: 0004
Revises: 0003b
Create Date: 2026-06-05
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB, ENUM

revision = "0004"
down_revision = "0003b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # Enum types
    # ------------------------------------------------------------------
    op.execute(text(
    "DO $$ BEGIN "
    "CREATE TYPE assetstatus AS ENUM ('in_stock','in_use','in_maintenance','retired','disposed','lost'); "
    "EXCEPTION WHEN duplicate_object THEN null; END $$"
))
    op.execute(text(
    "DO $$ BEGIN "
    "CREATE TYPE assetcondition AS ENUM ('new','good','fair','poor'); "
    "EXCEPTION WHEN duplicate_object THEN null; END $$"
))
    op.execute(text(
    "DO $$ BEGIN "
    "CREATE TYPE assethistoryaction AS ENUM ('created','updated','assigned','unassigned','status_changed','disposed','maintenance_scheduled','relationship_added','relationship_removed'); "
    "EXCEPTION WHEN duplicate_object THEN null; END $$"
))
    op.execute(text(
    "DO $$ BEGIN "
    "CREATE TYPE maintenancetype AS ENUM ('scheduled','reactive','preventive'); "
    "EXCEPTION WHEN duplicate_object THEN null; END $$"
))
    op.execute(text(
    "DO $$ BEGIN "
    "CREATE TYPE maintenancestatus AS ENUM ('scheduled','in_progress','completed','cancelled'); "
    "EXCEPTION WHEN duplicate_object THEN null; END $$"
))
    op.execute(text(
    "DO $$ BEGIN "
    "CREATE TYPE relationshiptype AS ENUM ('runs_on','connected_to','depends_on','hosts','part_of','backs_up'); "
    "EXCEPTION WHEN duplicate_object THEN null; END $$"
))

    # ------------------------------------------------------------------
    # asset_categories
    # ------------------------------------------------------------------
    op.create_table(
        "asset_categories",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.VARCHAR(255), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column(
            "parent_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("asset_categories.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("icon", sa.VARCHAR(255), nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
    )
    op.create_index("ix_asset_categories_tenant_id", "asset_categories", ["tenant_id"])

    # ------------------------------------------------------------------
    # asset_types
    # ------------------------------------------------------------------
    op.create_table(
        "asset_types",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "category_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("asset_categories.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("name", sa.VARCHAR(200), nullable=False),
        sa.Column(
            "custom_fields_schema",
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
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_asset_types_tenant_id", "asset_types", ["tenant_id"])
    op.create_index("ix_asset_types_category_id", "asset_types", ["category_id"])

    # ------------------------------------------------------------------
    # vendors
    # ------------------------------------------------------------------
    op.create_table(
        "vendors",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.VARCHAR(255), nullable=False),
        sa.Column("contact_email", sa.VARCHAR(320), nullable=True),
        sa.Column("contact_phone", sa.VARCHAR(50), nullable=True),
        sa.Column("website", sa.VARCHAR(2048), nullable=True),
        sa.Column("address", JSONB, nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
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
    op.create_index("ix_vendors_tenant_id", "vendors", ["tenant_id"])

    # ------------------------------------------------------------------
    # assets
    # ------------------------------------------------------------------
    op.create_table(
        "assets",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("asset_tag", sa.VARCHAR(50), nullable=False),
        sa.Column(
            "tenant_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "product_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("products.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "department_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("departments.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "type_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("asset_types.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("name", sa.VARCHAR(500), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column(
            "status",
            ENUM(
                "in_stock", "in_use", "in_maintenance", "retired", "disposed", "lost",
                name="assetstatus",
                create_type=False,
            ),
            nullable=False,
            server_default=sa.text("'in_stock'"),
        ),
        sa.Column(
            "condition",
            ENUM(
                "new", "good", "fair", "poor",
                name="assetcondition",
                create_type=False,
            ),
            nullable=False,
            server_default=sa.text("'new'"),
        ),
        sa.Column("serial_number", sa.VARCHAR(255), nullable=True),
        sa.Column("model_number", sa.VARCHAR(255), nullable=True),
        sa.Column("manufacturer", sa.VARCHAR(255), nullable=True),
        sa.Column(
            "vendor_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("vendors.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("purchase_date", sa.Date, nullable=True),
        sa.Column("purchase_cost", sa.Numeric(12, 2), nullable=True),
        sa.Column("warranty_expiry_date", sa.Date, nullable=True),
        sa.Column(
            "assigned_to",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("assigned_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("location", sa.VARCHAR(500), nullable=True),
        sa.Column(
            "location_dept_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("departments.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "custom_fields",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
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
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("deleted_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_unique_constraint(
        "uq_assets_tenant_asset_tag", "assets", ["tenant_id", "asset_tag"]
    )
    op.create_index("ix_assets_tenant_id", "assets", ["tenant_id"])
    op.create_index("ix_assets_tenant_status", "assets", ["tenant_id", "status"])
    op.create_index("ix_assets_tenant_assigned_to", "assets", ["tenant_id", "assigned_to"])
    op.create_index("ix_assets_tenant_type_id", "assets", ["tenant_id", "type_id"])
    op.create_index("ix_assets_tenant_deleted_at", "assets", ["tenant_id", "deleted_at"])

    # ------------------------------------------------------------------
    # asset_status_transitions  (seeded, immutable)
    # ------------------------------------------------------------------
    op.create_table(
        "asset_status_transitions",
        sa.Column("from_status", sa.VARCHAR(30), primary_key=True),
        sa.Column("to_status", sa.VARCHAR(30), primary_key=True),
    )

    # Seed the state machine
    op.bulk_insert(
        sa.table(
            "asset_status_transitions",
            sa.column("from_status", sa.VARCHAR),
            sa.column("to_status", sa.VARCHAR),
        ),
        [
            {"from_status": "in_stock", "to_status": "in_use"},
            {"from_status": "in_use", "to_status": "in_maintenance"},
            {"from_status": "in_maintenance", "to_status": "in_use"},
            {"from_status": "in_maintenance", "to_status": "in_stock"},
            {"from_status": "in_use", "to_status": "retired"},
            {"from_status": "in_use", "to_status": "lost"},
            {"from_status": "retired", "to_status": "disposed"},
            {"from_status": "in_stock", "to_status": "disposed"},
        ],
    )

    # ------------------------------------------------------------------
    # asset_relationships  (CMDB graph)
    # ------------------------------------------------------------------
    op.create_table(
        "asset_relationships",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_asset_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("assets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "target_asset_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("assets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "relationship_type",
            ENUM(
                "runs_on", "connected_to", "depends_on", "hosts", "part_of", "backs_up",
                name="relationshiptype",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("description", sa.VARCHAR(500), nullable=True),
        sa.Column(
            "created_by",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
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
    op.create_unique_constraint(
        "uq_asset_relationships",
        "asset_relationships",
        ["source_asset_id", "target_asset_id", "relationship_type"],
    )
    op.create_index("ix_asset_rel_tenant_source", "asset_relationships", ["tenant_id", "source_asset_id"])
    op.create_index("ix_asset_rel_tenant_target", "asset_relationships", ["tenant_id", "target_asset_id"])

    # ------------------------------------------------------------------
    # asset_history  (INSERT only — REVOKE enforced below)
    # ------------------------------------------------------------------
    op.create_table(
        "asset_history",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "asset_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("assets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "actor_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "action",
            ENUM(
                "created", "updated", "assigned", "unassigned", "status_changed",
                "disposed", "maintenance_scheduled", "relationship_added",
                "relationship_removed",
                name="assethistoryaction",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("field_changed", sa.VARCHAR(100), nullable=True),
        sa.Column("old_value", JSONB, nullable=True),
        sa.Column("new_value", JSONB, nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column(
            "changed_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_asset_history_asset_changed_at", "asset_history", ["asset_id", "changed_at"]
    )

    # Enforce immutability — application user may INSERT but never UPDATE or DELETE
    op.execute(text("REVOKE UPDATE, DELETE ON asset_history FROM itsm_user"))

    # ------------------------------------------------------------------
    # asset_maintenance
    # ------------------------------------------------------------------
    op.create_table(
        "asset_maintenance",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "asset_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("assets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "type",
            ENUM(
                "scheduled", "reactive", "preventive",
                name="maintenancetype",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            ENUM(
                "scheduled", "in_progress", "completed", "cancelled",
                name="maintenancestatus",
                create_type=False,
            ),
            nullable=False,
            server_default=sa.text("'scheduled'"),
        ),
        sa.Column("title", sa.VARCHAR(500), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("scheduled_date", sa.Date, nullable=False),
        sa.Column("completed_date", sa.Date, nullable=True),
        sa.Column("cost", sa.Numeric(12, 2), nullable=True),
        sa.Column(
            "performed_by",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "vendor_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("vendors.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("notes", sa.Text, nullable=True),
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
    op.create_index("ix_asset_maintenance_asset_id", "asset_maintenance", ["asset_id"])

    # ------------------------------------------------------------------
    # asset_attachments
    # ------------------------------------------------------------------
    op.create_table(
        "asset_attachments",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "asset_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("assets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "uploaded_by",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("filename", sa.VARCHAR(500), nullable=False),
        sa.Column("storage_url", sa.VARCHAR(2048), nullable=False),
        sa.Column("file_size", sa.BigInteger, nullable=False),
        sa.Column("mime_type", sa.VARCHAR(255), nullable=False),
        sa.Column("label", sa.VARCHAR(100), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_asset_attachments_asset_id", "asset_attachments", ["asset_id"])

    # ------------------------------------------------------------------
    # asset_ticket_links  (M:N)
    # ------------------------------------------------------------------
    op.create_table(
        "asset_ticket_links",
        sa.Column(
            "asset_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("assets.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "ticket_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("tickets.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "linked_by",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "linked_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("asset_ticket_links")

    op.drop_index("ix_asset_attachments_asset_id", table_name="asset_attachments")
    op.drop_table("asset_attachments")

    op.drop_index("ix_asset_maintenance_asset_id", table_name="asset_maintenance")
    op.drop_table("asset_maintenance")

    op.drop_index("ix_asset_history_asset_changed_at", table_name="asset_history")
    op.drop_table("asset_history")

    op.drop_index("ix_asset_rel_tenant_target", table_name="asset_relationships")
    op.drop_index("ix_asset_rel_tenant_source", table_name="asset_relationships")
    op.drop_table("asset_relationships")

    op.drop_table("asset_status_transitions")

    op.drop_index("ix_assets_tenant_deleted_at", table_name="assets")
    op.drop_index("ix_assets_tenant_type_id", table_name="assets")
    op.drop_index("ix_assets_tenant_assigned_to", table_name="assets")
    op.drop_index("ix_assets_tenant_status", table_name="assets")
    op.drop_index("ix_assets_tenant_id", table_name="assets")
    op.drop_table("assets")

    op.drop_index("ix_vendors_tenant_id", table_name="vendors")
    op.drop_table("vendors")

    op.drop_index("ix_asset_types_category_id", table_name="asset_types")
    op.drop_index("ix_asset_types_tenant_id", table_name="asset_types")
    op.drop_table("asset_types")

    op.drop_index("ix_asset_categories_tenant_id", table_name="asset_categories")
    op.drop_table("asset_categories")

    op.execute(text("DROP TYPE IF EXISTS relationshiptype"))
    op.execute(text("DROP TYPE IF EXISTS maintenancestatus"))
    op.execute(text("DROP TYPE IF EXISTS maintenancetype"))
    op.execute(text("DROP TYPE IF EXISTS assethistoryaction"))
    op.execute(text("DROP TYPE IF EXISTS assetcondition"))
    op.execute(text("DROP TYPE IF EXISTS assetstatus"))
