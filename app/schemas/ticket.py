"""Pydantic v2 request/response schemas for the Ticket domain."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.ticket import TicketPriority, TicketStatus, TicketType


# ---------------------------------------------------------------------------
# Comment schemas
# ---------------------------------------------------------------------------


class CreateCommentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1, max_length=50_000)
    is_internal: bool = False
    parent_comment_id: UUID | None = None


class UpdateCommentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1, max_length=50_000)


class CommentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    ticket_id: UUID
    author_id: UUID
    # Display name of the author, resolved from the users table in the API layer
    # (None when the user can't be found). Lets clients show a name, not a UUID.
    author_name: str | None = None
    body: str
    is_internal: bool
    parent_comment_id: UUID | None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None


# ---------------------------------------------------------------------------
# Attachment schemas
# ---------------------------------------------------------------------------


class CreateAttachmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: str = Field(min_length=1, max_length=255)
    mime_type: str = Field(min_length=1, max_length=255)
    file_size: int = Field(ge=0, le=25 * 1024 * 1024)


class AttachmentPresignResponse(BaseModel):
    attachment_id: UUID
    upload_url: str        # presigned S3 PUT URL
    storage_key: str
    expires_in_seconds: int


class AttachmentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    filename: str
    mime_type: str
    file_size: int
    created_at: datetime
    download_url: str | None = None  # short-lived presigned GET URL (view/download)


# ---------------------------------------------------------------------------
# Tag schemas
# ---------------------------------------------------------------------------


class CreateTagRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=50)
    color: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")


class TagResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    color: str


class AssignTagRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tag_id: UUID


# ---------------------------------------------------------------------------
# Watcher schemas
# ---------------------------------------------------------------------------


class AddWatcherRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: UUID


class WatcherResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: UUID
    added_at: datetime


class CreateTicketRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=3, max_length=500)
    description: str = Field(min_length=10, max_length=50_000)
    type: TicketType
    priority: TicketPriority
    category_id: UUID | None = None
    product_id: UUID | None = None
    # The bot/UI may pass the session product by slug (+ display name) instead of a
    # UUID; the service upserts a Product row for (tenant, slug) and links it.
    # Both null = no product selected → the ticket has no product (shown blank).
    product_slug: str | None = Field(default=None, max_length=100)
    product_name: str | None = Field(default=None, max_length=255)
    department_id: UUID | None = None


class UpdateTicketRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: TicketStatus | None = None
    priority: TicketPriority | None = None
    title: str | None = Field(default=None, min_length=3, max_length=500)
    description: str | None = None
    assignee_id: UUID | None = None
    team_id: UUID | None = None
    category_id: UUID | None = None
    due_date: datetime | None = None


class TicketResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    ticket_number: str
    type: TicketType
    status: TicketStatus
    priority: TicketPriority
    title: str
    description: str
    requester_id: UUID
    # Display name / email of the requester and assignee, resolved from the users
    # table in the API layer (None when not found). Clients show these instead of
    # the raw UUIDs; the ids remain available for linking.
    requester_name: str | None = None
    requester_email: str | None = None
    assignee_id: UUID | None
    assignee_name: str | None = None
    team_id: UUID | None
    category_id: UUID | None
    # Product the ticket is scoped to. product_name is resolved from the products
    # table in the API layer; both are null when no product was selected (blank).
    product_id: UUID | None = None
    product_name: str | None = None
    sla_response_due: datetime | None
    sla_resolve_due: datetime | None
    sla_breached: bool
    resolved_at: datetime | None
    closed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class TicketHistoryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    ticket_id: UUID
    actor_id: UUID | None
    field_changed: str
    old_value: dict | None
    new_value: dict | None
    changed_at: datetime
