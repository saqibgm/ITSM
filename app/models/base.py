from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from uuid_extensions import uuid7


def uuid7_pk() -> sa.Column:
    return sa.Column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        server_default=sa.func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
        nullable=False,
    )


class TenantScopedMixin:
    tenant_id: Mapped[sa.UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        nullable=False,
        index=True,
    )
