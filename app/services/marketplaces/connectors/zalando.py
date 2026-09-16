"""
Zalando connector — THIN, EMAIL-FALLBACK-ONLY scope (2026-09-15). No native
messaging API exists (confirmed by fetching Zalando's own OpenAPI spec
directly — the API surface is Orders/Shipments/Returns/Articles/Prices
only, nothing communication-related). This connector exists solely to
capture a real buyer_email on the order so itsm-service's existing
email-fallback mechanism has something to address — same category as
Shopify/Etsy/Walmart's messaging_capability = NONE.

UNVERIFIED — no live sandbox/credentials exist for this org's Zalando
account. Written directly against real, cited documentation — though this
one is on firmer ground than most of this batch, since the order schema
facts below came from reading Zalando's actual OpenAPI YAML spec, not a
secondary source.

Confirmed via direct research (2026-09-15), pulled directly from
developers.merchants.zalando.com/docs/openapi/specs/orders.yaml:
- No note/comment/instructions field anywhere in the Order/OrderLine
  schema — confirmed absent, not just unfound.
- customer_email is a real field in the Order schema, present on every
  order (unlike Amazon/eBay's PII-gating). customer_phone.number is
  explicitly noted in the spec as "exposed only with required
  permissions" — email may carry similar gating in practice, not fully
  clear from the spec alone.
- OAuth 2.0 OIDC client_credentials flow. Sandbox token endpoint
  confirmed real: https://api-sandbox.merchants.zalando.com/auth/token.
  Scopes: orders/read, orders/write.
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

_REFRESH_SKEW = timedelta(minutes=5)


def _base_urls(environment: str) -> tuple[str, str]:
    """Returns (token_url, api_base) — confirmed sandbox token endpoint
    from Zalando's own docs; production host inferred by dropping
    '-sandbox' per the same convention, not independently confirmed."""
    if environment == "sandbox":
        return "https://api-sandbox.merchants.zalando.com/auth/token", "https://api-sandbox.merchants.zalando.com"
    return "https://api.merchants.zalando.com/auth/token", "https://api.merchants.zalando.com"


class ZalandoConnector(CommerceConnector):
    provider = "zalando"
    # CONFIRMED none — see module docstring. Same category as Shopify/
    # Etsy/Walmart/BolCom: relies entirely on itsm-service's email fallback.
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
        environment = credentials.get("environment", "sandbox")
        if not client_id or not client_secret:
            return ConnectionResult(success=False, error="missing client_id or client_secret")

        token = await self._get_token(client_id, client_secret, environment)
        if not token:
            return ConnectionResult(success=False, error="could not obtain access token — check credentials and environment")
        return ConnectionResult(success=True, external_id=client_id)

    async def _get_token(self, client_id: str, client_secret: str, environment: str) -> Optional[str]:
        token_url, _ = _base_urls(environment)
        client = await self._get_client()
        try:
            resp = await client.post(
                token_url,
                data={"grant_type": "client_credentials", "scope": "orders/read"},
                auth=(client_id, client_secret),
            )
            if resp.status_code != 200:
                logger.warning("[Zalando] token grant (%s) -> %d: %s", environment, resp.status_code, resp.text[:200])
                return None
            return resp.json().get("access_token")
        except Exception as exc:
            logger.error("[Zalando] token grant failed: %r", exc, exc_info=True)
            return None

    async def _ensure_fresh_token(self, connection: MarketplaceConnection) -> Optional[str]:
        creds = connection.credentials
        expires_at_raw = creds.get("access_token_expires_at")
        expires_at = datetime.fromisoformat(expires_at_raw) if expires_at_raw else None
        cached_token = creds.get("access_token")
        if cached_token and expires_at and expires_at - datetime.now(timezone.utc) > _REFRESH_SKEW:
            return decrypt_secret(cached_token)

        token = await self._get_token(
            decrypt_secret(creds["client_id"]), decrypt_secret(creds["client_secret"]), creds.get("environment", "sandbox"),
        )
        if not token:
            return None
        creds["access_token"] = encrypt_secret(token)
        creds["access_token_expires_at"] = (datetime.now(timezone.utc) + timedelta(minutes=55)).isoformat()
        return token

    async def fetch_orders(
        self, connection: MarketplaceConnection, since: Optional[datetime] = None
    ) -> list[NormalizedOrder]:
        """Thin scope — minimal order fetch, just enough to capture the
        real customer_email confirmed in Zalando's own OpenAPI spec."""
        token = await self._ensure_fresh_token(connection)
        if not token:
            return []
        settings_environment = connection.credentials.get("environment", "sandbox")
        _, api_base = _base_urls(settings_environment)
        client = await self._get_client()
        try:
            resp = await client.get(
                f"{api_base}/orders",
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code != 200:
                logger.warning("[Zalando] GET /orders -> %d: %s", resp.status_code, resp.text[:200])
                return []
            body = resp.json()
        except Exception as exc:
            logger.error("[Zalando] GET /orders failed: %r", exc, exc_info=True)
            return []

        results = []
        for order in body.get("orders", body if isinstance(body, list) else []):
            total = order.get("total_amount") or order.get("grand_total")
            results.append(NormalizedOrder(
                external_order_id=str(order.get("order_number") or order.get("id")),
                status=str(order.get("status") or "new"),
                order_lines=[],  # out of scope — see module docstring
                total_amount=float(total) if total is not None else None,
                currency=order.get("currency"),
                buyer_email=order.get("customer_email"),
                buyer_name=order.get("customer_name"),
                placed_at=datetime.fromisoformat(order["created_at"].replace("Z", "+00:00")) if order.get("created_at") else None,
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
        return SendResult(success=False, error="Zalando has no buyer-messaging API at all — confirmed via their own OpenAPI spec, not a gap (see module docstring). Use the email fallback instead.")

    def order_url(self, connection: MarketplaceConnection, external_order_id: str) -> Optional[str]:
        """LOW-MEDIUM confidence — inferred partner-portal order pattern,
        not confirmed against a documented permalink spec."""
        return f"https://partner.zalando.com/orders/{external_order_id}"


zalando_connector = ZalandoConnector()

__all__ = ["ZalandoConnector", "zalando_connector"]
