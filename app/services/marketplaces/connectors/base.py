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


__all__ = [
    "MessagingCapability",
    "NormalizedOrder",
    "NormalizedReturn",
    "NormalizedMessage",
    "ConnectionResult",
    "SendResult",
    "CommerceConnector",
]
