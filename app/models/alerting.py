"""Alerting, escalation & paging (Phase 8 / S8.2).

Alerts (deduplicated) → routing → escalation policy → pages to the on-call
responder(s), advancing through timed steps until acknowledged. Per-user contact
methods + notification rules abstract the delivery channel (provider adapters are
pluggable later). Enum columns are VARCHAR validated by the Python enums.
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


class TargetType(str, enum.Enum):
    schedule = "schedule"
    user = "user"
    team = "team"


class NotifyStrategy(str, enum.Enum):
    current_oncall = "current_oncall"
    round_robin = "round_robin"
    notify_all = "notify_all"


class ContactType(str, enum.Enum):
    push = "push"
    sms = "sms"
    voice = "voice"
    email = "email"
    slack = "slack"
    teams = "teams"


class Urgency(str, enum.Enum):
    high = "high"
    low = "low"


class AlertStatus(str, enum.Enum):
    open = "open"
    acknowledged = "acknowledged"
    resolved = "resolved"


class PageStatus(str, enum.Enum):
    queued = "queued"
    sent = "sent"
    delivered = "delivered"
    failed = "failed"
    acknowledged = "acknowledged"


class EscalationPolicy(Base, TimestampMixin):
    __tablename__ = "escalation_policies"

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    name: Mapped[str] = mapped_column(sa.VARCHAR(255), nullable=False)
    repeat_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("0"))

    steps: Mapped[list["EscalationStep"]] = relationship(
        "EscalationStep", back_populates="policy", cascade="all, delete-orphan",
    )


class EscalationStep(Base):
    __tablename__ = "escalation_steps"

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    policy_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True), sa.ForeignKey("escalation_policies.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    position: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    target_type: Mapped[str] = mapped_column(sa.VARCHAR(10), nullable=False)
    target_id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), nullable=False)
    timeout_minutes: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("15"))
    notify_strategy: Mapped[str] = mapped_column(sa.VARCHAR(20), nullable=False, server_default=sa.text("'current_oncall'"))

    policy: Mapped["EscalationPolicy"] = relationship("EscalationPolicy", back_populates="steps")


class ContactMethod(Base):
    __tablename__ = "contact_methods"

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    user_id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    type: Mapped[str] = mapped_column(sa.VARCHAR(10), nullable=False)
    value: Mapped[str] = mapped_column(sa.VARCHAR(255), nullable=False)
    is_verified: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.text("false"))


class NotificationRule(Base):
    __tablename__ = "notification_rules"

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    user_id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    urgency: Mapped[str] = mapped_column(sa.VARCHAR(10), nullable=False, server_default=sa.text("'high'"))
    position: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("0"))
    delay_minutes: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("0"))
    contact_method_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True), sa.ForeignKey("contact_methods.id", ondelete="CASCADE"), nullable=False,
    )


class Alert(Base, TimestampMixin):
    __tablename__ = "alerts"

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    service_id: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True), sa.ForeignKey("oncall_services.id", ondelete="SET NULL"), nullable=True,
    )
    source: Mapped[str] = mapped_column(sa.VARCHAR(60), nullable=False, server_default=sa.text("'manual'"))
    dedup_key: Mapped[str] = mapped_column(sa.VARCHAR(255), nullable=False)
    title: Mapped[str] = mapped_column(sa.VARCHAR(500), nullable=False)
    payload: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    severity_id: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True), sa.ForeignKey("severity_levels.id", ondelete="SET NULL"), nullable=True,
    )
    status: Mapped[str] = mapped_column(sa.VARCHAR(15), nullable=False, server_default=sa.text("'open'"))
    occurrence_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("1"))
    incident_id: Mapped[Optional[UUID]] = mapped_column(sa.UUID(as_uuid=True), nullable=True)  # FK in S8.3
    # Escalation runtime bookkeeping (drives the timed advance).
    escalation_policy_id: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True), sa.ForeignKey("escalation_policies.id", ondelete="SET NULL"), nullable=True,
    )
    escalation_step_index: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("0"))
    next_escalation_at: Mapped[Optional[datetime]] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=True)
    acknowledged_at: Mapped[Optional[datetime]] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=True)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=True)

    __table_args__ = (
        # De-dup guard: at most one OPEN alert per (tenant, dedup_key).
        sa.Index("uq_alert_open_dedup", "tenant_id", "dedup_key", unique=True,
                 postgresql_where=sa.text("status = 'open'")),
        sa.Index("ix_alerts_tenant_status", "tenant_id", "status", "created_at"),
        sa.Index("ix_alerts_next_escalation", "status", "next_escalation_at"),
    )


class Page(Base):
    """A single notification attempt to a responder (immutable log)."""

    __tablename__ = "pages"

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    alert_id: Mapped[Optional[UUID]] = mapped_column(sa.UUID(as_uuid=True), sa.ForeignKey("alerts.id", ondelete="CASCADE"), nullable=True)
    incident_id: Mapped[Optional[UUID]] = mapped_column(sa.UUID(as_uuid=True), nullable=True)
    user_id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    contact_method_id: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True), sa.ForeignKey("contact_methods.id", ondelete="SET NULL"), nullable=True,
    )
    status: Mapped[str] = mapped_column(sa.VARCHAR(15), nullable=False, server_default=sa.text("'queued'"))
    escalation_step_id: Mapped[Optional[UUID]] = mapped_column(sa.UUID(as_uuid=True), nullable=True)
    sent_at: Mapped[datetime] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now())
    acknowledged_at: Mapped[Optional[datetime]] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=True)

    __table_args__ = (sa.Index("ix_pages_alert", "alert_id"),)


class AlertRoutingRule(Base):
    __tablename__ = "alert_routing_rules"

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    position: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    conditions: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=sa.text("'{}'::jsonb"))
    service_id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), sa.ForeignKey("oncall_services.id", ondelete="CASCADE"), nullable=False)
    severity_id: Mapped[Optional[UUID]] = mapped_column(sa.UUID(as_uuid=True), sa.ForeignKey("severity_levels.id", ondelete="SET NULL"), nullable=True)


class Heartbeat(Base):
    __tablename__ = "heartbeats"

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    service_id: Mapped[Optional[UUID]] = mapped_column(sa.UUID(as_uuid=True), sa.ForeignKey("oncall_services.id", ondelete="SET NULL"), nullable=True)
    name: Mapped[str] = mapped_column(sa.VARCHAR(255), nullable=False)
    interval_sec: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    last_ping_at: Mapped[Optional[datetime]] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=True)
    ping_token: Mapped[str] = mapped_column(sa.VARCHAR(64), nullable=False, unique=True)
