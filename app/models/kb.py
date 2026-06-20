"""
SQLAlchemy ORM models for the Knowledge Base domain.

All PKs are UUID v7 (sortable, time-prefixed).
search_vector is updated by a PostgreSQL trigger (not application code).

Exception to the platform-wide "tenant_id is never NULL" rule: KBSpace and
KBArticle allow NULL tenant_id (and NULL product_id) to support globally-
shared and product-shared knowledge base content. See KBSpace docstring for
the four resulting scoping levels. All other KB entities (categories, tags,
versions, feedback, attachments, links) remain tenant-scoped via their parent
article/space and are unaffected.
"""

import enum
from datetime import datetime
from typing import Optional
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship
from uuid_extensions import uuid7

from app.models.base import Base, TimestampMixin

try:
    from pgvector.sqlalchemy import Vector
except ImportError:
    Vector = None


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class KBArticleStatus(str, enum.Enum):
    draft = "draft"
    under_review = "under_review"
    published = "published"
    archived = "archived"


class KBArticleVisibility(str, enum.Enum):
    public = "public"
    internal = "internal"
    agents_only = "agents_only"


class KBSpaceScope(str, enum.Enum):
    tenant_wide = "tenant_wide"
    product_specific = "product_specific"


class KBArticleAccessLevel(str, enum.Enum):
    """Derived from (tenant_id, product_id) — the article's visibility scope.

      - global          tenant NULL,     product NULL     -> visible to all tenants/products
      - product         tenant NULL,     product NOT NULL -> any tenant, that product only
      - tenant          tenant NOT NULL, product NULL     -> that tenant, all products
      - tenant_product  tenant NOT NULL, product NOT NULL -> that tenant, that product only
    """

    global_ = "global"
    product = "product"
    tenant = "tenant"
    tenant_product = "tenant_product"


# ---------------------------------------------------------------------------
# KBSpace
# ---------------------------------------------------------------------------


class KBSpace(Base, TimestampMixin):
    """A knowledge base space.

    tenant_id and product_id are both nullable, giving four scoping levels:
      - both NULL          -> global (visible to all tenants/products)
      - product_id only    -> product-specific, cross-tenant
      - tenant_id only     -> tenant-wide
      - both set           -> tenant + product specific

    Slug uniqueness is enforced via partial unique indexes (see migration
    0011_kb_global_scoping) rather than a single UniqueConstraint, since
    NULL tenant_id values are not considered equal by standard constraints.
    """

    __tablename__ = "kb_spaces"

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    product_id: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("products.id", ondelete="SET NULL"),
        nullable=True,
    )
    name: Mapped[str] = mapped_column(sa.VARCHAR(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    slug: Mapped[str] = mapped_column(sa.VARCHAR(100), nullable=False)
    scope: Mapped[KBSpaceScope] = mapped_column(
        sa.Enum(KBSpaceScope, name="kbspacescope", create_type=False),
        nullable=False,
        default=KBSpaceScope.tenant_wide,
    )
    is_public: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False, server_default=sa.text("false")
    )
    is_active: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=True, server_default=sa.text("true")
    )

    # relationships
    tenant: Mapped["Tenant"] = relationship("Tenant")  # type: ignore[name-defined]
    product: Mapped[Optional["Product"]] = relationship("Product")  # type: ignore[name-defined]
    categories: Mapped[list["KBCategory"]] = relationship(
        "KBCategory", back_populates="space", cascade="all, delete-orphan"
    )
    articles: Mapped[list["KBArticle"]] = relationship(
        "KBArticle", back_populates="space", cascade="all, delete-orphan"
    )


# ---------------------------------------------------------------------------
# KBCategory
# ---------------------------------------------------------------------------


class KBCategory(Base, TimestampMixin):
    """Hierarchical category within a KB space."""

    __tablename__ = "kb_categories"

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    space_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("kb_spaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(sa.VARCHAR(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    parent_id: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("kb_categories.id", ondelete="SET NULL"),
        nullable=True,
    )
    display_order: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=0, server_default=sa.text("0")
    )
    is_active: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=True, server_default=sa.text("true")
    )

    space: Mapped["KBSpace"] = relationship("KBSpace", back_populates="categories")
    children: Mapped[list["KBCategory"]] = relationship(
        "KBCategory",
        back_populates="parent",
        cascade="all",
        foreign_keys="KBCategory.parent_id",
    )
    parent: Mapped[Optional["KBCategory"]] = relationship(
        "KBCategory",
        back_populates="children",
        foreign_keys="KBCategory.parent_id",
        remote_side="KBCategory.id",
    )
    articles: Mapped[list["KBArticle"]] = relationship(
        "KBArticle", back_populates="category"
    )


# ---------------------------------------------------------------------------
# KBArticle
# ---------------------------------------------------------------------------


class KBArticle(Base, TimestampMixin):
    """A knowledge base article. search_vector updated by DB trigger.

    tenant_id and product_id are independently nullable (see KBSpace
    docstring for the four scoping levels this enables). An article's
    scope is set explicitly on the article — it does not need to match
    its space's scope, though in practice they usually align.
    """

    __tablename__ = "kb_articles"
    __table_args__ = (
        sa.UniqueConstraint("space_id", "slug", name="uq_kb_articles_space_slug"),
        sa.Index("ix_kb_articles_search_vector", "search_vector", postgresql_using="gin"),
        sa.Index("ix_kb_articles_tenant_id", "tenant_id"),
        sa.Index("ix_kb_articles_product_id", "product_id"),
        sa.Index("ix_kb_articles_space_id", "space_id"),
        sa.Index(
            "ix_kb_articles_scope_status_visibility",
            "tenant_id", "product_id", "status", "visibility",
        ),
    )

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=True,
    )
    product_id: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("products.id", ondelete="SET NULL"),
        nullable=True,
    )
    space_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("kb_spaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    category_id: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("kb_categories.id", ondelete="SET NULL"),
        nullable=True,
    )
    title: Mapped[str] = mapped_column(sa.VARCHAR(500), nullable=False)
    slug: Mapped[str] = mapped_column(sa.VARCHAR(255), nullable=False)
    body: Mapped[str] = mapped_column(sa.Text, nullable=False)
    excerpt: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    status: Mapped[KBArticleStatus] = mapped_column(
        sa.Enum(KBArticleStatus, name="kbarticlestatus", create_type=False),
        nullable=False,
        default=KBArticleStatus.draft,
        server_default=sa.text("'draft'"),
    )
    visibility: Mapped[KBArticleVisibility] = mapped_column(
        sa.Enum(KBArticleVisibility, name="kbarticlevisibility", create_type=False),
        nullable=False,
        default=KBArticleVisibility.internal,
        server_default=sa.text("'internal'"),
    )
    # Nullable: global / product-wide (cross-tenant) articles are authored by
    # platform users who have no local users row mirror (see migration 0016).
    author_id: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    last_edited_by: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    version: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=1, server_default=sa.text("1")
    )
    view_count: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=0, server_default=sa.text("0")
    )
    helpful_count: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=0, server_default=sa.text("0")
    )
    not_helpful_count: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=0, server_default=sa.text("0")
    )
    published_at: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    # PostgreSQL FTS — updated by DB trigger (not application code)
    search_vector: Mapped[Optional[object]] = mapped_column(
        TSVECTOR, nullable=True
    )
    # pgvector hybrid search — HNSW index added in migration
    embedding: Mapped[Optional[object]] = mapped_column(
        Vector(1536) if Vector is not None else sa.Text,
        nullable=True,
    )
    metadata_: Mapped[dict] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        default=dict,
        server_default=sa.text("'{}'::jsonb"),
    )

    # relationships
    space: Mapped["KBSpace"] = relationship("KBSpace", back_populates="articles")
    category: Mapped[Optional["KBCategory"]] = relationship(
        "KBCategory", back_populates="articles"
    )
    author: Mapped["User"] = relationship(  # type: ignore[name-defined]
        "User", foreign_keys=[author_id]
    )
    editor: Mapped[Optional["User"]] = relationship(  # type: ignore[name-defined]
        "User", foreign_keys=[last_edited_by]
    )
    versions: Mapped[list["KBArticleVersion"]] = relationship(
        "KBArticleVersion", back_populates="article", cascade="all, delete-orphan"
    )
    tag_assignments: Mapped[list["KBArticleTagAssignment"]] = relationship(
        "KBArticleTagAssignment", back_populates="article", cascade="all, delete-orphan"
    )
    feedback: Mapped[list["KBArticleFeedback"]] = relationship(
        "KBArticleFeedback", back_populates="article", cascade="all, delete-orphan"
    )
    attachments: Mapped[list["KBArticleAttachment"]] = relationship(
        "KBArticleAttachment", back_populates="article", cascade="all, delete-orphan"
    )
    ticket_links: Mapped[list["TicketKBLink"]] = relationship(
        "TicketKBLink", back_populates="article", cascade="all, delete-orphan"
    )

    @property
    def access_level(self) -> KBArticleAccessLevel:
        """Derive the access level from the (tenant_id, product_id) pair."""
        if self.tenant_id is None:
            return (
                KBArticleAccessLevel.global_
                if self.product_id is None
                else KBArticleAccessLevel.product
            )
        return (
            KBArticleAccessLevel.tenant
            if self.product_id is None
            else KBArticleAccessLevel.tenant_product
        )


# ---------------------------------------------------------------------------
# KBArticleVersion
# ---------------------------------------------------------------------------


class KBArticleVersion(Base):
    """Immutable snapshot of a KB article at a given version number."""

    __tablename__ = "kb_article_versions"

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    article_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("kb_articles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version_number: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    title: Mapped[str] = mapped_column(sa.VARCHAR(500), nullable=False)
    body: Mapped[str] = mapped_column(sa.Text, nullable=False)
    changed_by: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    change_summary: Mapped[Optional[str]] = mapped_column(sa.VARCHAR(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )

    article: Mapped["KBArticle"] = relationship("KBArticle", back_populates="versions")
    changer: Mapped["User"] = relationship("User", foreign_keys=[changed_by])  # type: ignore[name-defined]


# ---------------------------------------------------------------------------
# KBTag
# ---------------------------------------------------------------------------


class KBTag(Base, TimestampMixin):
    """Tenant-scoped tag for KB articles."""

    __tablename__ = "kb_tags"
    __table_args__ = (
        sa.UniqueConstraint("tenant_id", "name", name="uq_kb_tags_tenant_name"),
    )

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(sa.VARCHAR(100), nullable=False)

    assignments: Mapped[list["KBArticleTagAssignment"]] = relationship(
        "KBArticleTagAssignment", back_populates="tag", cascade="all, delete-orphan"
    )


# ---------------------------------------------------------------------------
# KBArticleTagAssignment  (M:N)
# ---------------------------------------------------------------------------


class KBArticleTagAssignment(Base):
    """Links a KB article to a tag."""

    __tablename__ = "kb_article_tag_assignments"

    article_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("kb_articles.id", ondelete="CASCADE"),
        primary_key=True,
    )
    tag_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("kb_tags.id", ondelete="CASCADE"),
        primary_key=True,
    )

    article: Mapped["KBArticle"] = relationship(
        "KBArticle", back_populates="tag_assignments"
    )
    tag: Mapped["KBTag"] = relationship("KBTag", back_populates="assignments")


# ---------------------------------------------------------------------------
# KBArticleFeedback
# ---------------------------------------------------------------------------


class KBArticleFeedback(Base):
    """Thumbs-up / thumbs-down feedback on a published article.

    user_id is nullable to allow anonymous feedback on public articles.
    """

    __tablename__ = "kb_article_feedback"

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    article_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("kb_articles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    is_helpful: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    comment: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )

    article: Mapped["KBArticle"] = relationship("KBArticle", back_populates="feedback")
    user: Mapped[Optional["User"]] = relationship("User", foreign_keys=[user_id])  # type: ignore[name-defined]


# ---------------------------------------------------------------------------
# KBArticleAttachment
# ---------------------------------------------------------------------------


class KBArticleAttachment(Base):
    """File attachment on a KB article."""

    __tablename__ = "kb_article_attachments"

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    article_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("kb_articles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    uploaded_by: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    filename: Mapped[str] = mapped_column(sa.VARCHAR(500), nullable=False)
    storage_url: Mapped[str] = mapped_column(sa.VARCHAR(2048), nullable=False)
    file_size: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    mime_type: Mapped[str] = mapped_column(sa.VARCHAR(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )

    article: Mapped["KBArticle"] = relationship("KBArticle", back_populates="attachments")
    uploader: Mapped["User"] = relationship("User", foreign_keys=[uploaded_by])  # type: ignore[name-defined]


# ---------------------------------------------------------------------------
# TicketKBLink  (M:N — tickets ↔ kb_articles)
# ---------------------------------------------------------------------------


class TicketKBLink(Base):
    """Links a ticket to a KB article (manually or AI-suggested).

    ticket_id references the tickets table which is defined in ticket.py.
    No ORM back-reference to Ticket here to avoid circular imports;
    use explicit queries when traversing from Ticket side.
    """

    __tablename__ = "ticket_kb_links"

    ticket_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("tickets.id", ondelete="CASCADE"),
        primary_key=True,
    )
    article_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("kb_articles.id", ondelete="CASCADE"),
        primary_key=True,
    )
    linked_by: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    is_suggested: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False, server_default=sa.text("false")
    )
    linked_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )

    article: Mapped["KBArticle"] = relationship("KBArticle", back_populates="ticket_links")
    linker: Mapped[Optional["User"]] = relationship("User", foreign_keys=[linked_by])  # type: ignore[name-defined]


# ---------------------------------------------------------------------------
# SQLAlchemy event listener stub (tsvector is updated by DB trigger)
# ---------------------------------------------------------------------------
# The search_vector column is maintained by a PostgreSQL trigger defined in
# migration 0005_kb.py, which is more reliable than a SQLAlchemy event for
# server-computed columns. The stub below documents this contract.

# from sqlalchemy import event
#
# @event.listens_for(KBArticle, "before_insert")
# @event.listens_for(KBArticle, "before_update")
# def _update_search_vector(mapper, connection, target):
#     """No-op: tsvector is updated by the kb_articles_search_vector_trigger DB trigger."""
#     pass
