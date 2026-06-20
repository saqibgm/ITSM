"""Pydantic v2 request/response schemas for the Notification domain."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.notification import NotificationType


# ---------------------------------------------------------------------------
# Response schemas (ORM → API)
# ---------------------------------------------------------------------------


class NotificationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    type: NotificationType
    title: str
    body: str | None
    entity_type: str | None
    entity_id: UUID | None
    is_read: bool
    read_at: datetime | None
    created_at: datetime


class NotificationPreferenceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    type: NotificationType
    email_enabled: bool
    in_app_enabled: bool


# ---------------------------------------------------------------------------
# Request schemas (API → service)
# ---------------------------------------------------------------------------


class MarkReadRequest(BaseModel):
    """Body for PATCH /notifications/mark-read."""

    model_config = ConfigDict(extra="forbid")

    ids: list[UUID] | None = Field(
        default=None,
        description="Specific notification IDs to mark as read. Mutually exclusive with `all`.",
    )
    all: bool = Field(
        default=False,
        description="If true, mark all unread notifications as read.",
    )


class MarkReadResponse(BaseModel):
    marked: int


class UpdateNotificationPreferenceRequest(BaseModel):
    """Body for PATCH /notifications/preferences."""

    model_config = ConfigDict(extra="forbid")

    type: NotificationType
    email_enabled: bool
    in_app_enabled: bool
