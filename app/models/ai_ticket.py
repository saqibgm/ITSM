"""
SQLAlchemy ORM models for AI ticket enrichment.

AITicketClassification — one record per ticket; stores Claude's suggestions
  and the agent's accept/reject decision (HITL gold dataset for fine-tuning).

AIDuplicateCandidate — one row per (ticket, candidate) pair found by pgvector
  cosine similarity; agents can dismiss candidates they disagree with.
"""

from datetime import datetime
from typing import Optional
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from uuid_extensions import uuid7

from app.models.base import Base


# ---------------------------------------------------------------------------
# AITicketClassification
# ---------------------------------------------------------------------------


class AITicketClassification(Base):
    """Stores Claude's classification suggestions for a ticket and the agent
    decision (accept/reject).  The actual_* columns capture ground-truth labels
    set by the agent — these form the HITL fine-tuning dataset.

    accepted IS NULL   → pending (agent has not yet decided)
    accepted = True    → agent accepted (fields listed in accepted_fields)
    accepted = False   → agent rejected
    """

    __tablename__ = "ai_ticket_classifications"

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)

    # One classification record per ticket
    ticket_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("tickets.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )

    # Suggested values (model output)
    suggested_category_id: Mapped[Optional[UUID]] = mapped_column(
        "suggested_category",
        sa.UUID(as_uuid=True),
        sa.ForeignKey("ticket_categories.id", ondelete="SET NULL"),
        nullable=True,
    )
    suggested_priority: Mapped[Optional[str]] = mapped_column(
        sa.VARCHAR(20), nullable=True
    )  # TicketPriority enum value
    suggested_assignee_id: Mapped[Optional[UUID]] = mapped_column(
        "suggested_assignee",
        sa.UUID(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    suggested_team_id: Mapped[Optional[UUID]] = mapped_column(
        "suggested_team",
        sa.UUID(as_uuid=True),
        sa.ForeignKey("teams.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Model metadata
    confidence: Mapped[float] = mapped_column(sa.Float, nullable=False)
    model_version: Mapped[str] = mapped_column(sa.VARCHAR(100), nullable=False)

    # Agent decision (NULL = pending)
    accepted: Mapped[Optional[bool]] = mapped_column(sa.Boolean, nullable=True)
    accepted_fields: Mapped[Optional[list]] = mapped_column(
        JSONB, nullable=True
    )  # e.g. ["priority", "category"]
    accepted_by: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Ground truth (HITL dataset) — what the agent ultimately set
    actual_category_id: Mapped[Optional[UUID]] = mapped_column(
        "actual_category",
        sa.UUID(as_uuid=True),
        sa.ForeignKey("ticket_categories.id", ondelete="SET NULL"),
        nullable=True,
    )
    actual_priority: Mapped[Optional[str]] = mapped_column(
        sa.VARCHAR(20), nullable=True
    )
    actual_team_id: Mapped[Optional[UUID]] = mapped_column(
        "actual_team",
        sa.UUID(as_uuid=True),
        sa.ForeignKey("teams.id", ondelete="SET NULL"),
        nullable=True,
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

    # Relationships
    ticket: Mapped["Ticket"] = relationship(  # type: ignore[name-defined]
        "Ticket", foreign_keys=[ticket_id]
    )
    suggested_category: Mapped[Optional["TicketCategory"]] = relationship(  # type: ignore[name-defined]
        "TicketCategory", foreign_keys=[suggested_category_id]
    )
    suggested_assignee: Mapped[Optional["User"]] = relationship(  # type: ignore[name-defined]
        "User", foreign_keys=[suggested_assignee_id]
    )
    suggested_team: Mapped[Optional["Team"]] = relationship(  # type: ignore[name-defined]
        "Team", foreign_keys=[suggested_team_id]
    )
    accepted_by_user: Mapped[Optional["User"]] = relationship(  # type: ignore[name-defined]
        "User", foreign_keys=[accepted_by]
    )
    actual_category: Mapped[Optional["TicketCategory"]] = relationship(  # type: ignore[name-defined]
        "TicketCategory", foreign_keys=[actual_category_id]
    )
    actual_team: Mapped[Optional["Team"]] = relationship(  # type: ignore[name-defined]
        "Team", foreign_keys=[actual_team_id]
    )


# ---------------------------------------------------------------------------
# AIDuplicateCandidate
# ---------------------------------------------------------------------------


class AIDuplicateCandidate(Base):
    """A potential duplicate ticket pair detected by pgvector cosine similarity.

    ticket_id     — the newly created ticket
    candidate_id  — the existing ticket that may be a duplicate
    dismissed     — agent explicitly dismissed this suggestion
    """

    __tablename__ = "ai_duplicate_candidates"
    __table_args__ = (
        sa.Index(
            "ix_ai_duplicate_candidates_ticket_score",
            "ticket_id",
            sa.text("similarity_score DESC"),
        ),
    )

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    ticket_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("tickets.id", ondelete="CASCADE"),
        nullable=False,
    )
    candidate_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("tickets.id", ondelete="CASCADE"),
        nullable=False,
    )
    similarity_score: Mapped[float] = mapped_column(sa.Float, nullable=False)
    model_version: Mapped[str] = mapped_column(sa.VARCHAR(100), nullable=False)
    dismissed: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.text("false")
    )
    dismissed_by: Mapped[Optional[UUID]] = mapped_column(
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
    ticket: Mapped["Ticket"] = relationship(  # type: ignore[name-defined]
        "Ticket", foreign_keys=[ticket_id]
    )
    candidate: Mapped["Ticket"] = relationship(  # type: ignore[name-defined]
        "Ticket", foreign_keys=[candidate_id]
    )
    dismissed_by_user: Mapped[Optional["User"]] = relationship(  # type: ignore[name-defined]
        "User", foreign_keys=[dismissed_by]
    )


# ---------------------------------------------------------------------------
# AIResponseSuggestion
# ---------------------------------------------------------------------------


class AIResponseSuggestion(Base):
    """A Claude-generated draft reply for a ticket.

    stored after the agent clicks "AI Draft"; ``used`` is set True (and
    ``used_by`` recorded) when the agent views the suggestion within 60 s of
    creation — indicating they used it to compose their reply.

    comment_id — optionally links to the comment the agent sent after using
                 this suggestion (populated externally, not here).
    """

    __tablename__ = "ai_response_suggestions"
    __table_args__ = (
        sa.Index(
            "ix_ai_response_suggestions_ticket_created",
            "ticket_id",
            sa.text("created_at DESC"),
        ),
    )

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)

    ticket_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("tickets.id", ondelete="CASCADE"),
        nullable=False,
    )
    comment_id: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("ticket_comments.id", ondelete="SET NULL"),
        nullable=True,
    )
    body: Mapped[str] = mapped_column(sa.Text, nullable=False)
    model_version: Mapped[str] = mapped_column(sa.VARCHAR(100), nullable=False)
    used: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.text("false")
    )
    used_by: Mapped[Optional[UUID]] = mapped_column(
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
    ticket: Mapped["Ticket"] = relationship(  # type: ignore[name-defined]
        "Ticket", foreign_keys=[ticket_id]
    )
    comment: Mapped[Optional["TicketComment"]] = relationship(  # type: ignore[name-defined]
        "TicketComment", foreign_keys=[comment_id]
    )
    used_by_user: Mapped[Optional["User"]] = relationship(  # type: ignore[name-defined]
        "User", foreign_keys=[used_by]
    )


__all__ = ["AITicketClassification", "AIDuplicateCandidate", "AIResponseSuggestion"]
