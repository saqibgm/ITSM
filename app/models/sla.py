"""Service Level Management (SLM) models — Phase 7 / S7.1.

Promotes the flat ``sla_policies`` (see ``app.models.ticket.SLAPolicy``, kept for
back-compat and re-exported below) into a first-class SLA/OLA/UC model:

- ``SLAAgreement``  — a named agreement, kind = sla | ola | uc, versioned.
- ``SLATarget``     — one measurable clock within an agreement (per metric/scope).
- ``SLAUnderpinning`` — links a customer SLA target to supporting OLA/UC targets.
- ``SLARule``       — priority-ordered matcher (first match wins) → agreement.
- ``CoverageWindow`` — named service-hours calendar (generalises business hours).

Enum-valued columns are stored as VARCHAR (matching ``ticket_status_transitions``)
with the Python enums below used for validation in the service/schema layer —
avoids native-PG-enum migration friction.
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
# Re-export so `from app.models.sla import SLAPolicy` (webhook provisioning) resolves.
from app.models.ticket import SLAPolicy  # noqa: F401


# ---------------------------------------------------------------------------
# Enums (validation-layer; columns are VARCHAR)
# ---------------------------------------------------------------------------


class SLAAgreementKind(str, enum.Enum):
    sla = "sla"   # provider → customer
    ola = "ola"   # team → team (internal, underpins an SLA)
    uc = "uc"     # vendor → provider (underpins an SLA)


class SLAMetric(str, enum.Enum):
    first_response = "first_response"
    next_response = "next_response"
    periodic_update = "periodic_update"
    time_to_acknowledge = "time_to_acknowledge"
    time_to_mitigate = "time_to_mitigate"
    resolution = "resolution"


# Default (start_event, stop_event) per metric — used when a target does not
# override them. The §10 parity tweak lets a target set these explicitly.
METRIC_DEFAULT_EVENTS: dict[str, tuple[str, str]] = {
    "first_response":      ("ticket_created", "first_public_agent_reply"),
    "next_response":       ("customer_reply", "next_public_agent_reply"),
    "periodic_update":     ("ticket_created", "periodic_update_posted"),
    "time_to_acknowledge": ("ticket_created", "acknowledged"),
    "time_to_mitigate":    ("incident_declared", "mitigated"),
    "resolution":          ("ticket_created", "resolved"),
}


# ---------------------------------------------------------------------------
# CoverageWindow — named service-hours calendar
# ---------------------------------------------------------------------------


class CoverageWindow(Base, TimestampMixin):
    """A named service-hours calendar; many per tenant (24x7, 9-5, follow-the-sun)."""

    __tablename__ = "coverage_windows"

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(sa.VARCHAR(255), nullable=False)
    timezone: Mapped[str] = mapped_column(sa.VARCHAR(64), nullable=False, server_default=sa.text("'UTC'"))
    is_247: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.text("false"))
    # ISO weekday numbers 1=Mon..7=Sun
    work_days: Mapped[list[int]] = mapped_column(ARRAY(sa.Integer), nullable=False, server_default=sa.text("'{1,2,3,4,5}'"))
    # [{"start": "09:00", "end": "17:00"}, ...] — supports split shifts
    windows: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    holidays: Mapped[Optional[list]] = mapped_column(ARRAY(sa.Date), nullable=True)
    # Union of other coverage_windows (follow-the-sun composite)
    compose_of: Mapped[Optional[list[UUID]]] = mapped_column(ARRAY(sa.UUID(as_uuid=True)), nullable=True)


# ---------------------------------------------------------------------------
# SLAAgreement — the parent (sla | ola | uc)
# ---------------------------------------------------------------------------


class SLAAgreement(Base, TimestampMixin):
    """A named, versioned agreement. kind sla/ola/uc; ola carries owner_team,
    uc carries vendor. Editing an active agreement bumps ``version`` (in-flight
    tickets keep the version frozen on their instance — no retroactive breach)."""

    __tablename__ = "sla_agreements"

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(sa.VARCHAR(255), nullable=False)
    kind: Mapped[str] = mapped_column(sa.VARCHAR(10), nullable=False, server_default=sa.text("'sla'"))
    description: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    owner_team_id: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True), sa.ForeignKey("teams.id", ondelete="SET NULL"), nullable=True,
    )
    vendor_id: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True), sa.ForeignKey("vendors.id", ondelete="SET NULL"), nullable=True,
    )
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("1"))
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.text("true"))
    deleted_at: Mapped[Optional[datetime]] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=True)

    targets: Mapped[list["SLATarget"]] = relationship(
        "SLATarget", back_populates="agreement", cascade="all, delete-orphan",
    )


# ---------------------------------------------------------------------------
# SLATarget — one measurable clock within an agreement
# ---------------------------------------------------------------------------


class SLATarget(Base, TimestampMixin):
    """A single metric clock scoped by priority/type/category, on a coverage window."""

    __tablename__ = "sla_targets"

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    agreement_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True), sa.ForeignKey("sla_agreements.id", ondelete="CASCADE"), nullable=False,
    )
    metric: Mapped[str] = mapped_column(sa.VARCHAR(30), nullable=False)
    duration_minutes: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    coverage_window_id: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True), sa.ForeignKey("coverage_windows.id", ondelete="SET NULL"), nullable=True,
    )
    # Scope filters — NULL = applies to any
    applies_priority: Mapped[Optional[str]] = mapped_column(sa.VARCHAR(20), nullable=True)
    applies_type: Mapped[Optional[str]] = mapped_column(sa.VARCHAR(30), nullable=True)
    applies_category_id: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True), sa.ForeignKey("ticket_categories.id", ondelete="SET NULL"), nullable=True,
    )
    # §10 parity tweak: configurable start/stop events (NULL → METRIC_DEFAULT_EVENTS)
    start_event: Mapped[Optional[str]] = mapped_column(sa.VARCHAR(40), nullable=True)
    stop_event: Mapped[Optional[str]] = mapped_column(sa.VARCHAR(40), nullable=True)
    # Multi-condition pause, e.g. {waiting_on_customer, waiting_on_vendor}
    pause_conditions: Mapped[Optional[list[str]]] = mapped_column(ARRAY(sa.VARCHAR(40)), nullable=True)
    warn_thresholds_pct: Mapped[list[int]] = mapped_column(
        ARRAY(sa.Integer), nullable=False, server_default=sa.text("'{50,75,90}'"),
    )

    agreement: Mapped["SLAAgreement"] = relationship("SLAAgreement", back_populates="targets")


# ---------------------------------------------------------------------------
# SLAUnderpinning — SLA target ← supported by → OLA/UC target
# ---------------------------------------------------------------------------


class SLAUnderpinning(Base):
    """Links a customer SLA target to a supporting OLA/UC target (breach attribution)."""

    __tablename__ = "sla_underpinning"

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    parent_target_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True), sa.ForeignKey("sla_targets.id", ondelete="CASCADE"), nullable=False,
    )
    support_target_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True), sa.ForeignKey("sla_targets.id", ondelete="CASCADE"), nullable=False,
    )

    __table_args__ = (
        sa.UniqueConstraint("parent_target_id", "support_target_id", name="uq_sla_underpinning_pair"),
    )


# ---------------------------------------------------------------------------
# SLARule — priority-ordered matcher (first match wins)
# ---------------------------------------------------------------------------


class SLARule(Base, TimestampMixin):
    """Ordered rule that maps a ticket's attributes to an agreement (first match wins)."""

    __tablename__ = "sla_rules"

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    position: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    # {type, priority, category_id, product_id, tag, vip, asset_criticality}
    conditions: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=sa.text("'{}'::jsonb"))
    agreement_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True), sa.ForeignKey("sla_agreements.id", ondelete="CASCADE"), nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.text("true"))

    __table_args__ = (
        sa.Index("ix_sla_rules_tenant_position", "tenant_id", "position"),
    )
