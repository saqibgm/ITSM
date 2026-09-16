"""
Best Buy Marketplace connector — MESSAGING-ONLY scope (2026-09-16), same
rationale as mercadolibre.py/allegro.py/etc.'s module docstrings.
fetch_orders() exists only to anchor a MarketplaceOrder row for messages
to attach to — no returns sync, no order_lines detail.

UNVERIFIED — no live seller account/credentials exist for this org's Best
Buy Marketplace account. Written directly against real, cited
documentation — but this is the BEST-DOCUMENTED connector in this whole
project: Best Buy Marketplace runs on Mirakl (confirmed instance:
bestbuyus-prod.mirakl.net), whose developer docs at developer.mirakl.com
are genuinely public and fetchable (unlike most other marketplaces in
this build, where docs were JS-rendered SPAs or 403'd automated fetches).
Endpoint paths and field names below are pulled directly from Mirakl's
own reference pages, not secondary sources.

Confirmed via direct research (2026-09-15/16):
- Real, two-way, order-tied messaging — Mirakl's "Inbox Threads" API:
  * M11 — GET /api/inbox/threads?entity_type=MMP_ORDER&entity_id={order_id}
    &with_messages=true — find/list the thread(s) for an order.
  * OR43 — POST /api/orders/{order_id}/threads — create a NEW thread
    (first message) on an order. Body: thread_input.body,
    thread_input.to (OPERATOR|SHOP|CUSTOMER), thread_input.topic
    ({type: FREE_TEXT|REASON_CODE}).
  * M12 — POST /api/inbox/threads/{thread_id}/message — reply to an
    EXISTING thread (max 1000 messages/thread, 30MB/message).
  Best Buy's own Marketplace Program Policies confirm buyers can contact
  sellers within order details, and third-party tools (eDesk,
  Sellercloud) are independently confirmed already pulling Best Buy
  customer messages via this exact Mirakl API in production.
- OR41 (GET /api/orders/{order_id}/messages, the OLDER messages
  endpoint) is DEPRECATED — Mirakl's own docs say it's being removed
  April 5 2027 in favor of M10/M11/M12 above. Built against the current
  API, not the deprecated one.
- Auth: a single API key (Shop-API-Key header) generated per-seller from
  the Seller Portal / Mirakl Connect SSO — no OAuth redirect, simplest
  auth model of any connector in this build.
- STRATEGIC NOTE (see docs/plans/REMAINING_MARKETPLACES_PLAN.md §3):
  Mirakl is a shared marketplace-tech platform powering multiple
  retailers beyond Best Buy — this connector is written Best-Buy-
  specific (BASE_URL hardcoded) for now rather than generalized, since
  generalizing before a second Mirakl-powered marketplace is actually
  needed would be exactly the kind of premature abstraction this
  codebase avoids. If a second one is ever added, extracting a shared
  MiraklConnector base (varying only by base_url) would be straightforward
  — the API calls below are already Mirakl-standard, not Best-Buy-specific.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

from app.models.marketplace import MarketplaceConnection, MarketplaceOrder
from app.services.marketplaces.connectors.base import (
    CommerceConnector,
    ConnectionResult,
    MessagingCapability,
    NormalizedMessage,
    NormalizedOrder,
    NormalizedReturn,
    SendResult,
)
from app.services.marketplaces.crypto import decrypt_secret, encrypt_secret

logger = logging.getLogger(__name__)

# Confirmed real Mirakl instance for Best Buy Marketplace.
_API_BASE = "https://bestbuyus-prod.mirakl.net"


class BestBuyConnector(CommerceConnector):
    provider = "bestbuy"
    # FULL — Mirakl's Inbox Threads API is genuinely two-way
    # (create/list/reply), the best-confirmed capability in this batch.
    # Unverified live, see module docstring.
    messaging_capability = MessagingCapability.FULL

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=15.0)
        return self._client

    async def connect(self, tenant_id: str, credentials: dict) -> ConnectionResult:
        """No OAuth — a single API key generated per-seller. Validated
        here by attempting a real, cheap authenticated call."""
        api_key = credentials.get("api_key")
        if not api_key:
            return ConnectionResult(success=False, error="missing api_key")

        client = await self._get_client()
        try:
            resp = await client.get(
                f"{_API_BASE}/api/orders",
                headers={"Authorization": api_key},
                params={"max": 1},
            )
            if resp.status_code != 200:
                return ConnectionResult(success=False, error=f"could not validate API key (status {resp.status_code})")
        except Exception as exc:
            logger.error("[BestBuy] connect validation failed: %r", exc, exc_info=True)
            return ConnectionResult(success=False, error=str(exc))
        return ConnectionResult(success=True)

    async def fetch_orders(
        self, connection: MarketplaceConnection, since: Optional[datetime] = None
    ) -> list[NormalizedOrder]:
        """Messaging-only scope — minimal order fetch, just enough to
        anchor a MarketplaceOrder row for a thread to reference."""
        api_key = decrypt_secret(connection.credentials["api_key"])
        client = await self._get_client()
        try:
            resp = await client.get(
                f"{_API_BASE}/api/orders",
                headers={"Authorization": api_key},
                params={"max": 50},
            )
            if resp.status_code != 200:
                logger.warning("[BestBuy] GET /api/orders -> %d: %s", resp.status_code, resp.text[:200])
                return []
            body = resp.json()
        except Exception as exc:
            logger.error("[BestBuy] GET /api/orders failed: %r", exc, exc_info=True)
            return []

        results = []
        for order in body.get("orders", []):
            customer = order.get("customer") or {}
            billing = customer.get("billing_address") or {}
            price = order.get("total_price")
            results.append(NormalizedOrder(
                external_order_id=str(order.get("order_id")),
                status=str(order.get("order_state") or "new"),
                order_lines=[],  # out of scope — see module docstring
                total_amount=float(price) if price is not None else None,
                currency=order.get("currency_iso_code"),
                buyer_email=customer.get("email"),
                buyer_name=" ".join(p for p in (billing.get("firstname"), billing.get("lastname")) if p) or None,
                placed_at=datetime.fromisoformat(order["created_date"].replace("Z", "+00:00")) if order.get("created_date") else None,
                raw_metadata=order,
            ))
        return results

    async def fetch_returns(
        self, connection: MarketplaceConnection, since: Optional[datetime] = None
    ) -> list[NormalizedReturn]:
        """Out of scope — messaging-only connector, see module docstring."""
        return []

    def parse_webhook(self, raw_payload: bytes, headers: dict) -> None:
        return None

    async def _find_thread_id(self, api_key: str, order: MarketplaceOrder) -> Optional[str]:
        client = await self._get_client()
        try:
            resp = await client.get(
                f"{_API_BASE}/api/inbox/threads",
                headers={"Authorization": api_key},
                params={"entity_type": "MMP_ORDER", "entity_id": order.external_order_id},
            )
            if resp.status_code != 200:
                return None
            body = resp.json()
        except Exception as exc:
            logger.error("[BestBuy] GET /api/inbox/threads failed: %r", exc, exc_info=True)
            return None

        threads = body.get("threads", [])
        return str(threads[0]["thread_id"]) if threads else None

    async def send_message(self, connection: MarketplaceConnection, order: MarketplaceOrder, message: str) -> SendResult:
        api_key = decrypt_secret(connection.credentials["api_key"])
        client = await self._get_client()

        thread_id = await self._find_thread_id(api_key, order)
        try:
            if thread_id:
                # M12 — reply to the existing thread.
                resp = await client.post(
                    f"{_API_BASE}/api/inbox/threads/{thread_id}/message",
                    headers={"Authorization": api_key},
                    json={"body": message},
                )
            else:
                # OR43 — create a new thread (first message) on this order.
                resp = await client.post(
                    f"{_API_BASE}/api/orders/{order.external_order_id}/threads",
                    headers={"Authorization": api_key},
                    json={"thread_input": {"body": message, "to": "CUSTOMER", "topic": {"type": "FREE_TEXT"}}},
                )
            if resp.status_code not in (200, 201):
                logger.warning("[BestBuy] send message -> %d: %s", resp.status_code, resp.text[:200])
                return SendResult(success=False, error=f"Best Buy returned {resp.status_code}")
            body = resp.json()
        except Exception as exc:
            logger.error("[BestBuy] send_message failed: %r", exc, exc_info=True)
            return SendResult(success=False, error=str(exc))
        return SendResult(success=True, external_message_id=str(body.get("message_id")) if body.get("message_id") else None)

    async def fetch_messages(self, connection: MarketplaceConnection, order: MarketplaceOrder) -> list[NormalizedMessage]:
        api_key = decrypt_secret(connection.credentials["api_key"])
        client = await self._get_client()
        try:
            resp = await client.get(
                f"{_API_BASE}/api/inbox/threads",
                headers={"Authorization": api_key},
                params={"entity_type": "MMP_ORDER", "entity_id": order.external_order_id, "with_messages": "true"},
            )
            if resp.status_code != 200:
                logger.warning("[BestBuy] GET /api/inbox/threads -> %d: %s", resp.status_code, resp.text[:200])
                return []
            body = resp.json()
        except Exception as exc:
            logger.error("[BestBuy] GET /api/inbox/threads failed: %r", exc, exc_info=True)
            return []

        results = []
        for thread in body.get("threads", []):
            thread_id = str(thread.get("thread_id"))
            for msg in thread.get("messages", []):
                sender_type = str(msg.get("from_type") or msg.get("author_type") or "").upper()
                results.append(NormalizedMessage(
                    external_message_id=str(msg.get("message_id")) if msg.get("message_id") else None,
                    external_order_id=order.external_order_id,
                    external_case_id=thread_id,
                    body=msg.get("body") or "",
                    sent_at=datetime.fromisoformat(msg["date_created"].replace("Z", "+00:00")) if msg.get("date_created") else None,
                    raw_metadata={"direction": "inbound" if sender_type == "CUSTOMER" else "outbound"},
                ))
        return results

    def order_url(self, connection: MarketplaceConnection, external_order_id: str) -> Optional[str]:
        """MEDIUM confidence — Mirakl's standard seller-portal order URL
        pattern, not confirmed against a documented permalink spec
        specific to Best Buy's instance."""
        return f"https://marketplace.bestbuy.com/order/detail/{external_order_id}"


bestbuy_connector = BestBuyConnector()

__all__ = ["BestBuyConnector", "bestbuy_connector"]
