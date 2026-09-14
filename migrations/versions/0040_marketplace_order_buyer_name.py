"""marketplace order buyer_name column

Revision ID: 0040_order_buyer_name
Revises: 0039_marketplace_integration
Create Date: 2026-09-14

Note: revision id kept short (alembic_version.version_num is VARCHAR(32) in
this DB — the first version of this file used the full descriptive id
"0040_marketplace_order_buyer_name" (34 chars) and the upgrade rolled back
entirely on the final `UPDATE alembic_version` step, confirmed live 2026-09-14).

Adds buyer_name to marketplace_orders — a real gap found via live testing
(2026-09-14): the frontend's "Buyer" column had only buyer_email to show,
but email is frequently unavailable per-connector (Amazon needs separate
PII/RDT access; eBay's Fulfillment API doesn't expose email at all — see
connectors/ebay.py, which was storing the buyer's eBay USERNAME in
buyer_email before this fix, not a real email address). A dedicated
buyer_name column lets each connector populate whatever display name it
actually has, independent of whether a real email is available.
"""
from alembic import op
import sqlalchemy as sa

revision = "0040_order_buyer_name"
down_revision = "0039_marketplace_integration"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "marketplace_orders",
        sa.Column(
            "buyer_name",
            sa.VARCHAR(255),
            nullable=True,
            comment="PII — encrypt at rest before production use (SHOPIFY_INTEGRATION_PLAN.md §4.6 precedent). "
                    "Display name for the frontend's Buyer column; see buyer_email's own comment for why email "
                    "alone isn't always available.",
        ),
    )


def downgrade() -> None:
    op.drop_column("marketplace_orders", "buyer_name")
