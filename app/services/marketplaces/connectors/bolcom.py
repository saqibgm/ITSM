"""
Bol.com connector — THIN, EMAIL-FALLBACK-ONLY scope (2026-09-15). No native
messaging API exists (confirmed via direct research — the Retailer API's
docs cover only orders/shipments/returns/invoices, nothing communication-
related). This connector exists solely to capture a real buyer_email on
the order so itsm-service's existing email-fallback mechanism
(marketplace_sync.py's send_message_to_buyer) has something to address —
same category as Shopify/Etsy/Walmart's messaging_capability = NONE.

UNVERIFIED — no live sandbox/credentials exist for this org's bol.com
account. Written directly against real, cited documentation.

Confirmed via direct research (2026-09-15):
- No messaging endpoint anywhere in the Retailer API (checked ReDoc,
  functional guide, and demo JSON directly).
- Buyer email IS present on the order (shipmentDetails/billingDetails)
  but bol.com's own docs state it "returns no value after 61 days of
  placing the order" (a privacy purge) — the email fallback will simply
  stop working for older orders, not fail loudly; nothing this
  connector can do about that.
- OAuth 2.0 client_credentials grant against
  https://login.bol.com/retailer/auth/token (Basic auth with
  client_id:client_secret) — NOT a redirect/authorize flow, same
  client-credentials shape as this repo's Walmart/Cdiscount connectors.
- API uses vendor-specific media types (Accept/Content-Type:
  application/vnd.retailer.v10+json) rather than plain application/json
  — easy to miss and get a 406/415 without it.
"""

import logging
from datetime import datetime, timedelta, timezone
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

_TOKEN_URL = "https://login.bol.com/retailer/auth/token"
_API_BASE = "https://api.bol.com/retailer"
_MEDIA_TYPE = "application/vnd.retailer.v10+json"
_REFRESH_SKEW = timedelta(minutes=5)


class BolComConnector(CommerceConnector):
    provider = "bolcom"
    # CONFIRMED none — see module docstring. Same category as Shopify/
    # Etsy/Walmart: relies entirely on itsm-service's email fallback.
    messaging_capability = MessagingCapability.NONE

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=15.0)
        return self._client

    async def connect(self, tenant_id: str, credentials: dict) -> ConnectionResult:
        client_id = credentials.get("client_id")
        client_secret = credentials.get("client_secret")
        if not client_id or not client_secret:
            return ConnectionResult(success=False, error="missing client_id or client_secret")

        token = await self._get_token(client_id, client_secret)
        if not token:
            return ConnectionResult(success=False, error="could not obtain access token — check credentials")
        return ConnectionResult(success=True, external_id=client_id)

    async def _get_token(self, client_id: str, client_secret: str) -> Optional[str]:
        client = await self._get_client()
        try:
            resp = await client.post(
                _TOKEN_URL,
                data={"grant_type": "client_credentials"},
                auth=(client_id, client_secret),
                headers={"Accept": "application/json"},
            )
            if resp.status_code != 200:
                logger.warning("[BolCom] token grant -> %d: %s", resp.status_code, resp.text[:200])
                return None
            return resp.json().get("access_token")
        except Exception as exc:
            logger.error("[BolCom] token grant failed: %r", exc, exc_info=True)
            return None

    async def _ensure_fresh_token(self, connection: MarketplaceConnection) -> Optional[str]:
        creds = connection.credentials
        expires_at_raw = creds.get("access_token_expires_at")
        expires_at = datetime.fromisoformat(expires_at_raw) if expires_at_raw else None
        cached_token = creds.get("access_token")
        if cached_token and expires_at and expires_at - datetime.now(timezone.utc) > _REFRESH_SKEW:
            return decrypt_secret(cached_token)

        token = await self._get_token(decrypt_secret(creds["client_id"]), decrypt_secret(creds["client_secret"]))
        if not token:
            return None
        creds["access_token"] = encrypt_secret(token)
        creds["access_token_expires_at"] = (datetime.now(timezone.utc) + timedelta(minutes=59)).isoformat()
        return token

    async def fetch_orders(
        self, connection: MarketplaceConnection, since: Optional[datetime] = None
    ) -> list[NormalizedOrder]:
        """Thin scope — minimal order fetch, just enough to capture
        buyer_email for the email-fallback mechanism (see module docstring
        for the 61-day expiry caveat)."""
        token = await self._ensure_fresh_token(connection)
        if not token:
            return []
        client = await self._get_client()
        try:
            resp = await client.get(
                f"{_API_BASE}/orders",
                headers={"Authorization": f"Bearer {token}", "Accept": _MEDIA_TYPE},
            )
            if resp.status_code != 200:
                logger.warning("[BolCom] GET /orders -> %d: %s", resp.status_code, resp.text[:200])
                return []
            body = resp.json()
        except Exception as exc:
            logger.error("[BolCom] GET /orders failed: %r", exc, exc_info=True)
            return []

        results = []
        for order in body.get("orders", []):
            shipment = order.get("shipmentDetails") or {}
            results.append(NormalizedOrder(
                external_order_id=str(order.get("orderId")),
                status=str(order.get("status") or "new"),
                order_lines=[],  # out of scope — see module docstring
                total_amount=None,  # bol.com's order-list summary doesn't total across order items; out of scope to fetch order-items separately
                currency="EUR",
                buyer_email=shipment.get("email"),
                buyer_name=" ".join(p for p in (shipment.get("firstName"), shipment.get("surname")) if p) or None,
                placed_at=datetime.fromisoformat(order["orderPlacedDateTime"].replace("Z", "+00:00")) if order.get("orderPlacedDateTime") else None,
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
        return SendResult(success=False, error="bol.com has no buyer-messaging API at all — confirmed platform limitation, not a gap (see module docstring). Use the email fallback instead.")

    def order_url(self, connection: MarketplaceConnection, external_order_id: str) -> Optional[str]:
        """LOW-MEDIUM confidence — inferred seller-panel order pattern,
        not confirmed against a documented permalink spec."""
        return f"https://partnerplatform.bol.com/venus/orders/{external_order_id}"


bolcom_connector = BolComConnector()

__all__ = ["BolComConnector", "bolcom_connector"]
