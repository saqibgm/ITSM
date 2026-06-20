"""Knowledge Base tables.

Revision ID: 0005_kb
Revises: 0003b_ai_reply_suggestions
Create Date: 2026-06-05

Creates all KB tables in FK dependency order, GIN index on search_vector,
HNSW index on embedding (pgvector), and the tsvector trigger.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, ENUM

revision = "0005_kb"
down_revision = "0004b_ai_maintenance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # Enum types
    # ------------------------------------------------------------------
    op.execute(text(
    "DO $$ BEGIN "
    "CREATE TYPE kbspacescope AS ENUM ('tenant_wide', 'product_specific'); "
    "EXCEPTION WHEN duplicate_object THEN null; END $$"
))
    op.execute(text(
    "DO $$ BEGIN "
    "CREATE TYPE kbarticlestatus AS ENUM ('draft', 'under_review', 'published', 'archived'); "
    "EXCEPTION WHEN duplicate_object THEN null; END $$"
))
    op.execute(text(
    "DO $$ BEGIN "
    "CREATE TYPE kbarticlevisibility AS ENUM ('public', 'internal', 'agents_only'); "
    "EXCEPTION WHEN duplicate_object THEN null; END $$"
))

    # ------------------------------------------------------------------
    # kb_spaces
    # ------------------------------------------------------------------
    op.create_table(
        "kb_spaces",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
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
        sa.Column("name", sa.VARCHAR(255), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("slug", sa.VARCHAR(100), nullable=False),
        sa.Column(
            "scope",
            ENUM(
                "tenant_wide", "product_specific",
                name="kbspacescope",
                create_type=False,
            ),
            nullable=False,
            server_default=sa.text("'tenant_wide'"),
        ),
        sa.Column(
            "is_public",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "is_active",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("true"),
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
        "uq_kb_spaces_tenant_slug", "kb_spaces", ["tenant_id", "slug"]
    )
    op.create_index("ix_kb_spaces_tenant_id", "kb_spaces", ["tenant_id"])
    op.create_index("ix_kb_spaces_product_id", "kb_spaces", ["product_id"])

    # ------------------------------------------------------------------
    # kb_categories
    # ------------------------------------------------------------------
    op.create_table(
        "kb_categories",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "space_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("kb_spaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.VARCHAR(255), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column(
            "parent_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("kb_categories.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "display_order",
            sa.Integer,
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "is_active",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("true"),
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
    op.create_index("ix_kb_categories_space_id", "kb_categories", ["space_id"])
    op.create_index("ix_kb_categories_parent_id", "kb_categories", ["parent_id"])

    # ------------------------------------------------------------------
    # kb_articles
    # ------------------------------------------------------------------
    op.create_table(
        "kb_articles",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "space_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("kb_spaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "category_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("kb_categories.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("title", sa.VARCHAR(500), nullable=False),
        sa.Column("slug", sa.VARCHAR(255), nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("excerpt", sa.Text, nullable=True),
        sa.Column(
            "status",
            ENUM(
                "draft", "under_review", "published", "archived",
                name="kbarticlestatus",
                create_type=False,
            ),
            nullable=False,
            server_default=sa.text("'draft'"),
        ),
        sa.Column(
            "visibility",
            ENUM(
                "public", "internal", "agents_only",
                name="kbarticlevisibility",
                create_type=False,
            ),
            nullable=False,
            server_default=sa.text("'internal'"),
        ),
        sa.Column(
            "author_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "last_edited_by",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("version", sa.Integer, nullable=False, server_default=sa.text("1")),
        sa.Column("view_count", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("helpful_count", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column(
            "not_helpful_count", sa.Integer, nullable=False, server_default=sa.text("0")
        ),
        sa.Column("published_at", sa.TIMESTAMP(timezone=True), nullable=True),
        # Updated by PostgreSQL trigger below — nullable until first INSERT
        sa.Column("search_vector", TSVECTOR, nullable=True),
        # pgvector HNSW index created separately (try/except)
        sa.Column("embedding", sa.Text, nullable=True),  # placeholder; altered post-extension
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
    )
    op.create_unique_constraint(
        "uq_kb_articles_space_slug", "kb_articles", ["space_id", "slug"]
    )
    op.create_index("ix_kb_articles_tenant_id", "kb_articles", ["tenant_id"])
    op.create_index("ix_kb_articles_space_id", "kb_articles", ["space_id"])
    op.create_index(
        "ix_kb_articles_status_visibility",
        "kb_articles",
        ["tenant_id", "status", "visibility"],
    )
    op.create_index("ix_kb_articles_author_id", "kb_articles", ["author_id"])

    # GIN index for PostgreSQL full-text search
    op.execute(text(
        "CREATE INDEX ix_kb_articles_search_vector "
        "ON kb_articles USING gin(search_vector)"
    ))

    # Alter embedding column to VECTOR(1536) if pgvector extension is available
    try:
        op.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        op.execute(text(
            "ALTER TABLE kb_articles "
            "ALTER COLUMN embedding TYPE vector(1536) USING NULL"
        ))
        # HNSW index for approximate nearest-neighbour semantic search
        op.execute(text(
            "CREATE INDEX ix_kb_articles_embedding_hnsw "
            "ON kb_articles USING hnsw (embedding vector_cosine_ops)"
        ))
    except Exception:
        # pgvector not installed — embedding stays as TEXT, no HNSW index
        pass

    # ------------------------------------------------------------------
    # tsvector trigger function + trigger
    # ------------------------------------------------------------------
    op.execute(text("""
CREATE OR REPLACE FUNCTION kb_articles_search_vector_update() RETURNS trigger AS $$
BEGIN
  NEW.search_vector :=
    setweight(to_tsvector('english', coalesce(NEW.title, '')), 'A') ||
    setweight(to_tsvector('english', coalesce(NEW.body,  '')), 'B');
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""))

    op.execute(text("""
CREATE TRIGGER kb_articles_search_vector_trigger
BEFORE INSERT OR UPDATE ON kb_articles
FOR EACH ROW EXECUTE FUNCTION kb_articles_search_vector_update();
"""))

    # ------------------------------------------------------------------
    # kb_article_versions
    # ------------------------------------------------------------------
    op.create_table(
        "kb_article_versions",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "article_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("kb_articles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version_number", sa.Integer, nullable=False),
        sa.Column("title", sa.VARCHAR(500), nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column(
            "changed_by",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("change_summary", sa.VARCHAR(500), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_kb_article_versions_article_id", "kb_article_versions", ["article_id"]
    )
    op.create_index(
        "ix_kb_article_versions_article_version",
        "kb_article_versions",
        ["article_id", "version_number"],
    )

    # ------------------------------------------------------------------
    # kb_tags
    # ------------------------------------------------------------------
    op.create_table(
        "kb_tags",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.VARCHAR(100), nullable=False),
    )
    op.create_unique_constraint("uq_kb_tags_tenant_name", "kb_tags", ["tenant_id", "name"])
    op.create_index("ix_kb_tags_tenant_id", "kb_tags", ["tenant_id"])

    # ------------------------------------------------------------------
    # kb_article_tag_assignments  (M:N)
    # ------------------------------------------------------------------
    op.create_table(
        "kb_article_tag_assignments",
        sa.Column(
            "article_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("kb_articles.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "tag_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("kb_tags.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )

    # ------------------------------------------------------------------
    # kb_article_feedback
    # ------------------------------------------------------------------
    op.create_table(
        "kb_article_feedback",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "article_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("kb_articles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("is_helpful", sa.Boolean, nullable=False),
        sa.Column("comment", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_kb_article_feedback_article_id", "kb_article_feedback", ["article_id"]
    )

    # ------------------------------------------------------------------
    # kb_article_attachments
    # ------------------------------------------------------------------
    op.create_table(
        "kb_article_attachments",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "article_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("kb_articles.id", ondelete="CASCADE"),
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
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_kb_article_attachments_article_id",
        "kb_article_attachments",
        ["article_id"],
    )

    # ------------------------------------------------------------------
    # ticket_kb_links  (M:N — tickets ↔ kb_articles)
    # ------------------------------------------------------------------
    op.create_table(
        "ticket_kb_links",
        sa.Column(
            "ticket_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("tickets.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "article_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("kb_articles.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "linked_by",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "is_suggested",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "linked_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_ticket_kb_links_ticket_id", "ticket_kb_links", ["ticket_id"]
    )
    op.create_index(
        "ix_ticket_kb_links_article_id", "ticket_kb_links", ["article_id"]
    )


def downgrade() -> None:
    # Drop in reverse FK dependency order
    op.drop_index("ix_ticket_kb_links_article_id", table_name="ticket_kb_links")
    op.drop_index("ix_ticket_kb_links_ticket_id", table_name="ticket_kb_links")
    op.drop_table("ticket_kb_links")

    op.drop_index(
        "ix_kb_article_attachments_article_id", table_name="kb_article_attachments"
    )
    op.drop_table("kb_article_attachments")

    op.drop_index("ix_kb_article_feedback_article_id", table_name="kb_article_feedback")
    op.drop_table("kb_article_feedback")

    op.drop_table("kb_article_tag_assignments")

    op.drop_index("ix_kb_tags_tenant_id", table_name="kb_tags")
    op.drop_table("kb_tags")

    op.drop_index(
        "ix_kb_article_versions_article_version", table_name="kb_article_versions"
    )
    op.drop_index(
        "ix_kb_article_versions_article_id", table_name="kb_article_versions"
    )
    op.drop_table("kb_article_versions")

    # Drop trigger and function before dropping the table
    op.execute(text(
        "DROP TRIGGER IF EXISTS kb_articles_search_vector_trigger ON kb_articles"
    ))
    op.execute(text(
        "DROP FUNCTION IF EXISTS kb_articles_search_vector_update()"
    ))

    # Drop indexes on kb_articles
    op.execute(text("DROP INDEX IF EXISTS ix_kb_articles_embedding_hnsw"))
    op.execute(text("DROP INDEX IF EXISTS ix_kb_articles_search_vector"))
    op.drop_index("ix_kb_articles_author_id", table_name="kb_articles")
    op.drop_index("ix_kb_articles_status_visibility", table_name="kb_articles")
    op.drop_index("ix_kb_articles_space_id", table_name="kb_articles")
    op.drop_index("ix_kb_articles_tenant_id", table_name="kb_articles")
    op.drop_table("kb_articles")

    op.drop_index("ix_kb_categories_parent_id", table_name="kb_categories")
    op.drop_index("ix_kb_categories_space_id", table_name="kb_categories")
    op.drop_table("kb_categories")

    op.drop_index("ix_kb_spaces_product_id", table_name="kb_spaces")
    op.drop_index("ix_kb_spaces_tenant_id", table_name="kb_spaces")
    op.drop_table("kb_spaces")

    op.execute(text("DROP TYPE IF EXISTS kbarticlevisibility"))
    op.execute(text("DROP TYPE IF EXISTS kbarticlestatus"))
    op.execute(text("DROP TYPE IF EXISTS kbspacescope"))
