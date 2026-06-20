"""Pydantic request/response schemas for the Knowledge Base module."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.kb import (
    KBArticleAccessLevel,
    KBArticleStatus,
    KBArticleVisibility,
    KBSpaceScope,
)


# ---------------------------------------------------------------------------
# Spaces
# ---------------------------------------------------------------------------


class CreateKBSpaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    slug: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9-]+$")
    description: str | None = None
    scope: KBSpaceScope = KBSpaceScope.tenant_wide
    product_id: UUID | None = None
    is_public: bool = False


class UpdateKBSpaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    is_public: bool | None = None
    is_active: bool | None = None
    product_id: UUID | None = None


class KBSpaceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID | None
    name: str
    slug: str
    scope: KBSpaceScope
    is_public: bool
    is_active: bool
    product_id: UUID | None
    description: str | None
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------


class CreateKBCategoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    parent_id: UUID | None = None
    display_order: int = Field(default=0, ge=0)


class UpdateKBCategoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    parent_id: UUID | None = None
    display_order: int | None = Field(default=None, ge=0)
    is_active: bool | None = None


class KBCategoryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    space_id: UUID
    name: str
    description: str | None
    parent_id: UUID | None
    display_order: int
    is_active: bool
    created_at: datetime


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------


class CreateKBTagRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)


class UpdateKBTagRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)


class KBTagResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    name: str


class AssignKBTagRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tag_id: UUID


# ---------------------------------------------------------------------------
# Article attachments
# ---------------------------------------------------------------------------


class CreateKBArticleAttachmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: str = Field(min_length=1, max_length=500)
    file_size: int = Field(gt=0)
    mime_type: str = Field(min_length=1, max_length=255)


class KBAttachmentPresignResponse(BaseModel):
    upload_url: str
    attachment_id: UUID
    expires_in: int


class KBArticleAttachmentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    article_id: UUID
    uploaded_by: UUID
    filename: str
    storage_url: str
    file_size: int
    mime_type: str
    created_at: datetime


# ---------------------------------------------------------------------------
# Articles
# ---------------------------------------------------------------------------


class CreateKBArticleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    space_id: UUID
    title: str = Field(min_length=3, max_length=500)
    body: str = Field(min_length=10)
    category_id: UUID | None = None
    visibility: KBArticleVisibility = KBArticleVisibility.internal
    excerpt: str | None = None
    # Access level governs the tenant/product scope (see KBArticleAccessLevel).
    # When omitted it is inferred from product_id (product set → tenant_product,
    # else tenant) for backward compatibility. The `global` and `product` levels
    # (tenant_id NULL, cross-tenant) are platform-admin only.
    access_level: KBArticleAccessLevel | None = None
    # Required when access_level is `product` or `tenant_product`; ignored
    # otherwise. The server clears it for `global` / `tenant` levels.
    product_id: UUID | None = None


class UpdateKBArticleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=3, max_length=500)
    body: str | None = None
    category_id: UUID | None = None
    visibility: KBArticleVisibility | None = None
    excerpt: str | None = None
    # Change the article's scope. When provided, product_id is interpreted
    # against the new access_level (required for product/tenant_product).
    access_level: KBArticleAccessLevel | None = None
    product_id: UUID | None = None


class KBArticleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID | None
    product_id: UUID | None
    access_level: KBArticleAccessLevel
    title: str
    slug: str
    status: KBArticleStatus
    visibility: KBArticleVisibility
    space_id: UUID
    category_id: UUID | None
    author_id: UUID | None
    last_edited_by: UUID | None
    version: int
    view_count: int
    helpful_count: int
    not_helpful_count: int
    excerpt: str | None
    published_at: datetime | None
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


class KBArticleSearchResult(BaseModel):
    article: KBArticleResponse
    score: float
    match_type: str  # "fts" | "semantic" | "hybrid"


# ---------------------------------------------------------------------------
# Feedback
# ---------------------------------------------------------------------------


class KBFeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_helpful: bool
    comment: str | None = Field(default=None, max_length=2000)


class KBFeedbackResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    article_id: UUID
    is_helpful: bool
    comment: str | None
    created_at: datetime


# ---------------------------------------------------------------------------
# Article versions
# ---------------------------------------------------------------------------


class KBArticleVersionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    version_number: int
    title: str
    change_summary: str | None
    changed_by: UUID
    created_at: datetime


# ---------------------------------------------------------------------------
# Ticket-KB link
# ---------------------------------------------------------------------------


class LinkTicketToArticleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticket_id: UUID


class TicketKBLinkResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ticket_id: UUID
    article_id: UUID
    linked_by: UUID | None
    is_suggested: bool
    linked_at: datetime
