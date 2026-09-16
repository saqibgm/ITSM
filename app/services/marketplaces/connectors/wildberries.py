"""
Wildberries connector — MESSAGING-ONLY scope (2026-09-15), same rationale
as mercadolibre.py/allegro.py/cdiscount.py/lazada.py's module docstrings.
fetch_orders() exists only to anchor a MarketplaceOrder row for messages
to attach to — no returns sync, no order_lines detail.

UNVERIFIED — no live sandbox/credentials exist for this org's Wildberries
account. Structurally the MOST UNCERTAIN connector in this batch, for a
reason none of the others have: Wildberries' chat system is NOT order-
scoped at all — confirmed via WB's own docs, "one chat = one buyer,"
not one chat per order. Every other connector in this build (including
the rest of this messaging-only batch) can look up "the conversation for
this order" directly; this one can only approximate it by matching a
chat to whatever buyer identifier the order and the chat list both
happen to expose, which was NOT confirmed to be reliable — treat the
order-to-chat matching logic below as the weakest link, more uncertain
than even Lazada's HMAC-signing risk.

Confirmed via direct research (2026-09-15):
- Real chat API exists: GET https://buyer-chat-api.wildberries.ru/api/v1/
  seller/chats (list), plus chat-events/send-message endpoints under the
  same host (dev.wildberries.ru/en/swagger/communications — the
  interactive Swagger page itself wasn't fetchable directly, so exact
  request/response field names below are reasonable WB-camelCase-
  convention guesses, confirmed real only where explicitly cited).
- BUYER-INITIATED ONLY — confirmed explicitly in WB's own docs. A seller
  cannot open a new chat, only reply to one the buyer already started
  (WB recommends replying within 10 days). send_message() below can
  only ever reply to an existing chat, never originate one — this is a
  hard platform constraint, not a choice made here.
- Rate limit: 10 requests / 10 seconds per seller account on the chat-list
  endpoint (confirmed) — tighter than eBay's Trading API limit (75/60s),
  worth keeping in mind if this connector's sync loop ever scales up.
- No buyer-note or reliable buyer-email field confirmed on the order
  schema — WB's own docs note buyer name/phone are only exposed after
  order assembly for FBS/DBS orders, and no email field was found at all.
- Auth: a single long-lived API token generated per-seller in the WB
  seller portal (no OAuth redirect), submitted directly — same
  client-credentials-adjacent shape as this repo's Walmart connector,
  except WB's is a bare static token, not a client_id/secret pair
  exchanged for a short-lived access token. A genuine separate sandbox
  host exists (marketplace-api-sandbox.wildberries.ru) with its own
  test-scope tokens.
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


def _base_urls(environment: str) -> tuple[str, str]:
    """Returns (chat_api_base, orders_api_base) — WB's sandbox is a
    genuinely separate host, same pattern as this repo's Allegro/Walmart
    connectors, confirmed via WB's own sandbox-environment doc page."""
    if environment == "sandbox":
        return "https://buyer-chat-api-sandbox.wildberries.ru", "https://marketplace-api-sandbox.wildberries.ru"
    return "https://buyer-chat-api.wildberries.ru", "https://marketplace-api.wildberries.ru"


class WildberriesConnector(CommerceConnector):
    provider = "wildberries"
    # FULL, with a real caveat: buyer-initiated only, see module docstring.
    # Still FULL rather than a narrower enum value since messaging_capability
    # describes "can this connector read AND write messages" at all, not
    # "can it originate a conversation" — same reasoning as how OUTBOUND_ONLY
    # is used elsewhere for a directional (not originator) restriction.
    messaging_capability = MessagingCapability.FULL

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=15.0)
        return self._client

    async def connect(self, tenant_id: str, credentials: dict) -> ConnectionResult:
        """No OAuth — the tenant supplies their own WB-issued API token
        directly, plus which environment it belongs to. Validated here by
        attempting a real chat-list call."""
        api_token = credentials.get("api_token")
        environment = credentials.get("environment", "sandbox")
        if not api_token:
            return ConnectionResult(success=False, error="missing api_token")

        chat_base, _ = _base_urls(environment)
        client = await self._get_client()
        try:
            resp = await client.get(
                f"{chat_base}/api/v1/seller/chats",
                headers={"Authorization": api_token},
            )
            if resp.status_code != 200:
                return ConnectionResult(success=False, error=f"could not validate token (status {resp.status_code}) — check api_token and environment")
        except Exception as exc:
            logger.error("[Wildberries] connect validation failed: %r", exc, exc_info=True)
            return ConnectionResult(success=False, error=str(exc))
        return ConnectionResult(success=True)

    async def fetch_orders(
        self, connection: MarketplaceConnection, since: Optional[datetime] = None
    ) -> list[NormalizedOrder]:
        """Messaging-only scope — minimal order fetch. buyer_name is the
        best-effort key later used to try to match this order to a chat
        (see module docstring's caveat on why that matching is uncertain)."""
        creds = connection.credentials
        api_token = decrypt_secret(creds["api_token"])
        _, orders_base = _base_urls(creds.get("environment", "sandbox"))
        client = await self._get_client()
        try:
            resp = await client.get(
                f"{orders_base}/api/v3/orders",
                headers={"Authorization": api_token},
                params={"limit": 50},
            )
            if resp.status_code != 200:
                logger.warning("[Wildberries] GET /api/v3/orders -> %d: %s", resp.status_code, resp.text[:200])
                return []
            body = resp.json()
        except Exception as exc:
            logger.error("[Wildberries] GET /api/v3/orders failed: %r", exc, exc_info=True)
            return []

        results = []
        for order in body.get("orders", []):
            total = order.get("price") or order.get("convertedPrice")
            results.append(NormalizedOrder(
                external_order_id=str(order.get("id")),
                status=str(order.get("status") or "new"),
                order_lines=[],  # out of scope — see module docstring
                total_amount=float(total) / 100 if total is not None else None,  # WB prices are commonly in kopecks (minor units) — /100 per common convention, unconfirmed against a real response
                currency=order.get("currencyCode") or "RUB",
                buyer_email=None,  # not exposed — see module docstring
                buyer_name=order.get("buyerName") or order.get("clientId"),  # UNCONFIRMED field name — best-effort
                placed_at=datetime.fromisoformat(order["createdAt"].replace("Z", "+00:00")) if order.get("createdAt") else None,
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

    async def _find_chat_id(self, connection: MarketplaceConnection, order: MarketplaceOrder) -> Optional[str]:
        """Best-effort match of this order to an EXISTING chat by comparing
        the order's buyer_name against each chat's client identifier — see
        module docstring for why this matching is the weakest link in this
        whole batch. Returns None (not an error) when no confident match
        exists, same as 'the buyer hasn't messaged about this order.'"""
        if not order.buyer_name:
            return None
        creds = connection.credentials
        api_token = decrypt_secret(creds["api_token"])
        chat_base, _ = _base_urls(creds.get("environment", "sandbox"))
        client = await self._get_client()
        try:
            resp = await client.get(
                f"{chat_base}/api/v1/seller/chats",
                headers={"Authorization": api_token},
            )
            if resp.status_code != 200:
                return None
            body = resp.json()
        except Exception as exc:
            logger.error("[Wildberries] GET /seller/chats failed: %r", exc, exc_info=True)
            return None

        for chat in body.get("result", body.get("chats", [])):
            client_name = chat.get("clientName") or chat.get("buyerName")
            if client_name and client_name == order.buyer_name:
                return str(chat.get("chatID") or chat.get("id"))
        return None

    async def send_message(self, connection: MarketplaceConnection, order: MarketplaceOrder, message: str) -> SendResult:
        chat_id = await self._find_chat_id(connection, order)
        if not chat_id:
            return SendResult(success=False, error="no existing chat found for this order's buyer — Wildberries only allows replying to a chat the buyer already started, not originating one (and the order-to-chat match itself is best-effort, see module docstring)")

        creds = connection.credentials
        api_token = decrypt_secret(creds["api_token"])
        chat_base, _ = _base_urls(creds.get("environment", "sandbox"))
        client = await self._get_client()
        try:
            resp = await client.post(
                f"{chat_base}/api/v1/seller/message",
                headers={"Authorization": api_token},
                json={"chatID": chat_id, "message": message},
            )
            if resp.status_code not in (200, 201):
                logger.warning("[Wildberries] send message -> %d: %s", resp.status_code, resp.text[:200])
                return SendResult(success=False, error=f"Wildberries returned {resp.status_code}")
        except Exception as exc:
            logger.error("[Wildberries] send_message failed: %r", exc, exc_info=True)
            return SendResult(success=False, error=str(exc))
        return SendResult(success=True)

    async def fetch_messages(self, connection: MarketplaceConnection, order: MarketplaceOrder) -> list[NormalizedMessage]:
        chat_id = await self._find_chat_id(connection, order)
        if not chat_id:
            return []

        creds = connection.credentials
        api_token = decrypt_secret(creds["api_token"])
        chat_base, _ = _base_urls(creds.get("environment", "sandbox"))
        client = await self._get_client()
        try:
            resp = await client.get(
                f"{chat_base}/api/v1/seller/events",
                headers={"Authorization": api_token},
                params={"chatID": chat_id},
            )
            if resp.status_code != 200:
                logger.warning("[Wildberries] GET events -> %d: %s", resp.status_code, resp.text[:200])
                return []
            body = resp.json()
        except Exception as exc:
            logger.error("[Wildberries] GET events failed: %r", exc, exc_info=True)
            return []

        results = []
        for event in body.get("result", body.get("events", [])):
            if event.get("type") and event.get("type") != "message":
                continue  # skip non-message events (e.g. refund events, per module docstring)
            sender = str(event.get("sender", "")).upper()
            results.append(NormalizedMessage(
                external_message_id=str(event.get("id")) if event.get("id") else None,
                external_order_id=order.external_order_id,
                external_case_id=chat_id,
                body=(event.get("message") or {}).get("text") if isinstance(event.get("message"), dict) else event.get("message") or "",
                sent_at=datetime.fromisoformat(event["addTime"].replace("Z", "+00:00")) if event.get("addTime") else None,
                raw_metadata={"direction": "inbound" if sender == "CLIENT" else "outbound"},
            ))
        return results

    def order_url(self, connection: MarketplaceConnection, external_order_id: str) -> Optional[str]:
        """LOW confidence — no documented seller-portal order-permalink
        pattern found; best effort guess only."""
        return f"https://seller.wildberries.ru/orders/all?orderId={external_order_id}"


wildberries_connector = WildberriesConnector()

__all__ = ["WildberriesConnector", "wildberries_connector"]
