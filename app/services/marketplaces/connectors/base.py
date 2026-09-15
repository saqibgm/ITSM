"""
Shared connector interface — every native marketplace connector implements this.

Per docs/plans/NATIVE_MARKETPLACE_CONNECTORS_PLAN.md §1.1. The ingestion layer
(app/services/marketplaces/ingestion.py) is written against this interface only,
never against a specific marketplace's SDK/API shape directly — that's what lets
marketplace #6 onward be added without touching the ingestion layer or the
webhook receiver.

MessagingCapability is deliberately a first-class, per-connector value, not an
afterthought — Phase 0's research (§2 of the plan) found it varies sharply by
marketplace: Amazon is outbound-only, Etsy has none at all, Shopify has no
concept of it, while Coupang/Mercado Libre/Trendyol/TikTok Shop/Shopee/Allegro/
Wildberries/Lazada/Cdiscount all have (or likely have) full bidirectional support.
The ingestion layer and the admin UI both need this value to decide whether to
even offer a "reply from ITSM" action for a given connection.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional


class MessagingCapability(str, Enum):
    NONE = "none"
    OUTBOUND_ONLY = "outbound_only"
    FULL = "full"


@dataclass
class NormalizedOrder:
    """Connector-agnostic shape for one order — what MarketplaceOrder is built from."""

    external_order_id: str
    status: str  # 'new' | 'acknowledged' | 'shipped' | 'delivered' | 'cancelled'
    order_lines: list[dict] = field(default_factory=list)
    total_amount: Optional[float] = None
    currency: Optional[str] = None
    buyer_email: Optional[str] = None
    # Added 2026-09-14 — the frontend's "Buyer" column needs a display name,
    # not just an email; email alone is also frequently unavailable (Amazon
    # needs separate PII/RDT access, eBay doesn't expose it at all — see
    # ebay.py's fetch_orders, which was actually storing eBay's USERNAME in
    # buyer_email before this fix, not a real email address).
    buyer_name: Optional[str] = None
    # Added 2026-09-14 — a genuine buyer-authored note captured AT CHECKOUT
    # (not a live two-way channel — a one-time field on the order itself).
    # Real, confirmed fields, not internal merchant notes: Etsy's
    # message_from_buyer, eBay's buyerCheckoutNotes. Both already come back
    # on the same fetch_orders() call every connector already makes — no new
    # API access needed. Amazon only has a narrow GiftMessage (gift orders
    # only, not general buyer communication) — not populated here since it
    # isn't really the same thing. Shopify/Walmart have no such field at all
    # (confirmed via direct doc research — Shopify's CommentEvent is
    # internal-staff-only AND read-only via API; Walmart's order schema has
    # no note/comment field whatsoever).
    buyer_note: Optional[str] = None
    placed_at: Optional[datetime] = None
    raw_metadata: dict = field(default_factory=dict)


@dataclass
class NormalizedReturn:
    """Connector-agnostic shape for one return/replacement case — what a
    service_request Ticket + MarketplaceOrderTicketLink is built from."""

    external_case_id: str
    external_order_id: str
    link_type: str  # 'return' | 'replacement'
    reason: Optional[str] = None
    status: Optional[str] = None
    raw_metadata: dict = field(default_factory=dict)


@dataclass
class NormalizedMessage:
    """Connector-agnostic shape for one inbound buyer message — what a
    TicketComment is built from. Only produced by connectors whose
    messaging_capability is FULL (or, in principle, a connector-specific
    inbound channel like Amazon's buyer-message email forward, if that's
    ever added as a connector-internal detail rather than a separate system)."""

    external_message_id: str
    external_order_id: Optional[str]
    external_case_id: Optional[str]
    body: str
    sent_at: Optional[datetime] = None
    raw_metadata: dict = field(default_factory=dict)


@dataclass
class ConnectionResult:
    success: bool
    external_id: Optional[str] = None
    error: Optional[str] = None
    # Raw token-exchange payload (access_token, refresh_token, expires_in,
    # scope, ...) for connectors whose OAuth code is single-use — the route
    # layer must persist THIS instead of re-exchanging the same code a
    # second time to "get the full payload for storage" (Shopify bug,
    # 2026-09-14: the code had already been consumed inside connect(), so
    # the second exchange got a 400 from Shopify). None for connectors that
    # don't need this (e.g. Walmart's client_credentials grant has nothing
    # code-shaped to reuse).
    credentials: Optional[dict] = None


@dataclass
class SendResult:
    success: bool
    external_message_id: Optional[str] = None
    error: Optional[str] = None


class CommerceConnector(ABC):
    """Abstract base every native marketplace connector implements.

    ``provider`` must match the value stored in MarketplaceConnection.provider
    (app/models/marketplace.py) and the {provider} path segment of the inbound
    webhook route, once that's added.
    """

    provider: str
    messaging_capability: MessagingCapability = MessagingCapability.NONE

    @abstractmethod
    async def connect(self, tenant_id: str, credentials: dict) -> ConnectionResult:
        """Establish/validate a connection for a tenant, given raw credentials
        (OAuth code, API key, etc. — shape is connector-specific)."""
        raise NotImplementedError

    @abstractmethod
    async def fetch_orders(
        self, connection: "MarketplaceConnection", since: Optional[datetime] = None
    ) -> list[NormalizedOrder]:
        """Manual/backfill path — pull current orders directly from the
        marketplace's API, for initial sync or an agent-triggered 'sync now'."""
        raise NotImplementedError

    @abstractmethod
    async def fetch_returns(
        self, connection: "MarketplaceConnection", since: Optional[datetime] = None
    ) -> list[NormalizedReturn]:
        """Manual/backfill path for return/replacement cases."""
        raise NotImplementedError

    async def register_webhooks(self, connection: "MarketplaceConnection") -> None:
        """Auto path — subscribe to the marketplace's webhook/notification
        mechanism, where one exists. Default no-op; override where applicable
        (most of the pilot batch support this; a few marketplaces route
        notifications through a different mechanism entirely, e.g. Amazon's
        SQS-based Notifications API rather than an inbound HTTP webhook)."""
        return None

    @abstractmethod
    def parse_webhook(
        self, raw_payload: bytes, headers: dict[str, str]
    ) -> Optional[NormalizedOrder | NormalizedReturn | NormalizedMessage]:
        """Auto path, called from the HTTP webhook route — verify signature/
        auth (using the raw body) and normalize one inbound delivery. Returns
        None for events this connector chooses to ignore."""
        raise NotImplementedError

    def normalize_event(
        self, event_type: str, payload: dict
    ) -> Optional[NormalizedOrder | NormalizedReturn | NormalizedMessage]:
        """Same mapping as parse_webhook, but called from a stored
        MarketplaceEvent row (event_type + already-decoded payload dict) —
        the Celery task's entry point, since by the time it runs, signature
        verification already happened at the webhook route and the raw HTTP
        bytes/headers aren't available anymore, only what got persisted.
        Default None, matching connectors whose parse_webhook also always
        returns None (no webhook route wired yet — see each connector's
        module docstring for why). Connectors with a real webhook route
        (currently only Shopify) override this with real topic-dispatch
        logic; parse_webhook then delegates to it so there's one mapping
        implementation, not two copies to keep in sync."""
        return None

    async def send_message(
        self, connection: "MarketplaceConnection", order_or_case_id: str, message: str
    ) -> SendResult:
        """Outbound half of capability #3 — an agent's reply from ITSM, pushed
        to the marketplace. Only meaningful when messaging_capability is
        OUTBOUND_ONLY or FULL; connectors with NONE should not override this
        (the ingestion layer checks messaging_capability before calling it,
        but this default makes the failure mode explicit rather than silent)."""
        return SendResult(success=False, error=f"{self.provider} connector does not support sending messages")

    def order_url(self, connection: "MarketplaceConnection", external_order_id: str) -> Optional[str]:
        """Deep link to this order's page in the marketplace's OWN seller
        admin UI (Shopify Admin, Amazon Seller Central, ...) — added
        2026-09-14 so the Orders/Returns pages can link out to the real
        record instead of only showing our synced copy. Default None for
        connectors that don't override it (shouldn't happen — every
        connector below does — but kept safe rather than assuming).

        Confidence varies per connector — see each override's docstring.
        Shopify and Amazon's URL patterns are well-established/highly
        confident; eBay and Walmart's are best-effort (direct doc research
        couldn't confirm the exact seller-UI deep-link path, only the base
        Seller Hub/Seller Center it lives under) — worth a live click-through
        once real order data exists for those two, not blindly trusted.
        """
        return None


__all__ = [
    "MessagingCapability",
    "NormalizedOrder",
    "NormalizedReturn",
    "NormalizedMessage",
    "ConnectionResult",
    "SendResult",
    "CommerceConnector",
]
