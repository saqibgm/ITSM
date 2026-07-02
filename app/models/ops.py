"""Status page, maintenance windows & workflow automation (Phase 8 / S8.4)."""

import enum
from datetime import datetime
from typing import Optional
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column
from uuid_extensions import uuid7

from app.models.base import Base, TimestampMixin


class StatusPageChannel(str, enum.Enum):
    email = "email"
    sms = "sms"


class WorkflowRunStatus(str, enum.Enum):
    success = "success"
    partial = "partial"
    failed = "failed"
    dry_run = "dry_run"


class StatusPageConfig(Base, TimestampMixin):
    __tablename__ = "status_page_config"

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, unique=True,
    )
    slug: Mapped[str] = mapped_column(sa.VARCHAR(80), nullable=False, unique=True)
    custom_domain: Mapped[Optional[str]] = mapped_column(sa.VARCHAR(255), nullable=True)
    is_public: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.text("false"))
    branding: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)


class StatusPageSubscription(Base):
    __tablename__ = "status_page_subscriptions"

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    channel: Mapped[str] = mapped_column(sa.VARCHAR(10), nullable=False)
    value: Mapped[str] = mapped_column(sa.VARCHAR(255), nullable=False)
    service_ids: Mapped[Optional[list[UUID]]] = mapped_column(ARRAY(sa.UUID(as_uuid=True)), nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now())


class MaintenanceWindow(Base, TimestampMixin):
    __tablename__ = "maintenance_windows"

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    title: Mapped[str] = mapped_column(sa.VARCHAR(255), nullable=False)
    service_ids: Mapped[Optional[list[UUID]]] = mapped_column(ARRAY(sa.UUID(as_uuid=True)), nullable=True)
    start_at: Mapped[datetime] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=False)
    suppress_alerts: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.text("true"))
    created_by: Mapped[Optional[UUID]] = mapped_column(sa.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    __table_args__ = (sa.Index("ix_maintenance_windows_tenant_window", "tenant_id", "start_at", "end_at"),)


class Workflow(Base, TimestampMixin):
    """Declarative automation: trigger + conditions → ordered actions."""

    __tablename__ = "workflows"

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    name: Mapped[str] = mapped_column(sa.VARCHAR(255), nullable=False)
    trigger: Mapped[str] = mapped_column(sa.VARCHAR(40), nullable=False)  # alert_created, incident_declared, severity_changed
    conditions: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=sa.text("'{}'::jsonb"))
    actions: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=sa.text("'[]'::jsonb"))
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.text("true"))
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("1"))

    __table_args__ = (sa.Index("ix_workflows_tenant_trigger", "tenant_id", "trigger", "is_active"),)


class WorkflowRun(Base):
    """Immutable execution log."""

    __tablename__ = "workflow_runs"

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    workflow_id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), sa.ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False, index=True)
    incident_id: Mapped[Optional[UUID]] = mapped_column(sa.UUID(as_uuid=True), nullable=True)
    alert_id: Mapped[Optional[UUID]] = mapped_column(sa.UUID(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(sa.VARCHAR(10), nullable=False)
    result: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    at: Mapped[datetime] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now())
