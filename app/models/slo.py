"""SLI / SLO / Error-Budget — the reliability layer (Phase 9).

Distinct from the ticket-SLA engine (Phase 7): telemetry-fed *service*
objectives with error budgets and burn-rate alerts (Google-SRE / Nobl9 style).
Attaches to Module B's ``oncall_services`` and reuses its alert/paging rails.

Enum-valued columns are VARCHAR validated by the Python enums below (matching
the SLM/on-call convention). Budget consumed/remaining, current SLI and burn
rate are **derived** from ``slo_measurements`` — never stored — so they stay
reproducible and always consistent.
"""

import enum
from datetime import datetime
from typing import Optional
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from uuid_extensions import uuid7

from app.models.base import Base, TimestampMixin


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class SLISourceType(str, enum.Enum):
    prometheus = "prometheus"
    datadog = "datadog"
    http_uptime_check = "http_uptime_check"
    log_query = "log_query"
    internal_metric = "internal_metric"   # computed from data we already own
    push_api = "push_api"                 # external systems POST good/total


class SLOObjectiveType(str, enum.Enum):
    availability = "availability"
    latency = "latency"
    error_rate = "error_rate"
    freshness = "freshness"
    custom = "custom"


class SLOWindow(str, enum.Enum):
    rolling_7d = "rolling_7d"
    rolling_28d = "rolling_28d"
    rolling_30d = "rolling_30d"
    calendar_month = "calendar_month"
    quarter = "quarter"


# window → number of days (calendar_month/quarter approximated for rolling math)
WINDOW_DAYS: dict[str, int] = {
    "rolling_7d": 7,
    "rolling_28d": 28,
    "rolling_30d": 30,
    "calendar_month": 30,
    "quarter": 90,
}


class BurnAlertKind(str, enum.Enum):
    fast_burn = "fast_burn"
    slow_burn = "slow_burn"


class BurnAlertState(str, enum.Enum):
    ok = "ok"
    firing = "firing"


# Google-SRE multi-window defaults: (short_window_min, long_window_min, burn_threshold)
BURN_DEFAULTS: dict[str, dict] = {
    "fast_burn": {"short_window_min": 5,  "long_window_min": 60,   "burn_threshold": 14.4},
    "slow_burn": {"short_window_min": 30, "long_window_min": 360,  "burn_threshold": 6.0},
}


# ---------------------------------------------------------------------------
# SLI source — where a measurement comes from
# ---------------------------------------------------------------------------


class SLISource(Base, TimestampMixin):
    __tablename__ = "sli_sources"

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    name: Mapped[str] = mapped_column(sa.VARCHAR(255), nullable=False)
    type: Mapped[str] = mapped_column(sa.VARCHAR(30), nullable=False)
    # For query-based sources (prometheus/datadog/log_query): the good/valid queries.
    good_query: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    valid_query: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    # For internal_metric: {"metric": "ticket_sla_compliance"|"incident_uptime"|...}
    # For http_uptime_check: {"url": "..."}. Connection/auth ref (secret via env).
    config: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    connection: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.text("true"))


# ---------------------------------------------------------------------------
# SLO objective — the target on a service
# ---------------------------------------------------------------------------


class SLOObjective(Base, TimestampMixin):
    __tablename__ = "slo_objectives"

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    service_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True), sa.ForeignKey("oncall_services.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    sli_source_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True), sa.ForeignKey("sli_sources.id", ondelete="RESTRICT"), nullable=False,
    )
    name: Mapped[str] = mapped_column(sa.VARCHAR(255), nullable=False)
    objective_type: Mapped[str] = mapped_column(sa.VARCHAR(20), nullable=False, server_default=sa.text("'availability'"))
    target_pct: Mapped[float] = mapped_column(sa.Numeric(6, 3), nullable=False)   # e.g. 99.900
    window: Mapped[str] = mapped_column(sa.VARCHAR(20), nullable=False, server_default=sa.text("'rolling_28d'"))
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.text("true"))

    burn_alerts: Mapped[list["SLOBurnAlert"]] = relationship(
        "SLOBurnAlert", back_populates="slo", cascade="all, delete-orphan",
    )


# ---------------------------------------------------------------------------
# SLO measurement — immutable, time-bucketed raw material
# ---------------------------------------------------------------------------


class SLOMeasurement(Base):
    __tablename__ = "slo_measurements"
    __table_args__ = (
        sa.UniqueConstraint("slo_id", "bucket_start", name="uq_slo_measurements_bucket"),
        sa.Index("ix_slo_measurements_slo_bucket", "slo_id", sa.text("bucket_start DESC")),
    )

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    slo_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True), sa.ForeignKey("slo_objectives.id", ondelete="CASCADE"), nullable=False,
    )
    bucket_start: Mapped[datetime] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=False)
    good_count: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, server_default=sa.text("0"))
    total_count: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, server_default=sa.text("0"))
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now(),
    )


# ---------------------------------------------------------------------------
# SLO burn alert — multi-window burn-rate rule (ties into Module B)
# ---------------------------------------------------------------------------


class SLOBurnAlert(Base, TimestampMixin):
    __tablename__ = "slo_burn_alerts"

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    slo_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True), sa.ForeignKey("slo_objectives.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    kind: Mapped[str] = mapped_column(sa.VARCHAR(12), nullable=False)
    short_window_min: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("5"))
    long_window_min: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("60"))
    burn_threshold: Mapped[float] = mapped_column(sa.Numeric(8, 3), nullable=False, server_default=sa.text("14.4"))
    state: Mapped[str] = mapped_column(sa.VARCHAR(10), nullable=False, server_default=sa.text("'ok'"))
    last_fired_at: Mapped[Optional[datetime]] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=True)
    linked_alert_id: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True), sa.ForeignKey("alerts.id", ondelete="SET NULL"), nullable=True,
    )

    slo: Mapped["SLOObjective"] = relationship("SLOObjective", back_populates="burn_alerts")


__all__ = [
    "SLISource", "SLISourceType",
    "SLOObjective", "SLOObjectiveType", "SLOWindow", "WINDOW_DAYS",
    "SLOMeasurement",
    "SLOBurnAlert", "BurnAlertKind", "BurnAlertState", "BURN_DEFAULTS",
]
