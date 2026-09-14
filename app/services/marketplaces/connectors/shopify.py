"""
Shopify connector — first of the §5 pilot batch (Amazon, Shopify, Walmart,
eBay, Etsy), per docs/plans/NATIVE_MARKETPLACE_CONNECTORS_PLAN.md §3's reuse
strategy: the OAuth/GraphQL-client shape here is ported from the chatbot
repo's battle-tested `extensions/shopify/service.py` and `routes.py`
(Project-IQ-V2, built for a different purpose — customer-support order
lookup/refund/cancel via Rasa) rather than built fresh, since that code
already survived the "docs vs. live-testing reality" discovery pass
documented in that repo's SHOPIFY_INTEGRATION_PLAN.md §6a/§6b.

Adapted, not copied verbatim — itsm-service is FastAPI/async-SQLAlchemy, not
Flask/psycopg2, so token storage goes through MarketplaceConnection (this
repo's model) instead of a bespoke shopify_connections table, and HTTP goes
through httpx (already a dependency here) instead of aiohttp. The GraphQL
business-logic knowledge (cost-based partial-success handling, expiring-token
refresh with a safety skew) is preserved as-is — that's the part worth reusing.

ITSM scope differs from the chatbot's: this connector needs order sync +
something return-like + messaging, NOT the chat-bot's refund/cancel/
address-update WRITE actions (those stay in Project-IQ-V2 as customer-support
actions). Shopify has no distinct "return" object — refunds/cancellations are
the closest signal, so fetch_returns() is a best-effort mapping onto those,
flagged clearly rather than presented as a clean 1:1 return-case API.

messaging_capability = NONE — confirmed in Phase 0 research (plan §2):
Shopify has no order-tied buyer-messaging concept at all, not a gap to close.
"""

import base64
import hashlib
import hmac
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
    NormalizedMessage,
    NormalizedOrder,
    NormalizedReturn,
    SendResult,
)
from app.services.marketplaces.crypto import decrypt_secret, encrypt_secret

logger = logging.getLogger(__name__)

_REFRESH_SKEW = timedelta(minutes=5)

# Maps Shopify's own fulfillment-status vocabulary onto NormalizedOrder's
# documented 'new'|'acknowledged'|'shipped'|'delivered'|'cancelled' set
# (base.py) — a real gap found via live testing (2026-09-14): fetch_orders()
# and normalize_event() were both just lowercasing the raw Shopify value and
# storing it as-is ("unfulfilled", "fulfilled", ...), which matches NONE of
# the 5 canonical values the rest of the system (frontend status filter,
# badge colors) actually expects. Covers both spellings Shopify uses for the
# same concept — GraphQL's UPPER_SNAKE_CASE enum (displayFulfillmentStatus,
# used by fetch_orders) and REST webhooks' distinct lowercase strings
# (fulfillment_status, used by normalize_event) — by uppercasing whatever
# comes in before lookup. Shopify has no order-level 'delivered' signal
# without a separate tracking-events lookup, so nothing maps to it here;
# cancellation is a separate cancelled_at/cancelledAt field, not a
# fulfillment-status value, so callers pass that in separately.
_SHOPIFY_STATUS_MAP = {
    "FULFILLED": "shipped",
    "IN_PROGRESS": "acknowledged",
    "PARTIALLY_FULFILLED": "acknowledged",
    "PARTIAL": "acknowledged",  # REST webhook spelling of the same state
    "RESTOCKED": "cancelled",
    "UNFULFILLED": "new",
    "PENDING_FULFILLMENT": "new",
    "OPEN": "new",
    "ON_HOLD": "new",
    "SCHEDULED": "new",
}


def _map_shopify_status(raw_fulfillment_status: Optional[str], cancelled: bool) -> str:
    if cancelled:
        return "cancelled"
    return _SHOPIFY_STATUS_MAP.get((raw_fulfillment_status or "").upper(), "new")


def _customer_name(customer: Optional[dict]) -> Optional[str]:
    """Joins customer.firstName/lastName into a display name — None if
    neither is present rather than an empty/whitespace-only string."""
    if not customer:
        return None
    parts = [p for p in (customer.get("firstName"), customer.get("lastName")) if p]
    return " ".join(parts) or None


class ShopifyConnector(CommerceConnector):
    provider = "shopify"
    messaging_capability = MessagingCapability.NONE

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=15.0)
        return self._client

    # ------------------------------------------------------------------
    # OAuth — authorize URL + code exchange (routes call these; the actual
    # HTTP redirect/callback endpoints live in app/api/v1/marketplace_shopify.py,
    # not here — this class is the connector, not the web layer)
    # ------------------------------------------------------------------

    def authorize_url(self, shop_domain: str, state: str) -> str:
        settings = get_settings()
        params = httpx.QueryParams({
            "client_id": settings.SHOPIFY_CLIENT_ID,
            "scope": settings.SHOPIFY_SCOPES,
            "redirect_uri": settings.SHOPIFY_REDIRECT_URI,
            "state": state,
        })
        return f"https://{shop_domain}/admin/oauth/authorize?{params}"

    @staticmethod
    def verify_oauth_hmac(query_params: dict, client_secret: str) -> bool:
        """Shopify's OAuth-callback HMAC: sort remaining query params, join,
        HMAC-SHA256 hex digest — distinct scheme from the webhook HMAC below
        (that one's base64 over the raw body). Ported from bp_shopify's
        _verify_shopify_hmac."""
        provided = query_params.get("hmac", "")
        if not provided:
            return False
        pairs = sorted((k, v) for k, v in query_params.items() if k not in ("hmac", "signature"))
        message = "&".join(f"{k}={v}" for k, v in pairs)
        digest = hmac.new(client_secret.encode(), message.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(digest, provided)

    async def connect(self, tenant_id: str, credentials: dict) -> ConnectionResult:
        """Exchange an OAuth `code` for an expiring offline access token.

        credentials = {"shop_domain": ..., "code": ...} — the FastAPI callback
        route is responsible for HMAC/state verification before calling this;
        this method only does the token exchange + persists via the caller
        (it returns the token payload's essentials, the route layer upserts
        the MarketplaceConnection row — kept out of this method so `connect`
        stays testable without a DB session).
        """
        settings = get_settings()
        shop_domain = credentials.get("shop_domain")
        code = credentials.get("code")
        if not shop_domain or not code:
            return ConnectionResult(success=False, error="missing shop_domain or code")

        client = await self._get_client()
        try:
            resp = await client.post(
                f"https://{shop_domain}/admin/oauth/access_token",
                json={
                    "client_id": settings.SHOPIFY_CLIENT_ID,
                    "client_secret": settings.SHOPIFY_CLIENT_SECRET,
                    "code": code,
                    # Required — Shopify rejects non-expiring tokens outright.
                    # Confirmed live in the chatbot repo's build
                    # (SHOPIFY_INTEGRATION_PLAN.md §6a finding 1); omitting
                    # this silently returns the old non-expiring token shape.
                    "expiring": 1,
                },
            )
            payload = resp.json()
        except Exception as exc:
            logger.error("[Shopify] token exchange failed for shop %s: %r", shop_domain, exc, exc_info=True)
            return ConnectionResult(success=False, error=str(exc))

        access_token = payload.get("access_token")
        if not access_token:
            return ConnectionResult(success=False, error=payload.get("error", "token_exchange_failed"))

        # Hand the raw payload back rather than making the route layer
        # re-exchange `code` a second time — Shopify's authorization code is
        # single-use, so a second POST to /admin/oauth/access_token with the
        # same code 400s (was the actual cause of the "[object Object]"-
        # adjacent 500 the route used to raise, 2026-09-14).
        return ConnectionResult(success=True, external_id=shop_domain, credentials=payload)

    # ------------------------------------------------------------------
    # Token refresh — ported from ShopifyService._refresh_token/_ensure_fresh_token
    # ------------------------------------------------------------------

    async def _ensure_fresh_token(self, connection: MarketplaceConnection) -> Optional[dict]:
        """Returns the connection's credentials dict, refreshed first if the
        access token is expired or expiring within _REFRESH_SKEW. Caller is
        responsible for persisting the refreshed credentials back onto
        `connection` and committing — this method doesn't touch the DB
        session directly, to keep it usable from both the webhook task and
        the manual-sync endpoint without assuming a particular session shape.
        """
        creds = connection.credentials
        expires_at_raw = creds.get("access_token_expires_at")
        expires_at = datetime.fromisoformat(expires_at_raw) if expires_at_raw else None
        if expires_at and expires_at - datetime.now(timezone.utc) > _REFRESH_SKEW:
            return creds

        refresh_token = creds.get("refresh_token")
        if not refresh_token:
            logger.warning("[Shopify] connection %s has no refresh_token — must reconnect", connection.id)
            return None

        settings = get_settings()
        client = await self._get_client()
        try:
            resp = await client.post(
                f"https://{creds['shop_domain']}/admin/oauth/access_token",
                json={
                    "client_id": settings.SHOPIFY_CLIENT_ID,
                    "client_secret": settings.SHOPIFY_CLIENT_SECRET,
                    "grant_type": "refresh_token",
                    "refresh_token": decrypt_secret(refresh_token),
                },
            )
            payload = resp.json()
        except Exception as exc:
            logger.error("[Shopify] token refresh failed for connection %s: %r", connection.id, exc, exc_info=True)
            return None

        new_access_token = payload.get("access_token")
        if not new_access_token:
            return None

        # Shopify invalidates BOTH the old access_token and refresh_token on
        # every refresh — both must be persisted together or the connection
        # becomes unrecoverable (ported verbatim from the chatbot repo's
        # update_tokens() docstring warning).
        creds = dict(creds)
        creds["access_token"] = encrypt_secret(new_access_token)
        if payload.get("refresh_token"):
            creds["refresh_token"] = encrypt_secret(payload["refresh_token"])
        expires_in = payload.get("expires_in")
        if expires_in:
            creds["access_token_expires_at"] = (
                datetime.now(timezone.utc) + timedelta(seconds=expires_in)
            ).isoformat()
        return creds

    # ------------------------------------------------------------------
    # GraphQL client — ported from ShopifyService._graphql
    # ------------------------------------------------------------------

    async def _graphql(
        self, connection: MarketplaceConnection, query: str, variables: Optional[dict] = None
    ) -> Optional[dict]:
        """Returns `data` even alongside GraphQL-level `errors` — partial
        success is a real response shape (one field can error, e.g. a
        Protected Customer Data field not yet approved, while a mutation's
        own success/userErrors resolves fine elsewhere in the same response).
        Only returns None when there's genuinely no `data`: no connection,
        refresh failure, HTTP failure, or a 429. See ShopifyService._graphql's
        original docstring (Project-IQ-V2) for the full reasoning — preserved
        here, not rediscovered.
        """
        creds = await self._ensure_fresh_token(connection)
        if not creds:
            return None

        settings = get_settings()
        url = f"https://{creds['shop_domain']}/admin/api/{settings.SHOPIFY_API_VERSION}/graphql.json"
        headers = {
            "X-Shopify-Access-Token": decrypt_secret(creds["access_token"]),
            "Content-Type": "application/json",
        }
        client = await self._get_client()
        try:
            resp = await client.post(url, json={"query": query, "variables": variables or {}}, headers=headers)
            if resp.status_code == 429:
                logger.warning("[Shopify] rate-limited (429) for connection %s", connection.id)
                return None
            if resp.status_code != 200:
                logger.warning("[Shopify] %s -> %d: %s", url, resp.status_code, resp.text[:200])
                return None
            payload = resp.json()
            if payload.get("errors"):
                logger.warning("[Shopify] GraphQL errors for connection %s: %s", connection.id, payload["errors"])
            return payload.get("data")
        except Exception as exc:
            logger.error("[Shopify] request to %s failed: %r", url, exc, exc_info=True)
            return None

    # ------------------------------------------------------------------
    # Orders / Returns (manual/backfill path)
    # ------------------------------------------------------------------

    async def fetch_orders(
        self, connection: MarketplaceConnection, since: Optional[datetime] = None
    ) -> list[NormalizedOrder]:
        query_filter = f"updated_at:>={since.isoformat()}" if since else ""
        gql = """
        query RecentOrders($searchQuery: String!) {
          orders(first: 50, query: $searchQuery, sortKey: UPDATED_AT) {
            edges { node {
              id name email
              customer { email firstName lastName }
              displayFulfillmentStatus displayFinancialStatus cancelledAt
              createdAt
              totalPriceSet { shopMoney { amount currencyCode } }
              lineItems(first: 20) { edges { node { title quantity } } }
            } }
          }
        }
        """
        data = await self._graphql(connection, gql, {"searchQuery": query_filter})
        if not data:
            return []
        results = []
        for edge in (data.get("orders") or {}).get("edges") or []:
            node = edge["node"]
            money = (node.get("totalPriceSet") or {}).get("shopMoney") or {}
            results.append(NormalizedOrder(
                external_order_id=node["id"],
                status=_map_shopify_status(node.get("displayFulfillmentStatus"), bool(node.get("cancelledAt"))),
                order_lines=[
                    {"title": li["node"]["title"], "quantity": li["node"]["quantity"]}
                    for li in (node.get("lineItems") or {}).get("edges") or []
                ],
                total_amount=float(money["amount"]) if money.get("amount") else None,
                currency=money.get("currencyCode"),
                # Order.email is frequently null even when the order clearly
                # has a customer attached (confirmed live, 2026-09-14: 2 of 3
                # real test orders had email=null but customer.email set) —
                # not a protected-data restriction (no GraphQL errors, full
                # 200 response), Shopify's order-level email field is just
                # unreliable. customer.email is the fallback.
                buyer_email=node.get("email") or (node.get("customer") or {}).get("email"),
                buyer_name=_customer_name(node.get("customer")),
                placed_at=datetime.fromisoformat(node["createdAt"]) if node.get("createdAt") else None,
                raw_metadata=node,
            ))
        return results

    async def fetch_returns(
        self, connection: MarketplaceConnection, since: Optional[datetime] = None
    ) -> list[NormalizedReturn]:
        """Best-effort — Shopify has no distinct 'return case' object like
        eBay's Post-Order API or ChannelEngine's /returns endpoint. Refunds
        are the closest available signal: an order with a refund attached is
        treated as a 'return', which will under-represent in-transit/pending
        returns that haven't reached a refund decision yet. Flagged here
        rather than silently presented as equivalent to a real returns API —
        confirm against a live store before relying on this for anything
        beyond a rough signal.
        """
        gql = """
        query OrdersWithRefunds {
          orders(first: 50, query: "refunds:>0", sortKey: UPDATED_AT) {
            edges { node {
              id name
              refunds(first: 5) { id createdAt note }
            } }
          }
        }
        """
        data = await self._graphql(connection, gql)
        if not data:
            return []
        results = []
        for edge in (data.get("orders") or {}).get("edges") or []:
            node = edge["node"]
            for refund in node.get("refunds") or []:
                results.append(NormalizedReturn(
                    external_case_id=refund["id"],
                    external_order_id=node["id"],
                    link_type="return",
                    reason=refund.get("note"),
                    status="refunded",
                    raw_metadata=refund,
                ))
        return results

    # ------------------------------------------------------------------
    # Webhooks (auto path) — HMAC scheme ported from bp_shopify's
    # _verify_webhook_hmac (distinct from the OAuth-callback HMAC above)
    # ------------------------------------------------------------------

    @staticmethod
    def verify_webhook_hmac(raw_body: bytes, provided_b64: str, client_secret: str) -> bool:
        digest = hmac.new(client_secret.encode(), raw_body, hashlib.sha256).digest()
        return hmac.compare_digest(base64.b64encode(digest).decode(), provided_b64)

    def parse_webhook(
        self, raw_payload: bytes, headers: dict[str, str]
    ) -> Optional[NormalizedOrder | NormalizedReturn | NormalizedMessage]:
        """Signature verification happens in the webhook route (needs the raw
        body + the connection's own secret before this is even called) —
        this method assumes it's already been verified. Parses the HTTP-layer
        bits (topic header, JSON body) and delegates to normalize_event(),
        which is also called directly by the Celery task from a stored
        MarketplaceEvent row (event_type + already-decoded payload dict) —
        one mapping implementation, two entry points, not two copies of the
        topic-dispatch logic to keep in sync."""
        import json

        topic = headers.get("X-Shopify-Topic", "")
        try:
            payload = json.loads(raw_payload)
        except Exception:
            return None
        return self.normalize_event(topic, payload)

    def normalize_event(
        self, event_type: str, payload: dict
    ) -> Optional[NormalizedOrder | NormalizedReturn | NormalizedMessage]:
        """Returns None for topics this connector ignores (GDPR compliance
        topics are handled at the route layer, not mapped into a Normalized*
        shape here)."""
        topic = event_type
        if topic in ("orders/create", "orders/updated"):
            money = (payload.get("total_price_set") or {}).get("shop_money") or {}
            return NormalizedOrder(
                external_order_id=str(payload.get("id")),
                status=_map_shopify_status(payload.get("fulfillment_status"), bool(payload.get("cancelled_at"))),
                order_lines=[
                    {"title": li.get("title"), "quantity": li.get("quantity")}
                    for li in payload.get("line_items") or []
                ],
                total_amount=float(money["amount"]) if money.get("amount") else None,
                currency=money.get("currency_code"),
                buyer_email=payload.get("email") or (payload.get("customer") or {}).get("email"),
                # REST webhook payload's customer object is snake_case
                # (first_name/last_name), unlike GraphQL's camelCase — not
                # reusing _customer_name() here since it'd silently return
                # None against the wrong key spelling.
                buyer_name=" ".join(
                    p for p in (
                        (payload.get("customer") or {}).get("first_name"),
                        (payload.get("customer") or {}).get("last_name"),
                    ) if p
                ) or None,
                raw_metadata=payload,
            )
        if topic == "orders/cancelled":
            return NormalizedReturn(
                external_case_id=f"cancel:{payload.get('id')}",
                external_order_id=str(payload.get("id")),
                link_type="return",
                reason=payload.get("cancel_reason"),
                status="cancelled",
                raw_metadata=payload,
            )
        return None

    async def send_message(self, connection: MarketplaceConnection, order_or_case_id: str, message: str) -> SendResult:
        return SendResult(success=False, error="Shopify has no order-tied buyer-messaging API — not a gap, a platform limitation (Phase 0 finding)")


shopify_connector = ShopifyConnector()

__all__ = ["ShopifyConnector", "shopify_connector"]
