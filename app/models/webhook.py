"""
SQLAlchemy ORM models for Outbound Webhooks (S6B).

WebhookEndpoint — tenant-owned delivery target (URL + HMAC secret + event filters).
WebhookDelivery  — append-only delivery log; one row per HTTP attempt.
"""

from datetime import datetime
from typing import Optional
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from uuid_extensions import uuid7

from app.models.base import Base, TimestampMixin


class WebhookEndpoint(Base, TimestampMixin):
    """A configured outbound webhook endpoint for a tenant.

    ``events`` is a list of event-type patterns, e.g.:
        ["ticket.*", "asset.created", "kb_article.published"]

    ``failure_count`` tracks *consecutive* failures; resets to 0 on any
    successful delivery.  When it reaches 10 the endpoint is auto-disabled
    (``is_active = False``) to prevent hammering permanently-dead targets.
    """

    __tablename__ = "webhook_endpoints"
    __table_args__ = (
        sa.Index("idx_webhook_endpoints_tenant", "tenant_id", "is_active"),
    )

    id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True), primary_key=True, default=uuid7
    )
    tenant_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(sa.VARCHAR(255), nullable=False)
    url: Mapped[str] = mapped_column(sa.Text, nullable=False)
    # Stored as-is; used only for HMAC signing — never returned in API responses.
    secret: Mapped[str] = mapped_column(sa.Text, nullable=False)
    events: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")
    )
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    last_success_at: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    last_failure_at: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    failure_count: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=0, server_default=sa.text("0")
    )

    deliveries: Mapped[list["WebhookDelivery"]] = relationship(
        "WebhookDelivery",
        back_populates="endpoint",
        cascade="all, delete-orphan",
        order_by="WebhookDelivery.created_at.desc()",
    )


class WebhookDelivery(Base):
    """Append-only log of each webhook delivery attempt.

    One row is created with status='pending' before the HTTP call.
    The row is then updated to 'delivered', 'retrying', or 'failed'
    by the Celery worker.

    ``response_body`` is truncated to 500 chars to prevent large payloads
    from bloating the table.
    """

    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        sa.Index(
            "idx_webhook_deliveries_endpoint_status",
            "endpoint_id",
            "status",
            "next_retry_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True), primary_key=True, default=uuid7
    )
    endpoint_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("webhook_endpoints.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tenant_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True), nullable=False
    )
    event_type: Mapped[str] = mapped_column(sa.VARCHAR(100), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(
        sa.VARCHAR(20),
        nullable=False,
        default="pending",
        server_default=sa.text("'pending'"),
    )
    attempt_count: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=0, server_default=sa.text("0")
    )
    response_status_code: Mapped[Optional[int]] = mapped_column(
        sa.Integer, nullable=True
    )
    response_body: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    delivered_at: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    next_retry_at: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )

    endpoint: Mapped["WebhookEndpoint"] = relationship(
        "WebhookEndpoint", back_populates="deliveries"
    )
