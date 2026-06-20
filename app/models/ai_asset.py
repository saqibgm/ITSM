"""
SQLAlchemy ORM models for AI-powered predictive asset maintenance.

AIPredictiveMaintenance — one record per asset (upserted); stores Claude's
  risk score, predicted maintenance date, and the acknowledging user.
"""

from datetime import date, datetime
from typing import Optional
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship
from uuid_extensions import uuid7

from app.models.base import Base


class AIPredictiveMaintenance(Base):
    """Stores Claude's predictive maintenance assessment for an asset.

    One record per asset — new predictions replace (upsert) the previous one
    so the table never accumulates stale rows.

    risk_score    — float [0.0, 1.0]; scores > 0.7 trigger an in-app alert
    acknowledged  — set True by an agent/admin after reviewing the prediction
    """

    __tablename__ = "ai_maintenance_predictions"
    __table_args__ = (
        sa.Index(
            "ix_ai_maint_pred_asset_risk",
            "asset_id",
            sa.text("risk_score DESC"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True), primary_key=True, default=uuid7
    )
    asset_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("assets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    predicted_due_date: Mapped[date] = mapped_column(sa.Date, nullable=False)
    risk_score: Mapped[float] = mapped_column(sa.Float, nullable=False)
    reason: Mapped[str] = mapped_column(sa.Text, nullable=False)
    model_version: Mapped[str] = mapped_column(sa.VARCHAR(100), nullable=False)
    acknowledged: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.text("false")
    )
    acknowledged_by: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )

    # Relationships
    asset: Mapped["Asset"] = relationship(  # type: ignore[name-defined]
        "Asset", foreign_keys=[asset_id]
    )
    acknowledged_by_user: Mapped[Optional["User"]] = relationship(  # type: ignore[name-defined]
        "User", foreign_keys=[acknowledged_by]
    )


__all__ = ["AIPredictiveMaintenance"]
