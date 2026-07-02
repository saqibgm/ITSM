"""Incident management (Phase 8 / S8.3).

SRE incidents — distinct from incident-*type* tickets. Own IR-YYYY-NNNNN number.
Lifecycle is a seeded state machine (incident_status_transitions, like tickets);
roles, an immutable timeline, and stakeholder status updates hang off it. Links
to the alert/ticket that spawned it and to affected services (blast-radius).
"""

import enum
from datetime import datetime
from typing import Optional
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from uuid_extensions import uuid7

from app.models.base import Base, TimestampMixin


class IncidentStatus(str, enum.Enum):
    declared = "declared"
    investigating = "investigating"
    identified = "identified"
    monitoring = "monitoring"
    resolved = "resolved"
    postmortem = "postmortem"
    closed = "closed"
    cancelled = "cancelled"


class IncidentRoleType(str, enum.Enum):
    ic = "ic"        # incident commander
    comms = "comms"
    ops = "ops"
    scribe = "scribe"


class Audience(str, enum.Enum):
    internal = "internal"
    stakeholder = "stakeholder"
    public = "public"


class Incident(Base, TimestampMixin):
    __tablename__ = "incidents"

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    incident_number: Mapped[str] = mapped_column(sa.VARCHAR(30), nullable=False)
    title: Mapped[str] = mapped_column(sa.VARCHAR(500), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    severity_id: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True), sa.ForeignKey("severity_levels.id", ondelete="SET NULL"), nullable=True,
    )
    status: Mapped[str] = mapped_column(sa.VARCHAR(20), nullable=False, server_default=sa.text("'declared'"))
    declared_by: Mapped[Optional[UUID]] = mapped_column(sa.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    declared_at: Mapped[datetime] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now())
    mitigated_at: Mapped[Optional[datetime]] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=True)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=True)
    affected_service_ids: Mapped[Optional[list[UUID]]] = mapped_column(ARRAY(sa.UUID(as_uuid=True)), nullable=True)
    source_alert_id: Mapped[Optional[UUID]] = mapped_column(sa.UUID(as_uuid=True), sa.ForeignKey("alerts.id", ondelete="SET NULL"), nullable=True)
    source_ticket_id: Mapped[Optional[UUID]] = mapped_column(sa.UUID(as_uuid=True), sa.ForeignKey("tickets.id", ondelete="SET NULL"), nullable=True)
    chat_channel: Mapped[Optional[str]] = mapped_column(sa.VARCHAR(120), nullable=True)

    __table_args__ = (
        sa.UniqueConstraint("tenant_id", "incident_number", name="uq_incident_number"),
        sa.Index("ix_incidents_tenant_status", "tenant_id", "status", "declared_at"),
    )


class IncidentStatusTransition(Base):
    """Seeded, immutable — valid (from_status, to_status) pairs."""

    __tablename__ = "incident_status_transitions"

    from_status: Mapped[str] = mapped_column(sa.VARCHAR(20), primary_key=True)
    to_status: Mapped[str] = mapped_column(sa.VARCHAR(20), primary_key=True)


class IncidentRole(Base):
    __tablename__ = "incident_roles"

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    incident_id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), sa.ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(sa.VARCHAR(10), nullable=False)
    user_id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    assigned_at: Mapped[datetime] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now())

    __table_args__ = (sa.UniqueConstraint("incident_id", "role", name="uq_incident_role"),)


class IncidentTimeline(Base):
    """Immutable (INSERT-only) auto-generated timeline."""

    __tablename__ = "incident_timeline"

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    incident_id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), sa.ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False, index=True)
    actor_id: Mapped[Optional[UUID]] = mapped_column(sa.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    event_type: Mapped[str] = mapped_column(sa.VARCHAR(40), nullable=False)
    data: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    pinned: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.text("false"))
    at: Mapped[datetime] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now())

    __table_args__ = (sa.Index("ix_incident_timeline_incident_at", "incident_id", "at"),)


class IncidentStatusUpdate(Base):
    __tablename__ = "incident_status_updates"

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    incident_id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), sa.ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False, index=True)
    author_id: Mapped[Optional[UUID]] = mapped_column(sa.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    body: Mapped[str] = mapped_column(sa.Text, nullable=False)
    audience: Mapped[str] = mapped_column(sa.VARCHAR(15), nullable=False, server_default=sa.text("'internal'"))
    channels: Mapped[Optional[list[str]]] = mapped_column(ARRAY(sa.VARCHAR(20)), nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now())
