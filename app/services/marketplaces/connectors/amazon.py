"""
Amazon SP-API connector — pilot batch #2, per
docs/plans/NATIVE_MARKETPLACE_CONNECTORS_PLAN.md §3/§5.

Ports the LWA OAuth + order-lookup shape from Project-IQ-V2's chatbot repo
(extensions/amazon/service.py, routes.py). Two things are NOT ported because
they were never actually built there either — not a gap introduced by this
port, an existing, already-documented one:

1. Returns — the chatbot repo's own AMAZON_INTEGRATION_PLAN.md §1/§6 flags
   write actions (which is where returns/refund data would come from, via
   the Feeds API) as blocked on the Solution Provider Portal's Feeds
   role/permission grant, never resolved as of that repo's last update.
   fetch_returns() below is a stub returning an empty list with that same
   blocker noted, not a fabricated implementation.
2. Inbound webhook — Amazon has no HTTP webhook mechanism at all. Its
   Notifications API delivers via AWS SQS, a fundamentally different
   consumption model (a queue poller/consumer, not a webhook route) that
   the chatbot repo also never built (same doc, same section). parse_webhook()
   below always returns None; the "auto" path for Amazon isn't available
   until an SQS consumer is built as a separate mechanism — a real future
   phase, not something to fake here.

messaging_capability = OUTBOUND_ONLY (confirmed Phase 0 finding: SP-API's
Messaging API can send a templated message to a buyer but has no endpoint to
read what a buyer sent). send_message() is written against the documented
API shape (getMessagingActionsForOrder → the specific send action) but has
NO reference implementation to port — the chatbot repo never built this
either. Flagged as unverified rather than presented with false confidence;
needs sandbox validation before relying on it, same as everything else in
this file that touches a live endpoint for the first time.

Never log access_token/refresh_token values.
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

_REFRESH_SKEW = timedelta(minutes=5)
_SANDBOX_BASE_URL = "https://sandbox.sellingpartnerapi-na.amazon.com"
_PRODUCTION_BASE_URL = "https://sellingpartnerapi-na.amazon.com"
_LWA_TOKEN_URL = "https://api.amazon.com/auth/o2/token"


class AmazonConnector(CommerceConnector):
    provider = "amazon"
    messaging_capability = MessagingCapability.OUTBOUND_ONLY

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None

    def _base_url(self) -> str:
        settings = get_settings()
        return _SANDBOX_BASE_URL if settings.AMAZON_ENVIRONMENT == "sandbox" else _PRODUCTION_BASE_URL

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=15.0)
        return self._client

    # ------------------------------------------------------------------
    # OAuth
    # ------------------------------------------------------------------

    def authorize_url(self, state: str) -> str:
        """`version=beta` is required while the app is Draft/Sandbox — drop
        it once the Public Developer application is approved and live
        (ported note from the chatbot repo's amazon_connect())."""
        settings = get_settings()
        params = {"application_id": settings.AMAZON_APP_ID, "state": state}
        if settings.AMAZON_ENVIRONMENT == "sandbox":
            params["version"] = "beta"
        query = httpx.QueryParams(params)
        return f"https://sellercentral.amazon.com/apps/authorize/consent?{query}"

    async def connect(self, tenant_id: str, credentials: dict) -> ConnectionResult:
        """Exchange a `spapi_oauth_code` (distinct param name from Shopify's
        plain `code` — Amazon's own OAuth variant) for LWA tokens. The
        spapi_oauth_code expires 5 minutes after issuance — exchange
        immediately, same as the ported route does."""
        settings = get_settings()
        code = credentials.get("spapi_oauth_code")
        seller_id = credentials.get("selling_partner_id")
        if not code:
            return ConnectionResult(success=False, error="missing spapi_oauth_code")

        client = await self._get_client()
        try:
            resp = await client.post(
                _LWA_TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "client_id": settings.AMAZON_CLIENT_ID,
                    "client_secret": settings.AMAZON_CLIENT_SECRET,
                },
            )
            payload = resp.json()
        except Exception as exc:
            logger.error("[Amazon] token exchange failed for seller %s: %r", seller_id, exc, exc_info=True)
            return ConnectionResult(success=False, error=str(exc))

        if not payload.get("access_token") or not payload.get("refresh_token"):
            return ConnectionResult(success=False, error=payload.get("error", "token_exchange_failed"))

        return ConnectionResult(success=True, external_id=seller_id)

    async def _ensure_fresh_token(self, connection: MarketplaceConnection) -> Optional[dict]:
        """Unlike Shopify, Amazon's refresh_token does NOT rotate — only the
        access_token needs persisting after a refresh (ported behavior from
        AmazonService._refresh_token/_ensure_fresh_token)."""
        creds = connection.credentials
        expires_at_raw = creds.get("access_token_expires_at")
        expires_at = datetime.fromisoformat(expires_at_raw) if expires_at_raw else None
        if expires_at and expires_at - datetime.now(timezone.utc) > _REFRESH_SKEW:
            return creds

        settings = get_settings()
        client = await self._get_client()
        try:
            resp = await client.post(
                _LWA_TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": decrypt_secret(creds["refresh_token"]),
                    "client_id": settings.AMAZON_CLIENT_ID,
                    "client_secret": settings.AMAZON_CLIENT_SECRET,
                },
            )
            payload = resp.json()
        except Exception as exc:
            logger.error("[Amazon] token refresh failed for connection %s: %r", connection.id, exc, exc_info=True)
            return None

        new_access_token = payload.get("access_token")
        if not new_access_token:
            return None

        creds = dict(creds)
        creds["access_token"] = encrypt_secret(new_access_token)
        expires_in = payload.get("expires_in")
        if expires_in:
            creds["access_token_expires_at"] = (
                datetime.now(timezone.utc) + timedelta(seconds=expires_in)
            ).isoformat()
        return creds

    async def _get(self, connection: MarketplaceConnection, path: str, params: Optional[dict] = None) -> Optional[dict]:
        creds = await self._ensure_fresh_token(connection)
        if not creds:
            return None
        client = await self._get_client()
        try:
            resp = await client.get(
                f"{self._base_url()}{path}",
                headers={"x-amz-access-token": decrypt_secret(creds["access_token"])},
                params=params or {},
            )
            body = resp.json()
            if resp.status_code != 200:
                logger.warning("[Amazon] GET %s -> %d: %s", path, resp.status_code, str(body)[:300])
                return None
            return body
        except Exception as exc:
            logger.error("[Amazon] GET %s failed: %r", path, exc, exc_info=True)
            return None

    # ------------------------------------------------------------------
    # Orders (manual/backfill path — no inbound webhook exists for Amazon)
    # ------------------------------------------------------------------

    async def fetch_orders(
        self, connection: MarketplaceConnection, since: Optional[datetime] = None
    ) -> list[NormalizedOrder]:
        """getOrders (list), not the ported find_order()'s single-ID lookup —
        the CommerceConnector interface needs a backfill-capable list call.

        Date format — two real, confirmed-live findings (2026-09-14), not
        from docs alone:
        1. Production SP-API wants strict 'YYYY-MM-DDTHH:MM:SSZ' for
           CreatedAfter — Python's default .isoformat() produces
           '...+00:00' with microseconds, which SP-API's AmazonDateTime
           schema rejects ('Could not match input arguments', a 400 with no
           more specific detail). Fixed by formatting explicitly.
        2. The SANDBOX environment is a *static mock* system that doesn't
           accept real dates at all for CreatedAfter, despite Amazon's own
           docs saying "ISO 8601 format" — it requires the literal sentinel
           string 'TEST_CASE_200' to return canned mock data (confirmed
           against multiple independent reports of the exact same 400 this
           connector hit before this fix — this isn't a one-off account
           quirk, it's how the sandbox is built). Sending a real date to
           sandbox, or the sentinel to production, both fail the same way.
        Branches on the global AMAZON_ENVIRONMENT setting, same as
        _base_url() above — Amazon's environment isn't tracked per-connection
        anywhere in this connector (unlike Walmart, which genuinely needs
        per-tenant environment since its credentials are per-tenant too;
        Amazon's app-level config is one setting for the whole deployment)."""
        settings = get_settings()
        if settings.AMAZON_ENVIRONMENT == "sandbox":
            created_after = "TEST_CASE_200"
        else:
            created_after_dt = since or datetime.now(timezone.utc) - timedelta(days=30)
            created_after = created_after_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        body = await self._get(connection, "/orders/v0/orders", {
            "MarketplaceIds": settings.AMAZON_MARKETPLACE_IDS,
            "CreatedAfter": created_after,
        })
        if not body:
            return []
        orders = (body.get("payload") or {}).get("Orders") or []
        results = []
        for order in orders:
            total = order.get("OrderTotal") or {}
            results.append(NormalizedOrder(
                external_order_id=order.get("AmazonOrderId"),
                status=(order.get("OrderStatus") or "new").lower(),
                order_lines=[],  # getOrderItems is a separate call — not fetched here to avoid N+1; add if a mapping needs line-item detail
                total_amount=float(total["Amount"]) if total.get("Amount") else None,
                currency=total.get("CurrencyCode"),
                placed_at=datetime.fromisoformat(order["PurchaseDate"]) if order.get("PurchaseDate") else None,
                raw_metadata=order,
            ))
        return results

    async def fetch_returns(
        self, connection: MarketplaceConnection, since: Optional[datetime] = None
    ) -> list[NormalizedReturn]:
        """Stub — returns data requires either the Feeds API (write-side,
        blocked on a Solution Provider Portal permission grant that was never
        resolved in the chatbot repo either — see this file's module
        docstring) or a separate Reports API report type
        (GET_FBA_FULFILLMENT_CUSTOMER_RETURNS_DATA) not yet investigated for
        this connector. Returns empty rather than fabricating a call against
        an unconfirmed endpoint."""
        logger.info(
            "amazon_fetch_returns_not_implemented",
            extra={"connection_id": str(connection.id), "reason": "blocked on Feeds API permission grant, unresolved upstream"},
        )
        return []

    def parse_webhook(self, raw_payload: bytes, headers: dict) -> None:
        """Amazon has no inbound HTTP webhook mechanism — Notifications API
        delivers via AWS SQS, a queue-consumer model, not a webhook route.
        Always returns None; this connector's only sync path today is
        fetch_orders() (manual/backfill)."""
        return None

    # ------------------------------------------------------------------
    # Messaging — outbound only (Phase 0 finding). UNVERIFIED: no reference
    # implementation exists to port (the chatbot repo never built this
    # either); shape follows SP-API's documented Messaging API but needs
    # sandbox validation before being trusted.
    # ------------------------------------------------------------------

    async def send_message(self, connection: MarketplaceConnection, order_or_case_id: str, message: str) -> SendResult:
        """Per SP-API's documented (not yet sandbox-validated here) flow:
        call getMessagingActionsForOrder to discover which message action
        types are currently available for this order, then POST to whichever
        one applies. Real seller/buyer messages require a specific template
        action (e.g. AmazonMotors, confirmCustomizationDetails,
        legalDisclosure) — Amazon does NOT offer a generic free-text send;
        this simplification (treating `message` as if it maps to a generic
        action) will need real work once tested against a sandbox order that
        actually has message actions available."""
        actions_body = await self._get(
            connection, f"/messaging/v1/orders/{order_or_case_id}/messages"
        )
        if not actions_body:
            return SendResult(success=False, error="could not fetch available messaging actions for this order")

        # TODO: no sandbox order has been tested with real messagingActions
        # available yet — this is written against SP-API's documented shape,
        # not confirmed live. Do not treat as working until validated.
        return SendResult(success=False, error="Amazon send_message is unverified — needs sandbox validation before use, see module docstring")


amazon_connector = AmazonConnector()

__all__ = ["AmazonConnector", "amazon_connector"]
