"""
SQLAlchemy ORM models for the Virtual Agent domain (S4B.1).

Two tables:
  - virtual_agent_sessions  — one session per user conversation
  - virtual_agent_messages  — individual turns within a session
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
# VirtualAgentSession
# ---------------------------------------------------------------------------


class VirtualAgentSession(Base, TimestampMixin):
    """A single conversation session between a user and the virtual agent.

    Sessions are tenant-scoped.  user_id is nullable so anonymous sessions
    (e.g. unauthenticated widget) are allowed at the data layer even if the
    API currently requires a bearer token.
    """

    __tablename__ = "virtual_agent_sessions"
    __table_args__ = (
        sa.Index("idx_virtual_agent_sessions_tenant_id", "tenant_id"),
    )

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)

    tenant_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        nullable=False,
    )

    user_id: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    channel: Mapped[str] = mapped_column(
        sa.VARCHAR(50),
        nullable=False,
        comment="web_widget | mobile | slack | teams | api",
    )

    status: Mapped[str] = mapped_column(
        sa.VARCHAR(50),
        nullable=False,
        default="active",
        server_default=sa.text("'active'"),
        comment="active | closed | handed_off",
    )

    intent: Mapped[Optional[str]] = mapped_column(
        sa.VARCHAR(100),
        nullable=True,
        comment="Last detected intent",
    )

    context_summary: Mapped[Optional[str]] = mapped_column(
        sa.Text,
        nullable=True,
        comment="Rolling summary for long conversations",
    )

    handed_off_to_user_id: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    ended_at: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        nullable=True,
    )

    # relationships
    user: Mapped[Optional["User"]] = relationship(  # type: ignore[name-defined]
        "User", foreign_keys=[user_id]
    )
    handed_off_to: Mapped[Optional["User"]] = relationship(  # type: ignore[name-defined]
        "User", foreign_keys=[handed_off_to_user_id]
    )
    messages: Mapped[list["VirtualAgentMessage"]] = relationship(
        "VirtualAgentMessage",
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="VirtualAgentMessage.created_at",
    )


# ---------------------------------------------------------------------------
# VirtualAgentMessage
# ---------------------------------------------------------------------------


class VirtualAgentMessage(Base):
    """A single message turn within a VirtualAgentSession."""

    __tablename__ = "virtual_agent_messages"
    __table_args__ = (
        sa.Index(
            "idx_virtual_agent_messages_session_created",
            "session_id",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)

    session_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("virtual_agent_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )

    role: Mapped[str] = mapped_column(
        sa.VARCHAR(20),
        nullable=False,
        comment="user | assistant",
    )

    content: Mapped[str] = mapped_column(sa.Text, nullable=False)

    intent_detected: Mapped[Optional[str]] = mapped_column(
        sa.VARCHAR(100),
        nullable=True,
    )

    sources: Mapped[Optional[dict]] = mapped_column(
        JSONB,
        nullable=True,
        comment="List of {article_id, title, excerpt} dicts",
    )

    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )

    # relationship
    session: Mapped["VirtualAgentSession"] = relationship(
        "VirtualAgentSession", back_populates="messages"
    )
