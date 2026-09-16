"""
Flipkart connector — THIN, EMAIL-FALLBACK-ONLY scope (2026-09-15). No
native messaging API exists (confirmed — Flipkart's official API table of
contents covers only Listing / Order Management / Notification / Report,
no messaging resource). This connector exists solely to capture a real
buyer_email on the order so itsm-service's existing email-fallback
mechanism has something to address — same category as Shopify/Etsy/
Walmart's messaging_capability = NONE.

UNVERIFIED — no live sandbox/credentials exist for this org's Flipkart
seller account. Written directly against real, cited documentation.

Confirmed via direct research (2026-09-15):
- No messaging endpoint anywhere in Flipkart's published API surface.
- Buyer contact number/email is returned ONLY for self-ship orders
  (seller handles their own logistics) — Flipkart-fulfilled orders don't
  expose it. buyer_email below will simply be empty for the (likely
  majority) of Flipkart-fulfilled orders; nothing this connector can do
  about that split.
- OAuth 2.0 — Flipkart supports both client-credentials (for
  self-access/first-party apps) and authorization-code (for third-party
  apps) grants. Implemented here as authorization-code, the more general
  case most integrations (including this one, a third-party ITSM system)
  actually need.
- Flipkart's seller API access is GENERALLY INVITE-ONLY / restricted —
  flagged explicitly per the research brief, since this changes whether
  a connector is even usable regardless of what the API can technically
  do. Not confirmed whether this org has (or can get) that access.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from app.config import get_settings
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

_AUTH_URL = "https://api.flipkart.net/oauth-service/oauth/authorize"
_TOKEN_URL = "https://api.flipkart.net/oauth-service/oauth/token"
_API_BASE = "https://api.flipkart.net/sellers"
_REFRESH_SKEW = timedelta(minutes=5)


class FlipkartConnector(CommerceConnector):
    provider = "flipkart"
    # CONFIRMED none — see module docstring. Same category as Shopify/
    # Etsy/Walmart/BolCom/Zalando: relies entirely on itsm-service's
    # email fallback (and even that only works for self-ship orders).
    messaging_capability = MessagingCapability.NONE

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=15.0)
        return self._client

    def authorize_url(self, state: str) -> str:
        settings = get_settings()
        params = httpx.QueryParams({
            "response_type": "code",
            "client_id": settings.FLIPKART_CLIENT_ID,
            "redirect_uri": settings.FLIPKART_REDIRECT_URI,
            "state": state,
            "scope": "Seller_Api",
        })
        return f"{_AUTH_URL}?{params}"

    async def connect(self, tenant_id: str, credentials: dict) -> ConnectionResult:
        settings = get_settings()
        code = credentials.get("code")
        if not code:
            return ConnectionResult(success=False, error="missing code")

        client = await self._get_client()
        try:
            resp = await client.post(
                _TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": settings.FLIPKART_REDIRECT_URI,
                },
                auth=(settings.FLIPKART_CLIENT_ID, settings.FLIPKART_CLIENT_SECRET),
            )
            payload = resp.json()
        except Exception as exc:
            logger.error("[Flipkart] token exchange failed: %r", exc, exc_info=True)
            return ConnectionResult(success=False, error=str(exc))

        if not payload.get("access_token"):
            return ConnectionResult(success=False, error=payload.get("error_description", "token_exchange_failed"))
        return ConnectionResult(success=True, credentials=payload)

    async def _ensure_fresh_token(self, connection: MarketplaceConnection) -> Optional[dict]:
        creds = connection.credentials
        expires_at_raw = creds.get("access_token_expires_at")
        expires_at = datetime.fromisoformat(expires_at_raw) if expires_at_raw else None
        if expires_at and expires_at - datetime.now(timezone.utc) > _REFRESH_SKEW:
            return creds

        refresh_token = creds.get("refresh_token")
        if not refresh_token:
            logger.warning("[Flipkart] connection %s has no refresh_token — must reconnect", connection.id)
            return None

        settings = get_settings()
        client = await self._get_client()
        try:
            resp = await client.post(
                _TOKEN_URL,
                data={"grant_type": "refresh_token", "refresh_token": decrypt_secret(refresh_token)},
                auth=(settings.FLIPKART_CLIENT_ID, settings.FLIPKART_CLIENT_SECRET),
            )
            payload = resp.json()
        except Exception as exc:
            logger.error("[Flipkart] token refresh failed for connection %s: %r", connection.id, exc, exc_info=True)
            return None

        new_access_token = payload.get("access_token")
        if not new_access_token:
            return None
        creds = dict(creds)
        creds["access_token"] = encrypt_secret(new_access_token)
        if payload.get("refresh_token"):
            creds["refresh_token"] = encrypt_secret(payload["refresh_token"])
        expires_in = payload.get("expires_in")
        if expires_in:
            creds["access_token_expires_at"] = (
                datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
            ).isoformat()
        return creds

    async def fetch_orders(
        self, connection: MarketplaceConnection, since: Optional[datetime] = None
    ) -> list[NormalizedOrder]:
        """Thin scope — minimal order fetch. buyer_email/buyer_name only
        populate for self-ship orders, see module docstring."""
        creds = await self._ensure_fresh_token(connection)
        if not creds:
            return []
        client = await self._get_client()
        try:
            resp = await client.get(
                f"{_API_BASE}/v3/orders/search",
                headers={"Authorization": f"Bearer {decrypt_secret(creds['access_token'])}"},
            )
            if resp.status_code != 200:
                logger.warning("[Flipkart] GET orders/search -> %d: %s", resp.status_code, resp.text[:200])
                return []
            body = resp.json()
        except Exception as exc:
            logger.error("[Flipkart] GET orders/search failed: %r", exc, exc_info=True)
            return []

        results = []
        for order in body.get("orderItems", body.get("orders", [])):
            buyer = order.get("buyer") or order.get("customer") or {}
            total = order.get("totalPrice") or order.get("orderValue")
            results.append(NormalizedOrder(
                external_order_id=str(order.get("orderId") or order.get("orderItemId")),
                status=str(order.get("status") or "new"),
                order_lines=[],  # out of scope — see module docstring
                total_amount=float(total) if total is not None else None,
                currency="INR",
                buyer_email=buyer.get("email"),  # only present for self-ship orders — see module docstring
                buyer_name=buyer.get("name"),
                placed_at=datetime.fromisoformat(order["orderDate"].replace("Z", "+00:00")) if order.get("orderDate") else None,
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
        return SendResult(success=False, error="Flipkart has no buyer-messaging API at all — confirmed platform limitation, not a gap (see module docstring). Use the email fallback instead (self-ship orders only).")

    def order_url(self, connection: MarketplaceConnection, external_order_id: str) -> Optional[str]:
        """LOW-MEDIUM confidence — inferred seller-portal order pattern,
        not confirmed against a documented permalink spec."""
        return f"https://seller.flipkart.com/order-manager/order-detail/{external_order_id}"


flipkart_connector = FlipkartConnector()

__all__ = ["FlipkartConnector", "flipkart_connector"]
