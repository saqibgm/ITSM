"""Make kb_articles.author_id nullable for platform-authored content.

Revision ID: 0016_kb_article_author_nullable
Revises: 0015_fix_rls_predicate
Create Date: 2026-06-09

Global (tenant_id NULL) and product-wide (cross-tenant) KB articles are
authored by platform/super_admin users, who have no local `users` row mirror
(local_user_id is None — see auth.dependencies). author_id was a NOT NULL FK
to users.id, which blocked those users from authoring such content. Relax it
to nullable; the FK (ON DELETE RESTRICT) is preserved.
"""

from alembic import op

revision = "0016_kb_article_author_nullable"
down_revision = "0015_fix_rls_predicate"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("kb_articles", "author_id", nullable=True)


def downgrade() -> None:
    # Cannot restore NOT NULL while platform-authored rows have NULL author_id;
    # drop those rows first so the constraint can be re-applied cleanly.
    op.execute("DELETE FROM kb_articles WHERE author_id IS NULL")
    op.alter_column("kb_articles", "author_id", nullable=False)
