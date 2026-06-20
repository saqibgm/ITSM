"""
SQLAlchemy ORM models for the Automation Rules Engine (S6A).

AutomationRule  — tenant-scoped rule with trigger, conditions, actions.
AutomationLog   — immutable execution record (one row per rule evaluation).

All PKs are UUID v7.  Conditions and actions are stored as JSONB arrays.
"""

from datetime import datetime
from typing import Optional
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from uuid_extensions import uuid7

from app.models.base import Base, TimestampMixin


# ---------------------------------------------------------------------------
# AutomationRule
# ---------------------------------------------------------------------------


class AutomationRule(Base, TimestampMixin):
    """A tenant-scoped automation rule.

    Triggers:
      ticket_created | ticket_updated | ticket_sla_breached |
      asset_status_changed | ticket_pending_approval

    Conditions (JSONB array):
      {"field": "priority", "op": "eq|ne|in|gt|lt|contains", "value": ...}

    Actions (JSONB array):
      {"type": "assign_team",         "team_id": "..."}
      {"type": "assign_user",         "user_id": "..."}
      {"type": "set_priority",        "priority": "high"}
      {"type": "add_tag",             "tag": "urgent"}
      {"type": "send_notification",   "message": "...", "target": "assigned_user|reporter|team"}
      {"type": "auto_close",          "resolution_note": "..."}
      {"type": "escalate"}
    """

    __tablename__ = "automation_rules"
    __table_args__ = (
        sa.Index(
            "idx_automation_rules_tenant_trigger",
            "tenant_id",
            "trigger",
            "is_active",
        ),
    )

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(sa.VARCHAR(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    trigger: Mapped[str] = mapped_column(sa.VARCHAR(50), nullable=False)
    conditions: Mapped[list] = mapped_column(
        JSONB,
        nullable=False,
        server_default=sa.text("'[]'::jsonb"),
    )
    actions: Mapped[list] = mapped_column(
        JSONB,
        nullable=False,
        server_default=sa.text("'[]'::jsonb"),
    )
    is_active: Mapped[bool] = mapped_column(
        sa.Boolean,
        nullable=False,
        server_default=sa.text("true"),
    )
    run_order: Mapped[int] = mapped_column(
        sa.Integer,
        nullable=False,
        server_default=sa.text("0"),
    )
    last_triggered_at: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        nullable=True,
    )
    trigger_count: Mapped[int] = mapped_column(
        sa.Integer,
        nullable=False,
        server_default=sa.text("0"),
    )

    # Relationships
    tenant: Mapped["Tenant"] = relationship("Tenant")  # type: ignore[name-defined]
    logs: Mapped[list["AutomationLog"]] = relationship(
        "AutomationLog",
        back_populates="rule",
        cascade="all, delete-orphan",
    )


# ---------------------------------------------------------------------------
# AutomationLog
# ---------------------------------------------------------------------------


class AutomationLog(Base):
    """Immutable execution record for one rule evaluation.

    Written whether the actions succeeded or failed.  ``error`` is non-NULL
    when at least one action raised an exception.
    """

    __tablename__ = "automation_logs"
    __table_args__ = (
        sa.Index(
            "idx_automation_logs_rule_created",
            "rule_id",
            "created_at",
        ),
        sa.Index(
            "idx_automation_logs_tenant_entity",
            "tenant_id",
            "entity_type",
            "entity_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    rule_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("automation_rules.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tenant_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        nullable=False,
        index=True,
    )
    entity_type: Mapped[str] = mapped_column(sa.VARCHAR(50), nullable=False)
    entity_id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), nullable=False)
    actions_executed: Mapped[list] = mapped_column(JSONB, nullable=False)
    error: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )

    rule: Mapped["AutomationRule"] = relationship("AutomationRule", back_populates="logs")


__all__ = [
    "AutomationRule",
    "AutomationLog",
]
