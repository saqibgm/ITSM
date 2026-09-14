"""marketplace_messages table

Revision ID: 0041_marketplace_messages
Revises: 0040_order_buyer_name
Create Date: 2026-09-14

Adds marketplace_messages — capability #3's own persisted record. Before
this, an outbound send only got recorded as a TicketComment via
MarketplaceOrderTicketLink, so a message on a plain order (no return/
replacement) vanished after sending — no queryable record at all. Needed
once a dedicated cross-order Messaging page was requested (not just a
per-row send action with no history view). Same RLS predicate as
migration 0039's tenant-scoped tables (marketplace_connections, orders,
events, integration_settings) — see that migration's docstring for why
this exact predicate (not 0032's hardened rewrite) is the right one here.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.sql import text

revision = "0041_marketplace_messages"
down_revision = "0040_order_buyer_name"
branch_labels = None
depends_on = None

_RLS_PREDICATE = """(
    current_setting('app.tenant_id', true) IS NULL
    OR current_setting('app.tenant_id', true) = ''
    OR current_setting('app.bypass_rls', true) = 'on'
    OR tenant_id IS NULL
    OR tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
)"""


def upgrade() -> None:
    op.create_table(
        "marketplace_messages",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "order_id", sa.UUID(as_uuid=True),
            sa.ForeignKey("marketplace_orders.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("provider", sa.VARCHAR(50), nullable=False),
        sa.Column("direction", sa.VARCHAR(10), nullable=False, comment="'outbound' | 'inbound'"),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("external_message_id", sa.VARCHAR(255), nullable=True),
        sa.Column(
            "sent_by_user_id", sa.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("sent_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_marketplace_messages_order", "marketplace_messages", ["order_id"])
    op.create_index("ix_marketplace_messages_tenant_sent", "marketplace_messages", ["tenant_id", "sent_at"])

    op.execute(text("ALTER TABLE marketplace_messages ENABLE ROW LEVEL SECURITY"))
    op.execute(text("ALTER TABLE marketplace_messages FORCE ROW LEVEL SECURITY"))
    op.execute(text(
        f"CREATE POLICY tenant_isolation ON marketplace_messages "
        f"USING {_RLS_PREDICATE} WITH CHECK {_RLS_PREDICATE}"
    ))


def downgrade() -> None:
    op.execute(text("DROP POLICY IF EXISTS tenant_isolation ON marketplace_messages"))
    op.execute(text("ALTER TABLE marketplace_messages NO FORCE ROW LEVEL SECURITY"))
    op.execute(text("ALTER TABLE marketplace_messages DISABLE ROW LEVEL SECURITY"))
    op.drop_index("ix_marketplace_messages_tenant_sent", table_name="marketplace_messages")
    op.drop_index("ix_marketplace_messages_order", table_name="marketplace_messages")
    op.drop_table("marketplace_messages")
