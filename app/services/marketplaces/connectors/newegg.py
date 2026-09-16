"""
Newegg connector — THIN, EMAIL-FALLBACK-ONLY scope (2026-09-16). A real
buyer-messaging feature exists ("Send Message to Customer" in the Seller
Portal UI, order-triggered, supports attachments) but is confirmed NEVER
exposed via the API — Newegg's full published API category list (Item,
Order, Shipping Label, DataFeed, RMA, Reports, Seller, SBN, Service
Status) has no messaging category, and the only "Message" field found
anywhere is generic API error/status text, not a messaging endpoint. This
connector exists solely to capture the masked buyer email Newegg DOES
return on every order, so itsm-service's existing email-fallback
mechanism has something to address — same category as Shopify/Etsy/
Walmart's messaging_capability = NONE.

UNVERIFIED — no live seller account/credentials exist for this org's
Newegg account. Written directly against real, cited documentation
(developer.newegg.com's actual pages, reachable and fetchable — unlike
most marketplaces in this build).

Confirmed via direct research (2026-09-15/16):
- No messaging endpoint anywhere in Newegg's API — real buyer messaging
  only exists as a manual Seller Portal UI action, not automatable via
  this connector.
- CustomerEmailAddress IS returned on every order via Get Order
  Information, but it's a masked relay address (e.g.
  gdv6l0viwo4l7j1d@marketplace.newegg.com), documented as "the masked
  customer email address, you can reach the customer through this email
  address" — a legitimate relay, same pattern as Amazon's
  BuyerInfo.BuyerEmail, expected to actually work as an email fallback
  (unlike, say, bol.com's which expires after 61 days).
- A `Memo` field exists but is SELLER-authored, not a buyer checkout
  note — not populated as buyer_note here.
- Auth: static credential headers, NOT request signing —
  `Authorization: {api_key}` + `SecretKey: {secret_key}` headers, plus
  seller_id in the URL. Simplest auth of any connector in this build
  alongside Best Buy's single API key.
- Get Order Information is oddly a PUT (not GET) per Newegg's own docs
  — confirmed, not a typo carried over from elsewhere.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

from app.models.marketplace import MarketplaceConnection
from app.services.marketplaces.connectors.base import (
    CommerceConnector,
    ConnectionResult,
    MessagingCapability,
    NormalizedOrder,
    NormalizedReturn,
    SendResult,
)
from app.services.marketplaces.crypto import decrypt_secret, encrypt_secret

logger = logging.getLogger(__name__)

_API_BASE = "https://api.newegg.com/marketplace"

_STATUS_MAP = {
    "0": "new",           # Unshipped
    "1": "acknowledged",  # Partially Shipped
    "2": "shipped",       # Shipped
}


class NeweggConnector(CommerceConnector):
    provider = "newegg"
    # CONFIRMED none — see module docstring. Same category as Shopify/
    # Etsy/Walmart: relies entirely on itsm-service's email fallback
    # (this one via a masked-but-real relay address).
    messaging_capability = MessagingCapability.NONE

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=15.0)
        return self._client

    async def connect(self, tenant_id: str, credentials: dict) -> ConnectionResult:
        seller_id = credentials.get("seller_id")
        api_key = credentials.get("api_key")
        secret_key = credentials.get("secret_key")
        if not seller_id or not api_key or not secret_key:
            return ConnectionResult(success=False, error="missing seller_id, api_key, or secret_key")

        client = await self._get_client()
        try:
            resp = await client.put(
                f"{_API_BASE}/ordermgmt/order/orderinfo",
                headers={"Authorization": api_key, "SecretKey": secret_key},
                params={"sellerid": seller_id, "version": "1.0"},
                json={},
            )
            if resp.status_code not in (200, 400):  # 400 with no real order filter is still a valid-credentials signal; only a 401/403 means bad creds
                return ConnectionResult(success=False, error=f"could not validate credentials (status {resp.status_code})")
        except Exception as exc:
            logger.error("[Newegg] connect validation failed: %r", exc, exc_info=True)
            return ConnectionResult(success=False, error=str(exc))
        return ConnectionResult(success=True, external_id=seller_id)

    async def fetch_orders(
        self, connection: MarketplaceConnection, since: Optional[datetime] = None
    ) -> list[NormalizedOrder]:
        """Thin scope — minimal order fetch, just enough to capture the
        masked-but-real buyer email for the email fallback."""
        creds = connection.credentials
        client = await self._get_client()
        try:
            resp = await client.put(
                f"{_API_BASE}/ordermgmt/order/orderinfo",
                headers={"Authorization": decrypt_secret(creds["api_key"]), "SecretKey": decrypt_secret(creds["secret_key"])},
                params={"sellerid": connection.external_id, "version": "1.0"},
                json={"OrderStatus": "0,1,2"},
            )
            if resp.status_code != 200:
                logger.warning("[Newegg] PUT orderinfo -> %d: %s", resp.status_code, resp.text[:200])
                return []
            body = resp.json()
        except Exception as exc:
            logger.error("[Newegg] PUT orderinfo failed: %r", exc, exc_info=True)
            return []

        results = []
        for order in body.get("OrderInfoList", body.get("Orders", [])):
            total = order.get("OrderTotalAmount")
            results.append(NormalizedOrder(
                external_order_id=str(order.get("OrderNumber")),
                status=_STATUS_MAP.get(str(order.get("OrderStatus")), "new"),
                order_lines=[],  # out of scope — see module docstring
                total_amount=float(total) if total is not None else None,
                currency="USD",
                buyer_email=order.get("CustomerEmailAddress"),  # masked relay address, see module docstring
                buyer_name=order.get("CustomerName"),
                placed_at=datetime.fromisoformat(order["OrderDate"].replace("Z", "+00:00")) if order.get("OrderDate") else None,
                raw_metadata=order,
            ))
        return results

    async def fetch_returns(
        self, connection: MarketplaceConnection, since: Optional[datetime] = None
    ) -> list[NormalizedReturn]:
        """Out of scope — thin connector, see module docstring."""
        return []

    def parse_webhook(self, raw_payload: bytes, headers: dict) -> None:
        return None

    async def send_message(self, connection: MarketplaceConnection, order, message: str) -> SendResult:
        return SendResult(success=False, error="Newegg's buyer-messaging feature exists only in the Seller Portal UI, never exposed via API (confirmed — see module docstring). Use the email fallback instead.")

    def order_url(self, connection: MarketplaceConnection, external_order_id: str) -> Optional[str]:
        """LOW-MEDIUM confidence — inferred seller-portal order pattern,
        not confirmed against a documented permalink spec."""
        return f"https://sellerportal.newegg.com/order/detail?orderNumber={external_order_id}"


newegg_connector = NeweggConnector()

__all__ = ["NeweggConnector", "newegg_connector"]
