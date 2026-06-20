"""
SQLAlchemy ORM models for the Ticketing domain.

All PKs are UUID v7. Tables are tenant-scoped. ticket_history is immutable
(INSERT only) — UPDATE/DELETE revoked in migration.
"""

import enum
from datetime import datetime, time
from typing import Optional
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from uuid_extensions import uuid7

from app.models.base import Base, TimestampMixin
from app.models.ticket_transitions import TicketStatusTransition  # re-export

try:
    from pgvector.sqlalchemy import Vector
except ImportError:
    Vector = None


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class TicketType(str, enum.Enum):
    incident = "incident"
    service_request = "service_request"
    problem = "problem"
    change = "change"


class TicketStatus(str, enum.Enum):
    open = "open"
    in_progress = "in_progress"
    pending = "pending"
    pending_approval = "pending_approval"
    approved = "approved"
    rejected = "rejected"
    resolved = "resolved"
    closed = "closed"
    cancelled = "cancelled"


class TicketPriority(str, enum.Enum):
    critical = "critical"
    high = "high"
    medium = "medium"
    low = "low"


# SQLAlchemy Enum types — create_type=False because migrations create them explicitly
_ticket_type_enum = sa.Enum(
    TicketType,
    name="tickettype",
    values_callable=lambda e: [m.value for m in e],
    create_type=False,
)
_ticket_status_enum = sa.Enum(
    TicketStatus,
    name="ticketstatus",
    values_callable=lambda e: [m.value for m in e],
    create_type=False,
)
_ticket_priority_enum = sa.Enum(
    TicketPriority,
    name="ticketpriority",
    values_callable=lambda e: [m.value for m in e],
    create_type=False,
)


# ---------------------------------------------------------------------------
# BusinessHoursConfig
# ---------------------------------------------------------------------------


class BusinessHoursConfig(Base, TimestampMixin):
    """Per-tenant working-hours window used by the SLA engine."""

    __tablename__ = "business_hours_config"

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    timezone: Mapped[str] = mapped_column(
        sa.VARCHAR(100), nullable=False, server_default=sa.text("'UTC'")
    )
    work_days: Mapped[list] = mapped_column(
        ARRAY(sa.Integer),
        nullable=False,
        server_default=sa.text("ARRAY[1,2,3,4,5]"),
    )
    work_start_time: Mapped[time] = mapped_column(
        sa.Time, nullable=False, server_default=sa.text("'09:00'")
    )
    work_end_time: Mapped[time] = mapped_column(
        sa.Time, nullable=False, server_default=sa.text("'18:00'")
    )
    holidays: Mapped[list] = mapped_column(
        ARRAY(sa.Date),
        nullable=False,
        server_default=sa.text("ARRAY[]::date[]"),
    )

    tenant: Mapped["Tenant"] = relationship("Tenant")  # type: ignore[name-defined]


# ---------------------------------------------------------------------------
# SLAPolicy
# ---------------------------------------------------------------------------


class SLAPolicy(Base, TimestampMixin):
    """SLA response/resolution windows per tenant."""

    __tablename__ = "sla_policies"

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(sa.VARCHAR(255), nullable=False)
    response_time_minutes: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    resolution_time_minutes: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    business_hours_only: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.text("true")
    )

    tenant: Mapped["Tenant"] = relationship("Tenant")  # type: ignore[name-defined]


# ---------------------------------------------------------------------------
# TicketCategory
# ---------------------------------------------------------------------------


class TicketCategory(Base, TimestampMixin):
    """Hierarchical ticket category; can auto-apply SLA policy and team."""

    __tablename__ = "ticket_categories"

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    # NULL tenant_id = global category, shared by all tenants
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
    parent_id: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("ticket_categories.id", ondelete="SET NULL"),
        nullable=True,
    )
    default_sla_policy_id: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("sla_policies.id", ondelete="SET NULL"),
        nullable=True,
    )
    default_team_id: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("teams.id", ondelete="SET NULL"),
        nullable=True,
    )
    is_active: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.text("true")
    )

    tenant: Mapped["Tenant"] = relationship("Tenant")  # type: ignore[name-defined]
    parent: Mapped[Optional["TicketCategory"]] = relationship(
        "TicketCategory",
        back_populates="children",
        remote_side="TicketCategory.id",
        foreign_keys="TicketCategory.parent_id",
    )
    children: Mapped[list["TicketCategory"]] = relationship(
        "TicketCategory",
        back_populates="parent",
        foreign_keys="TicketCategory.parent_id",
    )
    default_sla_policy: Mapped[Optional["SLAPolicy"]] = relationship(
        "SLAPolicy", foreign_keys=[default_sla_policy_id]
    )
    default_team: Mapped[Optional["Team"]] = relationship(  # type: ignore[name-defined]
        "Team", foreign_keys=[default_team_id]
    )


# ---------------------------------------------------------------------------
# Ticket
# ---------------------------------------------------------------------------


class Ticket(Base, TimestampMixin):
    """Core ticketing entity. Tenant-scoped, UUID v7 PK."""

    __tablename__ = "tickets"
    __table_args__ = (
        sa.Index("ix_tickets_tenant_status_priority", "tenant_id", "status", "priority"),
        sa.Index("ix_tickets_tenant_assignee", "tenant_id", "assignee_id"),
        sa.Index("ix_tickets_tenant_requester", "tenant_id", "requester_id"),
        sa.Index(
            "ix_tickets_tenant_created_at",
            "tenant_id",
            sa.text("created_at DESC"),
        ),
        sa.Index(
            "uq_tickets_tenant_idempotency",
            "tenant_id",
            "idempotency_key",
            unique=True,
            postgresql_where=sa.text("idempotency_key IS NOT NULL"),
        ),
        # ticket_number is a single system-wide sequence (not per-tenant) —
        # globally unique. See migration 0017 / ticket_number_seq.
        sa.Index("uq_tickets_ticket_number", "ticket_number", unique=True),
    )

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    idempotency_key: Mapped[Optional[str]] = mapped_column(sa.VARCHAR(255), nullable=True)
    ticket_number: Mapped[str] = mapped_column(sa.VARCHAR(20), nullable=False)
    tenant_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    product_id: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("products.id", ondelete="SET NULL"),
        nullable=True,
    )
    department_id: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("departments.id", ondelete="SET NULL"),
        nullable=True,
    )
    type: Mapped[TicketType] = mapped_column(_ticket_type_enum, nullable=False)
    status: Mapped[TicketStatus] = mapped_column(
        _ticket_status_enum,
        nullable=False,
        server_default=sa.text("'open'"),
    )
    priority: Mapped[TicketPriority] = mapped_column(_ticket_priority_enum, nullable=False)
    title: Mapped[str] = mapped_column(sa.VARCHAR(500), nullable=False)
    description: Mapped[str] = mapped_column(sa.Text, nullable=False)
    requester_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    assignee_id: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    team_id: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("teams.id", ondelete="SET NULL"),
        nullable=True,
    )
    category_id: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("ticket_categories.id", ondelete="SET NULL"),
        nullable=True,
    )
    sla_policy_id: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("sla_policies.id", ondelete="SET NULL"),
        nullable=True,
    )
    sla_response_due: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    sla_resolve_due: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    sla_breached: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.text("false")
    )
    sla_paused_at: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    sla_paused_duration_sec: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default=sa.text("0")
    )
    parent_ticket_id: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("tickets.id", ondelete="SET NULL"),
        nullable=True,
    )
    due_date: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    resolved_at: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    closed_at: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    description_embedding: Mapped[Optional[object]] = mapped_column(
        Vector(1536) if Vector is not None else sa.Text,
        nullable=True,
    )
    metadata_: Mapped[dict] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        server_default=sa.text("'{}'::jsonb"),
    )

    # Relationships
    tenant: Mapped["Tenant"] = relationship("Tenant")  # type: ignore[name-defined]
    requester: Mapped["User"] = relationship(  # type: ignore[name-defined]
        "User", foreign_keys=[requester_id]
    )
    assignee: Mapped[Optional["User"]] = relationship(  # type: ignore[name-defined]
        "User", foreign_keys=[assignee_id]
    )
    team: Mapped[Optional["Team"]] = relationship(  # type: ignore[name-defined]
        "Team", foreign_keys=[team_id]
    )
    category: Mapped[Optional["TicketCategory"]] = relationship(
        "TicketCategory", foreign_keys=[category_id]
    )
    sla_policy: Mapped[Optional["SLAPolicy"]] = relationship(
        "SLAPolicy", foreign_keys=[sla_policy_id]
    )
    parent_ticket: Mapped[Optional["Ticket"]] = relationship(
        "Ticket",
        back_populates="sub_tickets",
        remote_side="Ticket.id",
        foreign_keys="Ticket.parent_ticket_id",
    )
    sub_tickets: Mapped[list["Ticket"]] = relationship(
        "Ticket",
        back_populates="parent_ticket",
        foreign_keys="Ticket.parent_ticket_id",
    )
    history: Mapped[list["TicketHistory"]] = relationship(
        "TicketHistory", back_populates="ticket", cascade="all, delete-orphan"
    )
    comments: Mapped[list["TicketComment"]] = relationship(
        "TicketComment", back_populates="ticket", cascade="all, delete-orphan"
    )
    attachments: Mapped[list["TicketAttachment"]] = relationship(
        "TicketAttachment", back_populates="ticket", cascade="all, delete-orphan"
    )
    escalations: Mapped[list["TicketEscalation"]] = relationship(
        "TicketEscalation", back_populates="ticket", cascade="all, delete-orphan"
    )
    tag_assignments: Mapped[list["TicketTagAssignment"]] = relationship(
        "TicketTagAssignment", back_populates="ticket", cascade="all, delete-orphan"
    )
    watchers: Mapped[list["TicketWatcher"]] = relationship(
        "TicketWatcher", back_populates="ticket", cascade="all, delete-orphan"
    )


# ---------------------------------------------------------------------------
# TicketHistory  (IMMUTABLE — INSERT only)
# ---------------------------------------------------------------------------


class TicketHistory(Base):
    """Immutable audit log of every field change on a ticket.

    UPDATE and DELETE are revoked from itsm_user in the migration.
    actor_id=NULL means the action was performed by the system (e.g. SLA engine).
    """

    __tablename__ = "ticket_history"
    __table_args__ = (
        sa.Index("ix_ticket_history_ticket_changed", "ticket_id", "changed_at"),
    )

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    ticket_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("tickets.id", ondelete="CASCADE"),
        nullable=False,
    )
    actor_id: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    field_changed: Mapped[str] = mapped_column(sa.VARCHAR(100), nullable=False)
    old_value: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    new_value: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    changed_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )

    ticket: Mapped["Ticket"] = relationship("Ticket", back_populates="history")
    actor: Mapped[Optional["User"]] = relationship(  # type: ignore[name-defined]
        "User", foreign_keys=[actor_id]
    )


# ---------------------------------------------------------------------------
# TicketEscalation
# ---------------------------------------------------------------------------


class TicketEscalation(Base):
    """Records each manual escalation event on a ticket."""

    __tablename__ = "ticket_escalations"

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    ticket_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("tickets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    escalated_by: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    escalated_to: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    reason: Mapped[str] = mapped_column(sa.Text, nullable=False)
    escalated_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )

    ticket: Mapped["Ticket"] = relationship("Ticket", back_populates="escalations")
    escalated_by_user: Mapped["User"] = relationship(  # type: ignore[name-defined]
        "User", foreign_keys=[escalated_by]
    )
    escalated_to_user: Mapped[Optional["User"]] = relationship(  # type: ignore[name-defined]
        "User", foreign_keys=[escalated_to]
    )


# ---------------------------------------------------------------------------
# TicketComment
# ---------------------------------------------------------------------------


class TicketComment(Base, TimestampMixin):
    """Threaded comment on a ticket. Soft-deletable. is_internal = agent note."""

    __tablename__ = "ticket_comments"

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    ticket_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("tickets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    author_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    body: Mapped[str] = mapped_column(sa.Text, nullable=False)
    is_internal: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.text("false")
    )
    parent_comment_id: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("ticket_comments.id", ondelete="SET NULL"),
        nullable=True,
    )
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )

    ticket: Mapped["Ticket"] = relationship("Ticket", back_populates="comments")
    author: Mapped["User"] = relationship(  # type: ignore[name-defined]
        "User", foreign_keys=[author_id]
    )
    parent_comment: Mapped[Optional["TicketComment"]] = relationship(
        "TicketComment",
        back_populates="replies",
        remote_side="TicketComment.id",
        foreign_keys="TicketComment.parent_comment_id",
    )
    replies: Mapped[list["TicketComment"]] = relationship(
        "TicketComment",
        back_populates="parent_comment",
        foreign_keys="TicketComment.parent_comment_id",
    )
    attachments: Mapped[list["TicketAttachment"]] = relationship(
        "TicketAttachment", back_populates="comment"
    )


# ---------------------------------------------------------------------------
# TicketAttachment
# ---------------------------------------------------------------------------


class TicketAttachment(Base):
    """File attachment for a ticket or comment; stored in S3-compatible storage."""

    __tablename__ = "ticket_attachments"

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    ticket_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("tickets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    comment_id: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("ticket_comments.id", ondelete="SET NULL"),
        nullable=True,
    )
    uploaded_by: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    filename: Mapped[str] = mapped_column(sa.VARCHAR(512), nullable=False)
    storage_url: Mapped[str] = mapped_column(sa.VARCHAR(2048), nullable=False)
    file_size: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    mime_type: Mapped[str] = mapped_column(sa.VARCHAR(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )

    ticket: Mapped["Ticket"] = relationship("Ticket", back_populates="attachments")
    comment: Mapped[Optional["TicketComment"]] = relationship(
        "TicketComment", back_populates="attachments"
    )
    uploader: Mapped["User"] = relationship(  # type: ignore[name-defined]
        "User", foreign_keys=[uploaded_by]
    )


# ---------------------------------------------------------------------------
# TicketTag
# ---------------------------------------------------------------------------


class TicketTag(Base):
    """Tenant-scoped label that can be applied to tickets."""

    __tablename__ = "ticket_tags"
    __table_args__ = (
        sa.UniqueConstraint("tenant_id", "name", name="uq_ticket_tags_tenant_name"),
    )

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(sa.VARCHAR(100), nullable=False)
    color: Mapped[str] = mapped_column(sa.VARCHAR(20), nullable=False)

    tenant: Mapped["Tenant"] = relationship("Tenant")  # type: ignore[name-defined]
    tag_assignments: Mapped[list["TicketTagAssignment"]] = relationship(
        "TicketTagAssignment", back_populates="tag", cascade="all, delete-orphan"
    )


# ---------------------------------------------------------------------------
# TicketTagAssignment  (M:N, tickets ↔ ticket_tags)
# ---------------------------------------------------------------------------


class TicketTagAssignment(Base):
    """Associates a tag with a ticket."""

    __tablename__ = "ticket_tag_assignments"

    ticket_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("tickets.id", ondelete="CASCADE"),
        primary_key=True,
    )
    tag_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("ticket_tags.id", ondelete="CASCADE"),
        primary_key=True,
    )
    assigned_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )

    ticket: Mapped["Ticket"] = relationship("Ticket", back_populates="tag_assignments")
    tag: Mapped["TicketTag"] = relationship("TicketTag", back_populates="tag_assignments")


# ---------------------------------------------------------------------------
# TicketWatcher  (M:N, tickets ↔ users)
# ---------------------------------------------------------------------------


class TicketWatcher(Base):
    """User subscribed to notifications for a ticket."""

    __tablename__ = "ticket_watchers"

    ticket_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("tickets.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    added_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )

    ticket: Mapped["Ticket"] = relationship("Ticket", back_populates="watchers")
    user: Mapped["User"] = relationship("User")  # type: ignore[name-defined]


__all__ = [
    "TicketType",
    "TicketStatus",
    "TicketPriority",
    "BusinessHoursConfig",
    "SLAPolicy",
    "TicketCategory",
    "TicketStatusTransition",
    "Ticket",
    "TicketHistory",
    "TicketEscalation",
    "TicketComment",
    "TicketAttachment",
    "TicketTag",
    "TicketTagAssignment",
    "TicketWatcher",
]
