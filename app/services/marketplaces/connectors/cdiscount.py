"""
Cdiscount connector — MESSAGING-ONLY scope (2026-09-15), same rationale as
mercadolibre.py/allegro.py's module docstrings. fetch_orders() exists only
to anchor a MarketplaceOrder row for messages to attach to — no returns
sync, no order_lines detail.

UNVERIFIED — no live sandbox/credentials exist for this org's Cdiscount
account. Written directly against real, cited documentation. Treat every
endpoint/field name here as needing a live check before trusting it in
production — more so than usual for this connector specifically, since the
Discussions API's exact request/response body shapes weren't as concretely
documented in the research pass as Mercado Libre/Allegro's were (endpoint
PATHS are confirmed real; body field names below are reasonable REST-
convention guesses, not verbatim-confirmed).

Confirmed via direct research (2026-09-15):
- Cdiscount's marketplace API runs on OCTOPIA (their marketplace tech
  platform — api.octopia-io.net), not a Cdiscount-branded host. A seller
  connects via Octopia regardless of which Octopia-network retailer
  (Cdiscount, and others) they're selling on.
- Real, two-way, structured messaging system — NOT literally called
  "chat," but a genuine "Discussions Management" resource with read
  receipts: GET/POST /seller/v2/discussions (list/create), POST
  /seller/v2/messages (send within a discussion), PATCH /seller/v2/
  messages/{messageId}, PATCH /seller/v2/discussions/{discussionId}.
  Creating a discussion needs an order reference; only available if the
  sales channel has seller-initiated discussions enabled (not confirmed
  whether this org's Cdiscount channel does).
- No buyer-note/checkout-comment field found on the order schema.
- shippingAddress.email exists but is explicitly documented as "can be
  expressed as an anonymised email specific to the current order" — same
  relay-address pattern as Amazon's BuyerInfo.BuyerEmail. Populated as
  best-effort buyer_email; may not be a real deliverable address.
- Auth: OAuth 2.0 client_credentials grant against
  https://auth.octopia-io.net/auth/realms/maas/protocol/openid-connect/
  token — NOT a redirect/authorize flow, the tenant supplies their own
  Octopia-issued client_id/client_secret directly, same shape as this
  repo's existing Walmart connector. Tokens are VERY short-lived (5
  minutes, confirmed via docs) — the shortest of any connector in this
  build, refreshed more aggressively than even Walmart's ~15-minute
  window.
"""

import logging
from datetime import datetime, timedelta, timezone
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

_TOKEN_URL = "https://auth.octopia-io.net/auth/realms/maas/protocol/openid-connect/token"
_API_BASE = "https://api.octopia-io.net/seller/v2"
_REFRESH_SKEW = timedelta(minutes=1)  # tokens expire every 5 min — tight skew, more aggressive than Walmart's 3-min on a 15-min token


class CdiscountConnector(CommerceConnector):
    provider = "cdiscount"
    # FULL — the Discussions API is genuinely two-way (create/list/reply),
    # confirmed via Octopia's own docs. Unverified live, see module docstring.
    messaging_capability = MessagingCapability.FULL

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=15.0)
        return self._client

    async def connect(self, tenant_id: str, credentials: dict) -> ConnectionResult:
        """No OAuth redirect — same shape as this repo's Walmart connector.
        The tenant supplies their own Octopia-issued seller_id/api_key
        (client_id/client_secret in OAuth terms) directly."""
        seller_id = credentials.get("seller_id")
        api_key = credentials.get("api_key")
        if not seller_id or not api_key:
            return ConnectionResult(success=False, error="missing seller_id or api_key")

        token = await self._get_token(seller_id, api_key)
        if not token:
            return ConnectionResult(success=False, error="could not obtain access token — check seller_id/api_key")
        return ConnectionResult(success=True, external_id=seller_id)

    async def _get_token(self, seller_id: str, api_key: str) -> Optional[str]:
        client = await self._get_client()
        try:
            resp = await client.post(
                _TOKEN_URL,
                data={"grant_type": "client_credentials", "client_id": seller_id, "client_secret": api_key},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if resp.status_code != 200:
                logger.warning("[Cdiscount] token grant -> %d: %s", resp.status_code, resp.text[:200])
                return None
            return resp.json().get("access_token")
        except Exception as exc:
            logger.error("[Cdiscount] token grant failed: %r", exc, exc_info=True)
            return None

    async def _ensure_fresh_token(self, connection: MarketplaceConnection) -> Optional[str]:
        creds = connection.credentials
        expires_at_raw = creds.get("access_token_expires_at")
        expires_at = datetime.fromisoformat(expires_at_raw) if expires_at_raw else None
        cached_token = creds.get("access_token")
        if cached_token and expires_at and expires_at - datetime.now(timezone.utc) > _REFRESH_SKEW:
            return decrypt_secret(cached_token)

        token = await self._get_token(decrypt_secret(creds["seller_id"]), decrypt_secret(creds["api_key"]))
        if not token:
            return None
        creds["access_token"] = encrypt_secret(token)
        creds["access_token_expires_at"] = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        return token

    async def fetch_orders(
        self, connection: MarketplaceConnection, since: Optional[datetime] = None
    ) -> list[NormalizedOrder]:
        """Messaging-only scope — minimal order fetch, just enough to
        anchor a MarketplaceOrder row for a discussion to reference."""
        token = await self._ensure_fresh_token(connection)
        if not token:
            return []
        client = await self._get_client()
        try:
            resp = await client.get(
                f"{_API_BASE}/orders",
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code != 200:
                logger.warning("[Cdiscount] GET /orders -> %d: %s", resp.status_code, resp.text[:200])
                return []
            body = resp.json()
        except Exception as exc:
            logger.error("[Cdiscount] GET /orders failed: %r", exc, exc_info=True)
            return []

        results = []
        for order in body.get("orders", body if isinstance(body, list) else []):
            total = order.get("totalAmount") or order.get("total")
            shipping = order.get("shippingAddress") or {}
            results.append(NormalizedOrder(
                external_order_id=str(order.get("orderId") or order.get("id")),
                status=str(order.get("status") or "new"),
                order_lines=[],  # out of scope — see module docstring
                total_amount=float(total) if total is not None else None,
                currency=order.get("currency"),
                # Anonymised relay address per Octopia's own docs, not
                # guaranteed a real deliverable email — see module docstring.
                buyer_email=shipping.get("email"),
                buyer_name=shipping.get("name") or shipping.get("firstName"),
                placed_at=datetime.fromisoformat(order["creationDate"].replace("Z", "+00:00")) if order.get("creationDate") else None,
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

    async def _find_or_create_discussion(self, connection: MarketplaceConnection, token: str, order: MarketplaceOrder) -> Optional[str]:
        """Looks up an existing discussion for this order, or creates one
        if none exists — UNCONFIRMED whether seller-initiated discussion
        creation is actually enabled for this org's sales channel (see
        module docstring); this attempts it and surfaces whatever error
        Octopia returns rather than assuming either way."""
        client = await self._get_client()
        try:
            resp = await client.get(
                f"{_API_BASE}/discussions",
                headers={"Authorization": f"Bearer {token}"},
                params={"orderId": order.external_order_id},
            )
            if resp.status_code == 200:
                body = resp.json()
                discussions = body.get("discussions", body if isinstance(body, list) else [])
                if discussions:
                    return str(discussions[0].get("discussionId") or discussions[0].get("id"))
        except Exception as exc:
            logger.error("[Cdiscount] GET /discussions failed: %r", exc, exc_info=True)

        try:
            resp = await client.post(
                f"{_API_BASE}/discussions",
                headers={"Authorization": f"Bearer {token}"},
                json={"orderId": order.external_order_id},
            )
            if resp.status_code not in (200, 201):
                logger.warning("[Cdiscount] POST /discussions -> %d: %s", resp.status_code, resp.text[:200])
                return None
            body = resp.json()
            return str(body.get("discussionId") or body.get("id"))
        except Exception as exc:
            logger.error("[Cdiscount] POST /discussions failed: %r", exc, exc_info=True)
            return None

    async def send_message(self, connection: MarketplaceConnection, order: MarketplaceOrder, message: str) -> SendResult:
        token = await self._ensure_fresh_token(connection)
        if not token:
            return SendResult(success=False, error="could not refresh token")

        discussion_id = await self._find_or_create_discussion(connection, token, order)
        if not discussion_id:
            return SendResult(success=False, error="could not find or create a discussion for this order (seller-initiated discussions may not be enabled for this sales channel — unconfirmed)")

        client = await self._get_client()
        try:
            resp = await client.post(
                f"{_API_BASE}/messages",
                headers={"Authorization": f"Bearer {token}"},
                json={"discussionId": discussion_id, "content": message},
            )
            if resp.status_code not in (200, 201):
                logger.warning("[Cdiscount] POST /messages -> %d: %s", resp.status_code, resp.text[:200])
                return SendResult(success=False, error=f"Cdiscount returned {resp.status_code}")
            body = resp.json()
        except Exception as exc:
            logger.error("[Cdiscount] send_message failed: %r", exc, exc_info=True)
            return SendResult(success=False, error=str(exc))
        return SendResult(success=True, external_message_id=str(body.get("messageId") or body.get("id") or "") or None)

    async def fetch_messages(self, connection: MarketplaceConnection, order: MarketplaceOrder) -> list[NormalizedMessage]:
        token = await self._ensure_fresh_token(connection)
        if not token:
            return []
        client = await self._get_client()
        try:
            resp = await client.get(
                f"{_API_BASE}/discussions",
                headers={"Authorization": f"Bearer {token}"},
                params={"orderId": order.external_order_id},
            )
            if resp.status_code != 200:
                return []
            body = resp.json()
            discussions = body.get("discussions", body if isinstance(body, list) else [])
            if not discussions:
                return []
            discussion_id = str(discussions[0].get("discussionId") or discussions[0].get("id"))
        except Exception as exc:
            logger.error("[Cdiscount] GET /discussions failed: %r", exc, exc_info=True)
            return []

        try:
            resp = await client.get(
                f"{_API_BASE}/discussions/{discussion_id}/messages",
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code != 200:
                logger.warning("[Cdiscount] GET messages -> %d: %s", resp.status_code, resp.text[:200])
                return []
            body = resp.json()
        except Exception as exc:
            logger.error("[Cdiscount] GET messages failed: %r", exc, exc_info=True)
            return []

        results = []
        for msg in body.get("messages", body if isinstance(body, list) else []):
            # author/sender field name UNCONFIRMED — "author" is a
            # reasonable REST-convention guess, not verbatim-documented.
            # Falls back to "outbound" (assumed ours) when unclear, same
            # safer-default reasoning as Allegro's connector.
            author = (msg.get("author") or msg.get("sender") or "").upper()
            results.append(NormalizedMessage(
                external_message_id=str(msg.get("messageId") or msg.get("id")) if (msg.get("messageId") or msg.get("id")) else None,
                external_order_id=order.external_order_id,
                external_case_id=discussion_id,
                body=msg.get("content") or msg.get("text") or "",
                sent_at=datetime.fromisoformat(msg["creationDate"].replace("Z", "+00:00")) if msg.get("creationDate") else None,
                raw_metadata={"direction": "inbound" if author in ("CUSTOMER", "BUYER") else "outbound"},
            ))
        return results

    def order_url(self, connection: MarketplaceConnection, external_order_id: str) -> Optional[str]:
        """LOW confidence — no documented seller-panel order-permalink
        pattern found for Cdiscount's Octopia-based seller portal; best
        effort guess only."""
        return f"https://seller.cdiscount.com/orders/{external_order_id}"


cdiscount_connector = CdiscountConnector()

__all__ = ["CdiscountConnector", "cdiscount_connector"]
