"""
SQLAlchemy ORM models for the Notification domain.

Notification rows are written by Celery tasks and the SLA breach detector —
never inline in the request path. Source of truth for in-app unread counts.
All PKs are UUID v7.
"""

import enum
from datetime import datetime
from typing import Optional
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship
from uuid_extensions import uuid7

from app.models.base import Base, TimestampMixin


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class NotificationType(str, enum.Enum):
    ticket_assigned = "ticket_assigned"
    ticket_updated = "ticket_updated"
    ticket_commented = "ticket_commented"
    ticket_sla_warning = "ticket_sla_warning"
    ticket_sla_breached = "ticket_sla_breached"
    asset_maintenance_due = "asset_maintenance_due"
    kb_article_published = "kb_article_published"
    mention = "mention"
    system = "system"
    recording_linked = "recording_linked"
    recording_required_missing = "recording_required_missing"
    recording_inaccessible = "recording_inaccessible"
    recording_consent_missing = "recording_consent_missing"
    recording_summary_ready = "recording_summary_ready"
    rca_required = "rca_required"
    rca_due_soon = "rca_due_soon"
    rca_overdue = "rca_overdue"
    rca_submitted = "rca_submitted"
    rca_rejected = "rca_rejected"
    rca_approved = "rca_approved"
    rca_action_assigned = "rca_action_assigned"
    rca_action_overdue = "rca_action_overdue"
    rca_completed = "rca_completed"


# SQLAlchemy native DB enum — create_type=False because migrations create it explicitly
_notification_type_enum = sa.Enum(
    NotificationType,
    name="notificationtype",
    values_callable=lambda e: [m.value for m in e],
    create_type=False,
)


# ---------------------------------------------------------------------------
# Notification
# ---------------------------------------------------------------------------


class Notification(Base):
    """In-app notification for a specific user.

    Written by Celery tasks and the SLA breach detector — never synchronously
    in the API request path.  DB is the source of truth; Celery dispatches the
    corresponding email / Socket.IO push after writing this row.
    """

    __tablename__ = "notifications"
    __table_args__ = (
        sa.Index(
            "ix_notifications_user_unread",
            "user_id",
            "is_read",
            sa.text("created_at DESC"),
        ),
        sa.Index(
            "ix_notifications_tenant",
            "tenant_id",
            sa.text("created_at DESC"),
        ),
    )

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    type: Mapped[NotificationType] = mapped_column(_notification_type_enum, nullable=False)
    title: Mapped[str] = mapped_column(sa.VARCHAR(500), nullable=False)
    body: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    entity_type: Mapped[Optional[str]] = mapped_column(
        sa.VARCHAR(50), nullable=True
    )  # "ticket" | "asset" | "kb_article"
    entity_id: Mapped[Optional[UUID]] = mapped_column(sa.UUID(as_uuid=True), nullable=True)
    is_read: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.text("false")
    )
    read_at: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )

    tenant: Mapped["Tenant"] = relationship("Tenant")  # type: ignore[name-defined]
    user: Mapped["User"] = relationship("User")  # type: ignore[name-defined]


# ---------------------------------------------------------------------------
# NotificationPreference
# ---------------------------------------------------------------------------


class NotificationPreference(Base):
    """Per-user, per-type delivery preferences.

    If no row exists for a user+type combination the default is: both
    email_enabled and in_app_enabled are treated as True.
    """

    __tablename__ = "notification_preferences"

    user_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    tenant_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    type: Mapped[NotificationType] = mapped_column(
        _notification_type_enum, primary_key=True
    )
    email_enabled: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.text("true")
    )
    in_app_enabled: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.text("true")
    )

    user: Mapped["User"] = relationship("User")  # type: ignore[name-defined]
    tenant: Mapped["Tenant"] = relationship("Tenant")  # type: ignore[name-defined]


__all__ = [
    "NotificationType",
    "Notification",
    "NotificationPreference",
]
