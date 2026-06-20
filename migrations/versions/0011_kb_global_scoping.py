"""KB global/product scoping — nullable tenant_id, add kb_articles.product_id.

Revision ID: 0011_kb_global_scoping
Revises: 0010_tenant_deleted_at
Create Date: 2026-06-07

Allows KB spaces and articles to be scoped four ways:
  - tenant_id NULL,     product_id NULL     -> global (visible to all tenants/products)
  - tenant_id NULL,     product_id NOT NULL -> product-specific (any tenant using that product)
  - tenant_id NOT NULL, product_id NULL     -> tenant-specific (any product within that tenant)
  - tenant_id NOT NULL, product_id NOT NULL -> tenant + product specific

Relaxes the "tenant_id NEVER NULL" invariant specifically for KB entities;
all other domains keep tenant_id NOT NULL.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

revision = "0011_kb_global_scoping"
down_revision = "0010_tenant_deleted_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # kb_spaces.tenant_id -> nullable
    # ------------------------------------------------------------------
    op.alter_column("kb_spaces", "tenant_id", nullable=True)

    # Replace the (tenant_id, slug) unique constraint: NULLs are not equal in
    # standard unique constraints, so multiple global spaces could share a slug.
    # Drop it and use two partial unique indexes instead.
    op.drop_constraint("uq_kb_spaces_tenant_slug", "kb_spaces", type_="unique")
    op.execute(text(
        "CREATE UNIQUE INDEX uq_kb_spaces_tenant_slug "
        "ON kb_spaces (tenant_id, slug) WHERE tenant_id IS NOT NULL"
    ))
    op.execute(text(
        "CREATE UNIQUE INDEX uq_kb_spaces_global_slug "
        "ON kb_spaces (slug) WHERE tenant_id IS NULL"
    ))

    # ------------------------------------------------------------------
    # kb_articles.tenant_id -> nullable; add product_id
    # ------------------------------------------------------------------
    op.alter_column("kb_articles", "tenant_id", nullable=True)
    op.add_column(
        "kb_articles",
        sa.Column(
            "product_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("products.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_kb_articles_product_id", "kb_articles", ["product_id"])

    # status/visibility composite index included tenant_id; replace with one
    # that also covers product_id so global/product-scoped lookups stay indexed
    op.drop_index("ix_kb_articles_status_visibility", table_name="kb_articles")
    op.create_index(
        "ix_kb_articles_scope_status_visibility",
        "kb_articles",
        ["tenant_id", "product_id", "status", "visibility"],
    )


def downgrade() -> None:
    op.drop_index("ix_kb_articles_scope_status_visibility", table_name="kb_articles")
    op.create_index(
        "ix_kb_articles_status_visibility",
        "kb_articles",
        ["tenant_id", "status", "visibility"],
    )

    op.drop_index("ix_kb_articles_product_id", table_name="kb_articles")
    op.drop_column("kb_articles", "product_id")

    op.execute(text(
        "DELETE FROM kb_articles WHERE tenant_id IS NULL"
    ))
    op.alter_column("kb_articles", "tenant_id", nullable=False)

    op.execute(text("DROP INDEX IF EXISTS uq_kb_spaces_global_slug"))
    op.execute(text("DROP INDEX IF EXISTS uq_kb_spaces_tenant_slug"))
    op.execute(text(
        "DELETE FROM kb_spaces WHERE tenant_id IS NULL"
    ))
    op.create_unique_constraint(
        "uq_kb_spaces_tenant_slug", "kb_spaces", ["tenant_id", "slug"]
    )
    op.alter_column("kb_spaces", "tenant_id", nullable=False)
