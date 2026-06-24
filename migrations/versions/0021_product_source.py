"""Product origin tracking — products.source ('iam' | 'manual').

Revision ID: 0021_product_source
Revises: 0020_webhook_format
Create Date: 2026-06-24

Adds `source` so the IAM sync and admin CRUD can coexist: 'iam' rows are
projected from IAM subscriptions (sync-owned, undeletable); 'manual' rows are
admin-created in ITSM (fully editable/deletable). Existing rows all originated
from the IAM product sync (the only prior write path), so they are backfilled
to 'iam'. New rows default to 'manual'.
"""

from alembic import op
import sqlalchemy as sa

revision = "0021_product_source"
down_revision = "0020_webhook_format"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "products",
        sa.Column("source", sa.VARCHAR(20), nullable=False,
                  server_default=sa.text("'manual'")),
    )
    # Every pre-existing product came from the IAM subscription sync.
    op.execute("UPDATE products SET source = 'iam'")


def downgrade() -> None:
    op.drop_column("products", "source")
