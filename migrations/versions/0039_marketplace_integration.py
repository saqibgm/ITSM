"""Native marketplace-integration tables (V3-Marketplaces, Phase 1 scaffold).

Revision ID: 0039_marketplace_integration
Revises: 0038_kb_chunks
Create Date: 2026-09-09

Adds: marketplace_connections, marketplace_orders, marketplace_events,
marketplace_order_ticket_links, marketplace_integration_settings.

Per docs/plans/NATIVE_MARKETPLACE_CONNECTORS_PLAN.md §1/§4. RLS applied to the
four tenant-scoped tables using the corrected fail-open predicate (matches
0038_kb_chunks — the ORIGINAL predicate including "OR tenant_id IS NULL", not
0032's hardened rewrite; see 0038's docstring for why that distinction
matters). marketplace_order_ticket_links gets no tenant_id/RLS of its own,
same precedent as asset_ticket_links — scoping is inherited transitively via
the FK'd order/ticket rows.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import text

revision = "0039_marketplace_integration"
down_revision = "0038_kb_chunks"
branch_labels = None
depends_on = None

_RLS_PREDICATE = """(
    current_setting('app.tenant_id', true) IS NULL
    OR current_setting('app.tenant_id', true) = ''
    OR current_setting('app.bypass_rls', true) = 'on'
    OR tenant_id IS NULL
    OR tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
)"""

_RLS_TABLES = (
    "marketplace_connections",
    "marketplace_orders",
    "marketplace_events",
    "marketplace_integration_settings",
)


def _enable_rls(table: str) -> None:
    op.execute(text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
    op.execute(text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
    op.execute(text(
        f"CREATE POLICY tenant_isolation ON {table} "
        f"USING {_RLS_PREDICATE} WITH CHECK {_RLS_PREDICATE}"
    ))


def _disable_rls(table: str) -> None:
    op.execute(text(f"DROP POLICY IF EXISTS tenant_isolation ON {table}"))
    op.execute(text(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY"))
    op.execute(text(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY"))


def upgrade() -> None:
    op.create_table(
        "marketplace_connections",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.VARCHAR(50), nullable=False),
        sa.Column("external_id", sa.VARCHAR(255), nullable=True),
        sa.Column("credentials", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("status", sa.VARCHAR(20), nullable=False, server_default=sa.text("'disconnected'")),
        sa.Column("messaging_capability", sa.VARCHAR(20), nullable=False, server_default=sa.text("'none'")),
        sa.Column("provider_metadata", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("last_synced_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "provider", name="uq_marketplace_connections_tenant_provider"),
    )
    op.create_index("ix_marketplace_connections_tenant_id", "marketplace_connections", ["tenant_id"])
    op.create_index(
        "ix_marketplace_connections_tenant_status", "marketplace_connections", ["tenant_id", "status"]
    )

    op.create_table(
        "marketplace_orders",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "connection_id", sa.UUID(as_uuid=True),
            sa.ForeignKey("marketplace_connections.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("provider", sa.VARCHAR(50), nullable=False),
        sa.Column("external_order_id", sa.VARCHAR(255), nullable=False),
        sa.Column("status", sa.VARCHAR(20), nullable=False, server_default=sa.text("'new'")),
        sa.Column("buyer_email", sa.VARCHAR(320), nullable=True),
        sa.Column("order_lines", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("total_amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("currency", sa.VARCHAR(3), nullable=True),
        sa.Column("placed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("raw_metadata", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "tenant_id", "provider", "external_order_id",
            name="uq_marketplace_orders_tenant_provider_external_id",
        ),
    )
    op.create_index("ix_marketplace_orders_tenant_id", "marketplace_orders", ["tenant_id"])
    op.create_index("ix_marketplace_orders_connection", "marketplace_orders", ["connection_id"])

    op.create_table(
        "marketplace_events",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "connection_id", sa.UUID(as_uuid=True),
            sa.ForeignKey("marketplace_connections.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("provider", sa.VARCHAR(50), nullable=False),
        sa.Column("external_event_id", sa.VARCHAR(255), nullable=False),
        sa.Column("event_type", sa.VARCHAR(100), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("status", sa.VARCHAR(20), nullable=False, server_default=sa.text("'received'")),
        sa.Column(
            "resulting_ticket_id", sa.UUID(as_uuid=True),
            sa.ForeignKey("tickets.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column(
            "resulting_order_id", sa.UUID(as_uuid=True),
            sa.ForeignKey("marketplace_orders.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("received_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("processed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "provider", "external_event_id", name="uq_marketplace_events_provider_external_id"
        ),
    )
    op.create_index("ix_marketplace_events_tenant_id", "marketplace_events", ["tenant_id"])
    op.create_index("ix_marketplace_events_tenant_status", "marketplace_events", ["tenant_id", "status"])
    op.create_index("ix_marketplace_events_connection", "marketplace_events", ["connection_id"])

    op.create_table(
        "marketplace_order_ticket_links",
        sa.Column(
            "order_id", sa.UUID(as_uuid=True),
            sa.ForeignKey("marketplace_orders.id", ondelete="CASCADE"), primary_key=True,
        ),
        sa.Column(
            "ticket_id", sa.UUID(as_uuid=True),
            sa.ForeignKey("tickets.id", ondelete="CASCADE"), primary_key=True,
        ),
        sa.Column("link_type", sa.VARCHAR(20), nullable=False),
        sa.Column("linked_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "marketplace_integration_settings",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column("settings", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("updated_by", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    for table in _RLS_TABLES:
        _enable_rls(table)


def downgrade() -> None:
    for table in reversed(_RLS_TABLES):
        _disable_rls(table)

    op.drop_table("marketplace_integration_settings")
    op.drop_table("marketplace_order_ticket_links")
    op.drop_index("ix_marketplace_events_connection", table_name="marketplace_events")
    op.drop_index("ix_marketplace_events_tenant_status", table_name="marketplace_events")
    op.drop_index("ix_marketplace_events_tenant_id", table_name="marketplace_events")
    op.drop_table("marketplace_events")
    op.drop_index("ix_marketplace_orders_connection", table_name="marketplace_orders")
    op.drop_index("ix_marketplace_orders_tenant_id", table_name="marketplace_orders")
    op.drop_table("marketplace_orders")
    op.drop_index("ix_marketplace_connections_tenant_status", table_name="marketplace_connections")
    op.drop_index("ix_marketplace_connections_tenant_id", table_name="marketplace_connections")
    op.drop_table("marketplace_connections")
