"""
SQLAlchemy ORM model for AI usage tracking.

ai_usage_daily aggregates per-tenant daily token consumption by feature and model.
Used by the Celery beat flush_daily_usage task (nightly) and the tenant AI budget API.
"""

from datetime import date, datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship
from uuid_extensions import uuid7

from app.models.base import Base


class AIUsageDaily(Base):
    """Per-tenant daily AI token usage, aggregated from Redis counters nightly.

    UNIQUE (tenant_id, date, ai_feature, model) — upserted by the Celery beat task.
    """

    __tablename__ = "ai_usage_daily"
    __table_args__ = (
        sa.UniqueConstraint(
            "tenant_id",
            "date",
            "ai_feature",
            "model",
            name="uq_ai_usage_daily_tenant_date_feature_model",
        ),
        sa.Index("ix_ai_usage_daily_tenant_date", "tenant_id", sa.text("date DESC")),
    )

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    date: Mapped[date] = mapped_column(sa.Date, nullable=False)
    ai_feature: Mapped[str] = mapped_column(sa.VARCHAR(50), nullable=False)
    model: Mapped[str] = mapped_column(sa.VARCHAR(100), nullable=False)
    input_tokens: Mapped[int] = mapped_column(
        sa.BigInteger, nullable=False, server_default=sa.text("0")
    )
    output_tokens: Mapped[int] = mapped_column(
        sa.BigInteger, nullable=False, server_default=sa.text("0")
    )
    call_count: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default=sa.text("0")
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
    )

    tenant: Mapped["Tenant"] = relationship("Tenant")  # type: ignore[name-defined]


__all__ = ["AIUsageDaily"]
