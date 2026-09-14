"""
SQLAlchemy ORM models for native marketplace-integration (V3-Marketplaces).

Scope per docs/plans/NATIVE_MARKETPLACE_CONNECTORS_PLAN.md §1/§4: sync orders,
returns/replacement, and messaging from marketplaces (Amazon, Shopify, Walmart,
eBay, Etsy first — §5's pilot batch) into ITSM tickets, via native per-marketplace
connectors rather than a middleware aggregator.

MarketplaceConnection   — a tenant's credentials/connection to one marketplace.
MarketplaceEvent        — append-only inbound-event log; idempotency + audit trail,
                          mirrors WebhookDelivery's shape but for inbound events.
MarketplaceOrder        — synced order record (capability #1).
MarketplaceOrderTicketLink — links an order to the Ticket opened for its
                          return/replacement case (capability #2); mirrors
                          AssetTicketLink's pattern.
MarketplaceIntegrationSettings — tenant-level config (§0a): enabled marketplaces,
                          per-event-type auto/manual mapping. Tenant-scoped only,
                          no system-level default layer (simplified during planning).

All PKs are UUID v7. Tenant-scoped tables get RLS (tenant_isolation policy),
same as every other multi-tenant table in this codebase (see migration 0039).
"""

from datetime import datetime
from typing import Optional
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from uuid_extensions import uuid7

from app.models.base import Base, TenantScopedMixin, TimestampMixin


# ---------------------------------------------------------------------------
# MarketplaceConnection
# ---------------------------------------------------------------------------


class MarketplaceConnection(Base, TimestampMixin, TenantScopedMixin):
    """A tenant's connection to one native marketplace connector.

    ``provider`` matches a registered ``CommerceConnector.provider`` value
    (app/services/marketplaces/connectors/base.py) — e.g. 'amazon', 'shopify',
    'walmart', 'ebay', 'etsy' for the §5 pilot batch, more added incrementally.

    ``credentials`` holds provider-specific auth (OAuth tokens, API keys) —
    encrypted at rest, same Fernet pattern as existing secret storage elsewhere
    in this codebase. ``provider_metadata`` holds anything connector-specific
    that doesn't warrant its own column (e.g. Amazon's marketplace_id list).
    """

    __tablename__ = "marketplace_connections"
    __table_args__ = (
        sa.UniqueConstraint("tenant_id", "provider", name="uq_marketplace_connections_tenant_provider"),
        sa.Index("ix_marketplace_connections_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    provider: Mapped[str] = mapped_column(sa.VARCHAR(50), nullable=False)
    external_id: Mapped[Optional[str]] = mapped_column(sa.VARCHAR(255), nullable=True)
    credentials: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    status: Mapped[str] = mapped_column(
        sa.VARCHAR(20), nullable=False, default="disconnected",
        server_default=sa.text("'disconnected'"),
    )
    messaging_capability: Mapped[str] = mapped_column(
        sa.VARCHAR(20), nullable=False, default="none",
        server_default=sa.text("'none'"),
        comment="'none' | 'outbound_only' | 'full' — set per-connector per Phase 0's "
                "per-marketplace findings; drives whether the UI offers a reply action",
    )
    provider_metadata: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    last_synced_at: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )

    events: Mapped[list["MarketplaceEvent"]] = relationship(
        "MarketplaceEvent", back_populates="connection", cascade="all, delete-orphan"
    )
    orders: Mapped[list["MarketplaceOrder"]] = relationship(
        "MarketplaceOrder", back_populates="connection", cascade="all, delete-orphan"
    )


# ---------------------------------------------------------------------------
# MarketplaceEvent — append-only inbound-event log
# ---------------------------------------------------------------------------


class MarketplaceEvent(Base, TenantScopedMixin):
    """Append-only log of one inbound event from a marketplace connector.

    Gives idempotency (UNIQUE provider+external_event_id — a replayed webhook
    or a re-run backfill returns the existing row instead of reprocessing) and
    an audit/replay trail, mirroring WebhookDelivery's shape but for the
    inbound direction.

    Written 'received' before processing, updated by the Celery task
    (app/workers/tasks_marketplace_sync.py) to its terminal status.
    """

    __tablename__ = "marketplace_events"
    __table_args__ = (
        sa.UniqueConstraint("provider", "external_event_id", name="uq_marketplace_events_provider_external_id"),
        sa.Index("ix_marketplace_events_tenant_status", "tenant_id", "status"),
        sa.Index("ix_marketplace_events_connection", "connection_id"),
    )

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    connection_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("marketplace_connections.id", ondelete="CASCADE"),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(sa.VARCHAR(50), nullable=False)
    external_event_id: Mapped[str] = mapped_column(sa.VARCHAR(255), nullable=False)
    event_type: Mapped[str] = mapped_column(sa.VARCHAR(100), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(
        sa.VARCHAR(20), nullable=False, default="received",
        server_default=sa.text("'received'"),
        comment="received | processed | ticket_created | order_updated | "
                "comment_added | failed | ignored",
    )
    resulting_ticket_id: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True), sa.ForeignKey("tickets.id", ondelete="SET NULL"), nullable=True
    )
    resulting_order_id: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True), sa.ForeignKey("marketplace_orders.id", ondelete="SET NULL"), nullable=True
    )
    error_message: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    received_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
    )
    processed_at: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )

    connection: Mapped["MarketplaceConnection"] = relationship(
        "MarketplaceConnection", back_populates="events"
    )


# ---------------------------------------------------------------------------
# MarketplaceOrder
# ---------------------------------------------------------------------------


class MarketplaceOrder(Base, TimestampMixin, TenantScopedMixin):
    """A synced order (capability #1) — silent upsert by default (§3's
    conservative-default table); does not itself create a Ticket unless a
    return/replacement event links one via MarketplaceOrderTicketLink.
    """

    __tablename__ = "marketplace_orders"
    __table_args__ = (
        sa.UniqueConstraint("tenant_id", "provider", "external_order_id", name="uq_marketplace_orders_tenant_provider_external_id"),
        sa.Index("ix_marketplace_orders_connection", "connection_id"),
    )

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    connection_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("marketplace_connections.id", ondelete="CASCADE"),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(sa.VARCHAR(50), nullable=False)
    external_order_id: Mapped[str] = mapped_column(sa.VARCHAR(255), nullable=False)
    status: Mapped[str] = mapped_column(
        sa.VARCHAR(20), nullable=False, default="new", server_default=sa.text("'new'"),
        comment="new | acknowledged | shipped | delivered | cancelled",
    )
    buyer_email: Mapped[Optional[str]] = mapped_column(
        sa.VARCHAR(320), nullable=True,
        comment="PII — encrypt at rest before production use (SHOPIFY_INTEGRATION_PLAN.md §4.6 precedent)",
    )
    buyer_name: Mapped[Optional[str]] = mapped_column(
        sa.VARCHAR(255), nullable=True,
        comment="PII — added 2026-09-14, same encrypt-at-rest note as buyer_email. Display name for the "
                "frontend's Buyer column; email alone is frequently unavailable (Amazon needs PII/RDT "
                "access, eBay doesn't expose it at all — eBay populates this with the buyer's username instead).",
    )
    order_lines: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=sa.text("'[]'::jsonb"))
    total_amount: Mapped[Optional[float]] = mapped_column(sa.Numeric(12, 2), nullable=True)
    currency: Mapped[Optional[str]] = mapped_column(sa.VARCHAR(3), nullable=True)
    placed_at: Mapped[Optional[datetime]] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=True)
    raw_metadata: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=sa.text("'{}'::jsonb"))

    connection: Mapped["MarketplaceConnection"] = relationship(
        "MarketplaceConnection", back_populates="orders"
    )
    ticket_links: Mapped[list["MarketplaceOrderTicketLink"]] = relationship(
        "MarketplaceOrderTicketLink", back_populates="order", cascade="all, delete-orphan"
    )
    messages: Mapped[list["MarketplaceMessage"]] = relationship(
        "MarketplaceMessage", back_populates="order", cascade="all, delete-orphan"
    )


# ---------------------------------------------------------------------------
# MarketplaceMessage — capability #3's own record, independent of whether the
# order has a linked return/replacement ticket. Added 2026-09-14: the
# original design only recorded an outbound send as a TicketComment via
# MarketplaceOrderTicketLink, which meant a message on a plain order (no
# return/replacement) vanished from any queryable record after sending —
# a real gap once a dedicated cross-order Messaging page was asked for, not
# just a per-row "send" action with no history view.
# ---------------------------------------------------------------------------


class MarketplaceMessage(Base, TenantScopedMixin):
    """One message in either direction for one order — the read+write record
    behind the standalone Messaging page, as opposed to MarketplaceOrderTicketLink's
    ticket-comment mirror (kept alongside this, not replaced by it, so a
    return/replacement ticket's thread still shows the same message inline
    too — see marketplace_sync.py's send_message_to_buyer()).
    """

    __tablename__ = "marketplace_messages"
    __table_args__ = (
        sa.Index("ix_marketplace_messages_order", "order_id"),
        sa.Index("ix_marketplace_messages_tenant_sent", "tenant_id", "sent_at"),
    )

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    order_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("marketplace_orders.id", ondelete="CASCADE"),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(sa.VARCHAR(50), nullable=False)
    direction: Mapped[str] = mapped_column(sa.VARCHAR(10), nullable=False, comment="'outbound' | 'inbound'")
    body: Mapped[str] = mapped_column(sa.Text, nullable=False)
    external_message_id: Mapped[Optional[str]] = mapped_column(sa.VARCHAR(255), nullable=True)
    # Set only for outbound messages sent by an agent through the ITSM UI —
    # None for inbound (buyer-authored, no local User row to point at) and
    # for any future auto/system-sent outbound message.
    sent_by_user_id: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    sent_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
    )

    order: Mapped["MarketplaceOrder"] = relationship("MarketplaceOrder", back_populates="messages")


# ---------------------------------------------------------------------------
# MarketplaceOrderTicketLink — junction, mirrors AssetTicketLink
# ---------------------------------------------------------------------------


class MarketplaceOrderTicketLink(Base):
    """Links a MarketplaceOrder to the Ticket opened for its return/replacement
    case (capability #2). One order can have zero (never returned) or more than
    one link (multiple partial-return events on the same order).

    No tenant_id/RLS of its own, same precedent as AssetTicketLink — scoping is
    inherited transitively via the FK'd order/ticket rows, which are themselves
    tenant-scoped and RLS-protected.
    """

    __tablename__ = "marketplace_order_ticket_links"

    order_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("marketplace_orders.id", ondelete="CASCADE"),
        primary_key=True,
    )
    ticket_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("tickets.id", ondelete="CASCADE"),
        primary_key=True,
    )
    link_type: Mapped[str] = mapped_column(
        sa.VARCHAR(20), nullable=False, comment="'return' | 'replacement'"
    )
    linked_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
    )

    order: Mapped["MarketplaceOrder"] = relationship(
        "MarketplaceOrder", back_populates="ticket_links"
    )


# ---------------------------------------------------------------------------
# MarketplaceIntegrationSettings — tenant-level config only (§0a)
# ---------------------------------------------------------------------------


class MarketplaceIntegrationSettings(Base, TimestampMixin):
    """Tenant-level marketplace-integration config — one row per tenant, no
    system-level default/override layer (simplified during planning to
    tenant-only). ``settings`` holds enabled_marketplaces + per-event-type
    auto/manual mapping (§3 of the plan), read/written via the tenant-scoped
    admin config page (§0a).
    """

    __tablename__ = "marketplace_integration_settings"

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), nullable=False, unique=True)
    settings: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=sa.text("'{}'::jsonb"))
    updated_by: Mapped[Optional[UUID]] = mapped_column(sa.UUID(as_uuid=True), nullable=True)


__all__ = [
    "MarketplaceConnection",
    "MarketplaceEvent",
    "MarketplaceOrder",
    "MarketplaceOrderTicketLink",
    "MarketplaceIntegrationSettings",
]
