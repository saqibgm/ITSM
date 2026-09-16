"""
Walmart Marketplace API connector — pilot batch #3, per
docs/plans/NATIVE_MARKETPLACE_CONNECTORS_PLAN.md §3/§5.

No reference implementation exists to port — unlike Shopify/Amazon, this
org has no prior Walmart integration anywhere. Everything here is built
directly from Walmart's published API docs (developer.walmart.com), not
verified against a live seller/sandbox account. Treat every endpoint call
below as needing sandbox validation before production use, same category of
risk the Shopify/Amazon builds already lived through and documented.

Auth model is structurally DIFFERENT from Shopify/Amazon and matters for the
routes layer (marketplace_walmart.py): Walmart does NOT use a redirect-based
OAuth consent flow where one itsm-service "app" gets authorized by many
sellers. Per Walmart's own docs, a seller/solution-provider generates a
Client ID + Client Secret directly in the Walmart Developer Portal and
provides them to whatever system needs API access — there's no
"/authorize?state=..." step. So this connector's `connect()` takes the
tenant's own client_id/client_secret directly (submitted via a form, not an
OAuth redirect), and does an OAuth2 client_credentials grant internally
before each API call (tokens last only ~15 minutes — the shortest-lived of
any connector in this pilot batch, refreshed far more aggressively than
Shopify's ~1hr or Amazon's similar window).

fetch_returns() and messaging are both UNRESEARCHED per Phase 0 (plan §2's
table lists Walmart's messaging column as "unclear — needs a direct docs
deep-dive", explicitly flagged as a gap to close before this connector's
scope was finalized — not closed here either, still a stub).

Sandbox/production correction (2026-09-14, caught by direct doc verification
before this was ever live-tested): Walmart's sandbox is a SEPARATE hostname
(sandbox.walmartapis.com), not the same host with different credentials the
way this file originally assumed. Since Walmart's auth model is already
per-tenant (no shared app-level config, see above), `environment` is stored
per-connection in `credentials` alongside client_id/secret, not as a global
setting — a tenant could plausibly hold separate sandbox and production
Walmart credentials, and there's no app-wide config layer here to put it in
even if that weren't true.
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

# Tokens are short-lived (~15 min per Walmart's docs) — refresh with a
# generous skew rather than Shopify/Amazon's 5 min, since a 15-min token
# leaves much less room for request latency to eat into validity.
_REFRESH_SKEW = timedelta(minutes=3)

# Maps Walmart's orderLineStatus onto NormalizedOrder's documented
# 'new'|'acknowledged'|'shipped'|'delivered'|'cancelled' set (base.py) —
# Walmart's own vocabulary happens to overlap almost exactly (unlike
# Shopify/Amazon/eBay, which needed a real translation table), but "Created"
# and "Refund" don't match anything in the canonical set as-is (2026-09-14
# fix) — was just lowercasing the raw value and storing "created"/"refund"
# directly, neither of which the frontend's status filter/badges recognize.
_WALMART_STATUS_MAP = {
    "CREATED": "new",
    "ACKNOWLEDGED": "acknowledged",
    "SHIPPED": "shipped",
    "DELIVERED": "delivered",
    "CANCELLED": "cancelled",
    "REFUND": "cancelled",
}


def _map_walmart_status(raw_order_line_status) -> str:
    return _WALMART_STATUS_MAP.get((raw_order_line_status or "").upper(), "new")


def _base_urls(environment: str) -> tuple[str, str]:
    """Returns (token_url, api_base) — confirmed via direct doc verification
    (2026-09-14) that sandbox is a genuinely separate hostname
    (sandbox.walmartapis.com), not the production host with different
    credentials. Get this wrong and every call 401s regardless of how
    correct the credentials are — same category of mistake eBay's
    RuName-vs-URL confusion or Etsy's assumed-vs-actual client_secret
    already caught elsewhere in this pilot batch."""
    if environment == "sandbox":
        return "https://sandbox.walmartapis.com/v3/token", "https://sandbox.walmartapis.com/v3"
    return "https://marketplace.walmartapis.com/v3/token", "https://marketplace.walmartapis.com/v3"


class WalmartConnector(CommerceConnector):
    provider = "walmart"
    # CONFIRMED none (2026-09-14, direct doc research at developer.walmart.com/
    # us-marketplace/docs/introduction-to-marketplace-apis) — Walmart's full
    # published API category list is Items, Inventory, Orders, Pricing,
    # Promotions, Returns, Refunds, Reporting. No messaging/communication
    # category exists. Same confirmed-platform-limitation status as Shopify
    # and Etsy now, not the "unresearched, needs a deep-dive" flag Phase 0
    # originally left this at.
    messaging_capability = MessagingCapability.NONE

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=15.0)
        return self._client

    async def connect(self, tenant_id: str, credentials: dict) -> ConnectionResult:
        """No OAuth redirect — the tenant supplies their own Walmart-issued
        client_id/client_secret directly (see module docstring), plus which
        environment those credentials belong to ('sandbox' | 'production',
        defaults to sandbox — sandbox and production credentials are NOT
        interchangeable, see _base_urls()). This just validates them by
        attempting a token grant; the caller (routes layer) persists the
        (encrypted) client_id/client_secret + environment on success, same
        as every other connector's connect()."""
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
                data={"grant_type": "client_credentials"},
                auth=(client_id, client_secret),
                headers={"Accept": "application/json", "WM_SVC.NAME": "Walmart Marketplace"},
            )
            if resp.status_code != 200:
                logger.warning("[Walmart] token grant (%s) -> %d: %s", environment, resp.status_code, resp.text[:200])
                return None
            return resp.json().get("access_token")
        except Exception as exc:
            logger.error("[Walmart] token grant failed: %r", exc, exc_info=True)
            return None

    async def _ensure_fresh_token(self, connection: MarketplaceConnection) -> Optional[str]:
        creds = connection.credentials
        expires_at_raw = creds.get("access_token_expires_at")
        expires_at = datetime.fromisoformat(expires_at_raw) if expires_at_raw else None
        cached_token = creds.get("access_token")
        if cached_token and expires_at and expires_at - datetime.now(timezone.utc) > _REFRESH_SKEW:
            return decrypt_secret(cached_token)

        token = await self._get_token(
            decrypt_secret(creds["client_id"]), decrypt_secret(creds["client_secret"]),
            creds.get("environment", "sandbox"),
        )
        if not token:
            return None

        # Caller (the Celery task / route) is responsible for persisting this
        # back onto the connection row — same pattern as Shopify/Amazon's
        # _ensure_fresh_token, kept consistent across connectors even though
        # Walmart's much shorter token lifetime means this runs far more often.
        creds["access_token"] = encrypt_secret(token)
        creds["access_token_expires_at"] = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
        return token

    async def fetch_orders(
        self, connection: MarketplaceConnection, since: Optional[datetime] = None
    ) -> list[NormalizedOrder]:
        token = await self._ensure_fresh_token(connection)
        if not token:
            return []

        created_start = (since or datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        _, api_base = _base_urls(connection.credentials.get("environment", "sandbox"))
        client = await self._get_client()
        try:
            resp = await client.get(
                f"{api_base}/orders",
                headers={"WM_SEC.ACCESS_TOKEN": token, "Accept": "application/json"},
                params={"createdStartDate": created_start},
            )
            if resp.status_code != 200:
                logger.warning("[Walmart] GET /orders -> %d: %s", resp.status_code, resp.text[:200])
                return []
            body = resp.json()
        except Exception as exc:
            logger.error("[Walmart] GET /orders failed: %r", exc, exc_info=True)
            return []

        results = []
        for order in (body.get("list", {}).get("elements", {}).get("order") or []):
            results.append(NormalizedOrder(
                external_order_id=order.get("purchaseOrderId"),
                status=_map_walmart_status(
                    order.get("orderLines", {}).get("orderLine", [{}])[0]
                    .get("orderLineStatuses", {}).get("orderLineStatus", [{}])[0].get("status")
                ),
                order_lines=[
                    {"title": ol.get("item", {}).get("productName"), "quantity": ol.get("orderLineQuantity", {}).get("amount")}
                    for ol in order.get("orderLines", {}).get("orderLine", [])
                ],
                buyer_email=(order.get("shippingInfo") or {}).get("email"),
                buyer_name=((order.get("shippingInfo") or {}).get("postalAddress") or {}).get("name"),
                placed_at=datetime.fromtimestamp(order["orderDate"] / 1000, tz=timezone.utc) if order.get("orderDate") else None,
                raw_metadata=order,
            ))
        return results

    async def fetch_returns(
        self, connection: MarketplaceConnection, since: Optional[datetime] = None
    ) -> list[NormalizedReturn]:
        """Stub — unresearched per Phase 0 (plan §2: 'needs a direct docs
        deep-dive'). Not implemented against a guessed endpoint; returns
        empty until that research actually happens."""
        logger.info("walmart_fetch_returns_not_implemented", extra={"connection_id": str(connection.id)})
        return []

    def parse_webhook(self, raw_payload: bytes, headers: dict) -> None:
        """Walmart does publish webhook notifications per its docs, but the
        exact signature/verification scheme wasn't confirmed in this org's
        research (Phase 0 focused on messaging capability, not the webhook
        signing mechanism specifically). Returns None rather than accepting
        an unverified payload — do not wire a webhook ROUTE for Walmart
        until that scheme is confirmed; fetch_orders() is this connector's
        safe path for now."""
        return None

    async def send_message(self, connection: MarketplaceConnection, order: "MarketplaceOrder", message: str) -> SendResult:
        return SendResult(
            success=False,
            error="Walmart has no buyer-messaging API at all — confirmed hard platform limitation (2026-09-14 doc research), not a gap to close",
        )

    def order_url(self, connection: MarketplaceConnection, external_order_id: str) -> Optional[str]:
        """LOW-MEDIUM confidence — confirmed Seller Center's Order Management
        Dashboard is where a PO's detail view lives (developer.walmart.com
        docs reference sellers clicking a PO link there), but the exact
        deep-link URL/query-param spec for one specific order wasn't
        documented anywhere found (2026-09-14 research). This is a
        best-effort guess at the pattern, not confirmed — needs a real
        click-through once a Walmart connection + order actually exists,
        more so than any other connector's order_url()."""
        return f"https://seller.walmart.com/order-management/orders/{external_order_id}"


walmart_connector = WalmartConnector()

__all__ = ["WalmartConnector", "walmart_connector"]
