"""Post-incident review — retrospectives, action items, AI PIR drafts (Phase 8 / S8.5)."""

import enum
from datetime import datetime
from typing import Optional
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from uuid_extensions import uuid7

from app.models.base import Base, TimestampMixin


class RetroStatus(str, enum.Enum):
    draft = "draft"
    in_review = "in_review"
    published = "published"


class ActionItemStatus(str, enum.Enum):
    open = "open"
    in_progress = "in_progress"
    done = "done"


class PIRDraftStatus(str, enum.Enum):
    pending_review = "pending_review"
    accepted = "accepted"
    rejected = "rejected"


class IncidentRetrospective(Base, TimestampMixin):
    """One retrospective per incident (isolated via the incident FK)."""

    __tablename__ = "incident_retrospectives"

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    incident_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True), sa.ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False, unique=True,
    )
    summary: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    contributing_factors: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    impact: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    status: Mapped[str] = mapped_column(sa.VARCHAR(15), nullable=False, server_default=sa.text("'draft'"))
    published_at: Mapped[Optional[datetime]] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=True)


class RetroActionItem(Base):
    __tablename__ = "retro_action_items"

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    retro_id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), sa.ForeignKey("incident_retrospectives.id", ondelete="CASCADE"), nullable=False, index=True)
    description: Mapped[str] = mapped_column(sa.Text, nullable=False)
    owner_id: Mapped[Optional[UUID]] = mapped_column(sa.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    ticket_id: Mapped[Optional[UUID]] = mapped_column(sa.UUID(as_uuid=True), sa.ForeignKey("tickets.id", ondelete="SET NULL"), nullable=True)
    status: Mapped[str] = mapped_column(sa.VARCHAR(15), nullable=False, server_default=sa.text("'open'"))


class AIPIRDraft(Base):
    """AI/heuristic-generated post-incident review draft (HITL: accept/reject)."""

    __tablename__ = "ai_pir_drafts"

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    incident_id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), sa.ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False)
    summary: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    contributing_factors: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    impact: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    action_items: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(sa.VARCHAR(20), nullable=False, server_default=sa.text("'pending_review'"))
    model_version: Mapped[str] = mapped_column(sa.VARCHAR(40), nullable=False, server_default=sa.text("'template-v1'"))
    created_at: Mapped[datetime] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now())
