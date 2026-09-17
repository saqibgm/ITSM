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
2. Inbound webhook for ORDER/RETURN events — still none. Amazon's
   Notifications API delivers those via AWS SQS, a fundamentally different
   consumption model (a queue poller/consumer, not a webhook route) that
   the chatbot repo also never built (same doc, same section). parse_webhook()
   below always returns None; the "auto" path for orders/returns isn't
   available until an SQS consumer is built as a separate mechanism — a
   real future phase, not something to fake here.

messaging_capability = FULL as of 2026-09-17 (was OUTBOUND_ONLY). SP-API's
Messaging API is confirmed send-only and mostly action-gated (see
_send_via_sp_api()'s docstring) — that part of the Phase 0 finding still
holds. What changed: Amazon officially forwards buyer-seller messages to a
seller-configured email address, and replying via email (from the address
registered on that account) is also policy-legitimate — a genuine two-way
channel that doesn't touch SP-API at all. See
marketplace_amazon_inbound_email.py (the inbound route),
normalize_event()/_send_via_email_bridge() below. UNVERIFIED against live
traffic — no seller account has exercised this path yet, same caveat as
the SP-API messaging code it sits alongside.

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
    NormalizedMessage,
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

# Maps SP-API's OrderStatus onto NormalizedOrder's documented
# 'new'|'acknowledged'|'shipped'|'delivered'|'cancelled' set (base.py) — was
# just lowercasing the raw Amazon value before (2026-09-14 fix), which
# doesn't match any of the 5 canonical values the frontend's status filter/
# badges actually expect. Amazon's Orders API has no 'delivered' signal at
# this level (that needs a separate Tracking API call per shipment) —
# nothing maps to it here, same honest gap as eBay's status mapping.
_AMAZON_STATUS_MAP = {
    "PENDING": "new",
    "PENDINGAVAILABILITY": "new",
    "INVOICEUNCONFIRMED": "new",
    "UNSHIPPED": "acknowledged",
    "PARTIALLYSHIPPED": "acknowledged",
    "UNFULFILLABLE": "acknowledged",
    "SHIPPED": "shipped",
    "CANCELED": "cancelled",
}


def _map_amazon_status(raw_order_status: Optional[str]) -> str:
    return _AMAZON_STATUS_MAP.get((raw_order_status or "").upper(), "new")


class AmazonConnector(CommerceConnector):
    provider = "amazon"
    # Bumped from OUTBOUND_ONLY to FULL (2026-09-17) — SP-API's Messaging
    # API is genuinely send-only and mostly action-gated (see send_message()
    # below), but the inbound-email bridge (parse_webhook/normalize_event
    # below, route in marketplace_amazon_inbound_email.py) gives a real,
    # working two-way channel once a tenant points Seller Central's
    # Buyer-Seller Messages notification at their connection's inbound
    # address. UNVERIFIED against live traffic — no seller account has
    # actually exercised this path yet.
    messaging_capability = MessagingCapability.FULL

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

        # Hand the payload back instead of making the route re-exchange
        # `code` — same single-use-code bug fixed in Shopify's connector
        # (2026-09-14), applies here too since spapi_oauth_code is also
        # single-use and expires 5 minutes after issuance.
        return ConnectionResult(success=True, external_id=seller_id, credentials=payload)

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
                status=_map_amazon_status(order.get("OrderStatus")),
                order_lines=[],  # getOrderItems is a separate call — not fetched here to avoid N+1; add if a mapping needs line-item detail
                total_amount=float(total["Amount"]) if total.get("Amount") else None,
                currency=total.get("CurrencyCode"),
                # BuyerInfo.BuyerEmail only appears in the GetOrders response
                # when the app has PII access approved AND the call is made
                # with a Restricted Data Token (a separate createRestrictedDataToken
                # exchange, not implemented here) instead of the normal access
                # token this connector uses. Left as a best-effort .get() —
                # will stay empty in sandbox regardless (confirmed live,
                # 2026-09-14: the sandbox mock order has no BuyerInfo key at
                # all) and in production until that RDT flow is built.
                buyer_email=(order.get("BuyerInfo") or {}).get("BuyerEmail"),
                # BuyerInfo.BuyerName has historically been less restricted
                # than BuyerEmail on SP-API, but still absent from this
                # sandbox's mock response — same "will populate in
                # production, not sandbox" caveat as buyer_email above.
                buyer_name=(order.get("BuyerInfo") or {}).get("BuyerName"),
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
        """Still true for the SQS-based Notifications API specifically —
        Amazon has no HTTP webhook mechanism for order/return events. The
        inbound-message EMAIL bridge (marketplace_amazon_inbound_email.py)
        is a deliberately separate, Amazon-specific route that does its own
        parsing at the route layer (raw email bytes, not a marketplace
        webhook payload shape) and calls normalize_event() directly rather
        than going through this generic parse_webhook() entry point — so
        this still correctly returns None."""
        return None

    async def normalize_event(
        self, event_type: str, payload: dict, *, db=None, tenant_id=None, connection=None
    ) -> Optional[NormalizedMessage]:
        """Dispatch target for the inbound-email bridge's 'inbound_email'
        event_type (see marketplace_amazon_inbound_email.py) — called from
        tasks_marketplace_sync.py's process_marketplace_event, same as every
        other connector's webhook-sourced events. relay_alias is stashed in
        raw_metadata so tasks_marketplace_sync.py can persist it onto the
        matched MarketplaceOrder for send_message() below to read back.
        db/tenant_id/connection unused here — the forwarded email already
        carries everything needed (2026-09-17 base.py widening, added for
        eBay's Message API which genuinely needs live lookups)."""
        if event_type != "inbound_email":
            return None
        return NormalizedMessage(
            external_message_id=payload.get("message_id"),
            external_order_id=payload.get("order_id"),
            external_case_id=None,
            body=payload.get("body") or "",
            sent_at=None,
            raw_metadata={"direction": "inbound", "relay_alias": payload.get("from")},
        )

    # ------------------------------------------------------------------
    # Messaging — outbound only (Phase 0 finding). UNVERIFIED: no reference
    # implementation exists to port (the chatbot repo never built this
    # either); shape follows SP-API's documented Messaging API but needs
    # sandbox validation before being trusted.
    # ------------------------------------------------------------------

    async def _send_via_sp_api(self, connection: MarketplaceConnection, order: "MarketplaceOrder", message: str) -> SendResult:
        """Confirmed via direct doc research (2026-09-14): Amazon's Messaging
        API is action-based, not a generic free-text send, same limitation
        eBay's send_message hit. getMessagingActionsForOrder returns which
        of a fixed set of templated actions (AmazonMotors, digitalAccessKey,
        legalDisclosure, warranty, billInvoice, negativeFeedbackRemoval,
        unexpectedProblem, confirmCustomizationDetails, ...) are currently
        available for THIS order — most are templated/fixed-content, not
        free text. confirmCustomizationDetails is the one action that does
        accept genuine free text (1-800 chars) — used here as the closest
        available mapping for a generic "send this message" call. If it
        isn't in the order's available-actions list (most orders won't have
        it — it's meant for confirming customization/personalization
        details, not general buyer contact), there is no free-text option
        for that order and this fails honestly rather than picking a
        templated action and stuffing `message` somewhere it doesn't belong.

        Also currently blocked independent of all this: this connection's
        access token gets a 403 Unauthorized on getMessagingActionsForOrder
        (confirmed live, 2026-09-14) — the Messaging role isn't granted to
        this app in the Solution Provider Portal. That's an app-permission
        gap, not something fixable in code; needs the role added + reconsent
        before this can be live-verified at all. send_message() below tries
        this first and falls back to the email bridge on any failure, so
        nothing regresses once that role is eventually granted.
        """
        actions_body = await self._get(
            connection, f"/messaging/v1/orders/{order.external_order_id}/messages"
        )
        if not actions_body:
            return SendResult(success=False, error="could not fetch available messaging actions for this order (likely a Messaging role/permission gap, not a transient failure)")

        actions = {a.get("name") for a in (actions_body.get("payload") or {}).get("_links", {}).get("actions", [])}
        if "confirmCustomizationDetails" not in actions:
            return SendResult(success=False, error="no free-text messaging action available for this order (Amazon's Messaging API is action-based, not generic send)")

        settings = get_settings()
        creds = await self._ensure_fresh_token(connection)
        if not creds:
            return SendResult(success=False, error="could not refresh token")
        client = await self._get_client()
        try:
            resp = await client.post(
                f"{self._base_url()}/messaging/v1/orders/{order.external_order_id}/messages/confirmCustomizationDetails",
                headers={"x-amz-access-token": decrypt_secret(creds["access_token"])},
                params={"marketplaceIds": settings.AMAZON_MARKETPLACE_IDS},
                json={"text": message},
            )
            if resp.status_code not in (200, 201, 202):
                logger.warning("[Amazon] send_message (SP-API) -> %d: %s", resp.status_code, resp.text[:200])
                return SendResult(success=False, error=f"Amazon returned {resp.status_code}")
        except Exception as exc:
            logger.error("[Amazon] send_message (SP-API) failed: %r", exc, exc_info=True)
            return SendResult(success=False, error=str(exc))
        return SendResult(success=True)

    async def _send_via_email_bridge(self, connection: MarketplaceConnection, order: "MarketplaceOrder", message: str) -> SendResult:
        """Reply through the inbound-email bridge (2026-09-17) — see this
        module's messaging_capability comment and
        marketplace_amazon_inbound_email.py. relay_alias is stashed onto
        order.raw_metadata by tasks_marketplace_sync.py the moment an
        inbound buyer email is received for this order; without at least
        one inbound message on file there's nothing to reply TO yet (Amazon
        requires threading through the buyer's own relay alias, not a
        fixed address), so this fails honestly rather than guessing one.

        Sent via the existing send_email_notification Celery task (plain
        SMTP, no new provider integration) — same fire-and-forget pattern
        already accepted by marketplace_sync.py's own email fallback for
        Shopify/Etsy/Walmart, so success here means "queued", not "buyer
        received it"."""
        relay_alias = (order.raw_metadata or {}).get("amazon_relay_alias")
        if not relay_alias:
            return SendResult(success=False, error="no inbound buyer message on file for this order yet — nothing to reply to (see module docstring)")

        settings = get_settings()
        from app.workers.tasks_notifications import send_email_notification
        send_email_notification.delay(
            to_email=relay_alias,
            from_email=f"amazon+{connection.id}@{settings.AMAZON_INBOUND_EMAIL_DOMAIN}",
            template_name="marketplace_order_message",
            context={
                "title": f"Message about your Amazon order {order.external_order_id}",
                "body": message,
                "buyer_name": order.buyer_name,
                "provider": "amazon",
                "external_order_id": order.external_order_id,
            },
        )
        return SendResult(success=True)

    async def send_message(self, connection: MarketplaceConnection, order: "MarketplaceOrder", message: str) -> SendResult:
        """Tries the native SP-API path first (works once the Messaging role
        is granted); falls back to the email bridge, which works today
        wherever a buyer has already emailed in."""
        sp_api_result = await self._send_via_sp_api(connection, order, message)
        if sp_api_result.success:
            return sp_api_result
        return await self._send_via_email_bridge(connection, order, message)

    def order_url(self, connection: MarketplaceConnection, external_order_id: str) -> Optional[str]:
        """HIGH confidence — orders-v3/order/{AmazonOrderId} is Seller
        Central's current, well-established order-details URL pattern.
        Same URL for every marketplace this app is registered for; no
        per-marketplace subdomain needed. Sandbox orders (like the ones
        this org's test connection actually has) have no real browsable
        page behind this link — it only resolves to something real for a
        production seller account."""
        return f"https://sellercentral.amazon.com/orders-v3/order/{external_order_id}"


amazon_connector = AmazonConnector()

__all__ = ["AmazonConnector", "amazon_connector"]
