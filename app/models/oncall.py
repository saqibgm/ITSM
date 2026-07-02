"""On-Call & Incident — schedules and services (Phase 8 / S8.1).

Greenfield SRE module. This file covers the S8.1 primitives: services, severity
levels, on-call schedules (layers + overrides). Alerts/escalation/incidents land
in later slices. Enum-valued columns are VARCHAR (validated via the Python enums
below), matching the SLM convention.
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


class ServiceState(str, enum.Enum):
    operational = "operational"
    degraded = "degraded"
    partial_outage = "partial_outage"
    major_outage = "major_outage"
    maintenance = "maintenance"


class RotationType(str, enum.Enum):
    daily = "daily"
    weekly = "weekly"
    custom = "custom"
    follow_the_sun = "follow_the_sun"


class OverrideOrigin(str, enum.Enum):
    manual = "manual"
    swap = "swap"


# Default rotation length (hours) per rotation type when not set explicitly.
ROTATION_DEFAULT_HOURS = {"daily": 24, "weekly": 168, "follow_the_sun": 24}


class OnCallService(Base, TimestampMixin):
    """A monitored service; links to a CMDB asset for blast-radius (S8.3)."""

    __tablename__ = "oncall_services"

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    name: Mapped[str] = mapped_column(sa.VARCHAR(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    asset_id: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True), sa.ForeignKey("assets.id", ondelete="SET NULL"), nullable=True,
    )
    # FK constraint added in S8.2 when escalation_policies exists.
    escalation_policy_id: Mapped[Optional[UUID]] = mapped_column(sa.UUID(as_uuid=True), nullable=True)
    current_state: Mapped[str] = mapped_column(sa.VARCHAR(20), nullable=False, server_default=sa.text("'operational'"))
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.text("true"))


class SeverityLevel(Base, TimestampMixin):
    """Incident severity (SEV1..SEVn); drives auto-page + required roles (S8.3)."""

    __tablename__ = "severity_levels"

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    name: Mapped[str] = mapped_column(sa.VARCHAR(40), nullable=False)
    rank: Mapped[int] = mapped_column(sa.Integer, nullable=False)  # 1 = most severe
    auto_page: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.text("false"))
    required_roles: Mapped[Optional[list[str]]] = mapped_column(ARRAY(sa.VARCHAR(20)), nullable=True)
    default_agreement_id: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True), sa.ForeignKey("sla_agreements.id", ondelete="SET NULL"), nullable=True,
    )
    impact_urgency_map: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)


class Schedule(Base, TimestampMixin):
    """An on-call schedule for a team. Layers rotate their ordered participants;
    overrides take precedence. ``start_at`` (or created_at) anchors the rotation."""

    __tablename__ = "schedules"

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    name: Mapped[str] = mapped_column(sa.VARCHAR(255), nullable=False)
    team_id: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True), sa.ForeignKey("teams.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    timezone: Mapped[str] = mapped_column(sa.VARCHAR(64), nullable=False, server_default=sa.text("'UTC'"))
    rotation_type: Mapped[str] = mapped_column(sa.VARCHAR(20), nullable=False, server_default=sa.text("'weekly'"))
    rotation_length_hours: Mapped[Optional[int]] = mapped_column(sa.Integer, nullable=True)
    handoff_time: Mapped[Optional[time]] = mapped_column(sa.Time, nullable=True)
    start_at: Mapped[Optional[datetime]] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.text("true"))

    layers: Mapped[list["ScheduleLayer"]] = relationship(
        "ScheduleLayer", back_populates="schedule", cascade="all, delete-orphan",
    )


class ScheduleLayer(Base, TimestampMixin):
    """A rotation layer (rank 1 = primary, 2 = secondary…) with ordered participants."""

    __tablename__ = "schedule_layers"

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    schedule_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True), sa.ForeignKey("schedules.id", ondelete="CASCADE"), nullable=False,
    )
    layer_rank: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    participants: Mapped[list[UUID]] = mapped_column(ARRAY(sa.UUID(as_uuid=True)), nullable=False, server_default=sa.text("'{}'"))

    schedule: Mapped["Schedule"] = relationship("Schedule", back_populates="layers")


class ScheduleOverride(Base):
    """A time-boxed override of the primary on-call (manual or a swap)."""

    __tablename__ = "schedule_overrides"

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    schedule_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True), sa.ForeignKey("schedules.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    user_id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    start_at: Mapped[datetime] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=False)
    origin: Mapped[str] = mapped_column(sa.VARCHAR(10), nullable=False, server_default=sa.text("'manual'"))
    created_by: Mapped[Optional[UUID]] = mapped_column(sa.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now())
