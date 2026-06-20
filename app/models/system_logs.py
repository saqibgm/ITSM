"""
ORM model for the system_logs table.

Dual-written by SystemLogDBHandler for WARNING+ log records.
Tenant admins and platform admins query this table in the UI.
Retained 1 year (enforced by Celery purge task — future session).

No updated_at column — rows are INSERT-only; only created_at from TimestampMixin
is used (updated_at is excluded by overriding the column set here).
"""

from typing import Optional
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from uuid_extensions import uuid7

from app.models.base import Base


class SystemLog(Base):
    """Application-level warning/error log entry, queryable from the UI.

    Stack traces (for CRITICAL / unhandled exceptions) are stored here but
    are *never* forwarded in HTTP responses — they exist for operator
    investigation only.
    """

    __tablename__ = "system_logs"
    __table_args__ = (
        sa.Index(
            "ix_syslog_tenant_level_created",
            "tenant_id",
            "level",
            "created_at",
            postgresql_ops={"created_at": "DESC"},
        ),
        sa.Index(
            "ix_syslog_level_created",
            "level",
            "created_at",
            postgresql_ops={"created_at": "DESC"},
        ),
    )

    id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True), primary_key=True, default=uuid7
    )
    tenant_id: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True),
        nullable=True,
        index=False,  # composite index above covers tenant_id queries
        comment="Nullable — tenant may be deleted but log row is retained",
    )
    request_id: Mapped[Optional[str]] = mapped_column(sa.VARCHAR(255), nullable=True)
    level: Mapped[str] = mapped_column(
        sa.VARCHAR(20),
        nullable=False,
        comment="warning | error | critical",
    )
    event: Mapped[str] = mapped_column(sa.VARCHAR(500), nullable=False)
    message: Mapped[str] = mapped_column(sa.Text, nullable=False)
    exception_type: Mapped[Optional[str]] = mapped_column(sa.VARCHAR(255), nullable=True)
    stack_trace: Mapped[Optional[str]] = mapped_column(
        sa.Text,
        nullable=True,
        comment="CRITICAL / unhandled only — never forwarded in HTTP responses",
    )
    metadata_: Mapped[dict] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        server_default=sa.text("'{}'::jsonb"),
    )
    created_at: Mapped[sa.DateTime] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )
