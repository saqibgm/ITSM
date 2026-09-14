"""
eBay connector — pilot batch #4, per
docs/plans/NATIVE_MARKETPLACE_CONNECTORS_PLAN.md §3/§5.

No reference implementation to port (same situation as Walmart) — built
directly from eBay's published Sell APIs / Post-Order API docs, not verified
against a live sandbox account. Everything here needs sandbox validation
before production use.

Auth is a standard OAuth 2.0 authorization-code consent flow, structurally
like Shopify/Amazon — EXCEPT eBay's redirect_uri isn't a raw callback URL.
eBay requires registering a "RuName" (a special identifier string eBay
issues after you register your actual callback URL in their developer
portal) and using THAT as the redirect_uri param — passing a real URL
directly in the authorize request fails. `settings.EBAY_REDIRECT_URI` in
this repo's config should hold that RuName, not a URL, despite the setting's
generic name — flagged here since it's an easy mistake to make by analogy
with Shopify/Amazon's plain-URL redirect_uri.

Per Phase 0 (plan §2): eBay's messaging is "likely full, not sandbox-
validated" — the Post-Order API's case object carries inquiries, but
whether that gives a clean send/receive message thread the way Amazon's
Messaging API or Shopee's Chat API do hasn't been confirmed against a real
sandbox case. messaging_capability is set to FULL to reflect that finding,
but send_message()/inbound handling below are still unverified — same
honesty flag as Amazon's send_message.
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

# Maps eBay's orderFulfillmentStatus onto NormalizedOrder's documented
# 'new'|'acknowledged'|'shipped'|'delivered'|'cancelled' set (base.py) — was
# just lowercasing the raw eBay value before (2026-09-14 fix), which doesn't
# match any of the 5 canonical values the frontend's status filter/badges
# actually expect. eBay's Fulfillment API order object has no
# 'delivered'/'cancelled' signal at this field — cancellations live in the
# separate Post-Order API case model — so nothing maps to those here, same
# honest gap as Amazon's status mapping.
_EBAY_STATUS_MAP = {
    "NOT_STARTED": "new",
    "IN_PROGRESS": "acknowledged",
    "FULFILLED": "shipped",
}


def _map_ebay_status(raw_fulfillment_status) -> str:
    return _EBAY_STATUS_MAP.get((raw_fulfillment_status or "").upper(), "new")


def _base_urls(environment: str) -> tuple[str, str]:
    """Returns (authorize_base, api_base).

    These are TWO DIFFERENT eBay hosts, not the same host reused — a real
    bug found via live testing (2026-09-14): the user-facing consent screen
    lives on auth.*.ebay.com, while the token endpoint (/identity/v1/oauth2/
    token, used for both the initial code exchange and refreshes) and the
    Sell/Post-Order APIs live on api.*.ebay.com. Redirecting the browser to
    api.sandbox.ebay.com/oauth2/authorize (the original, wrong version of
    this function) 404s — that host has no such page.
    """
    if environment == "sandbox":
        return "https://auth.sandbox.ebay.com", "https://api.sandbox.ebay.com"
    return "https://auth.ebay.com", "https://api.ebay.com"


class EbayConnector(CommerceConnector):
    provider = "ebay"
    messaging_capability = MessagingCapability.FULL  # per Phase 0, unconfirmed depth — see module docstring

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=15.0)
        return self._client

    def authorize_url(self, state: str) -> str:
        settings = get_settings()
        authorize_base, _ = _base_urls(settings.EBAY_ENVIRONMENT)
        params = httpx.QueryParams({
            "client_id": settings.EBAY_CLIENT_ID,
            "redirect_uri": settings.EBAY_REDIRECT_URI,  # RuName, not a URL — see module docstring
            "response_type": "code",
            "scope": settings.EBAY_SCOPES,
            "state": state,
        })
        return f"{authorize_base}/oauth2/authorize?{params}"

    async def connect(self, tenant_id: str, credentials: dict) -> ConnectionResult:
        settings = get_settings()
        code = credentials.get("code")
        if not code:
            return ConnectionResult(success=False, error="missing code")

        _, api_base = _base_urls(settings.EBAY_ENVIRONMENT)
        client = await self._get_client()
        try:
            resp = await client.post(
                f"{api_base}/identity/v1/oauth2/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": settings.EBAY_REDIRECT_URI,
                },
                auth=(settings.EBAY_CLIENT_ID, settings.EBAY_CLIENT_SECRET),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            payload = resp.json()
        except Exception as exc:
            logger.error("[eBay] token exchange failed: %r", exc, exc_info=True)
            return ConnectionResult(success=False, error=str(exc))

        if not payload.get("access_token") or not payload.get("refresh_token"):
            return ConnectionResult(success=False, error=payload.get("error", "token_exchange_failed"))
        # Hand the payload back instead of making the route re-exchange
        # `code` — same single-use-code bug fixed in Shopify's connector
        # (2026-09-14), applies here too.
        return ConnectionResult(success=True, credentials=payload)

    async def _ensure_fresh_token(self, connection: MarketplaceConnection) -> Optional[dict]:
        creds = connection.credentials
        expires_at_raw = creds.get("access_token_expires_at")
        expires_at = datetime.fromisoformat(expires_at_raw) if expires_at_raw else None
        if expires_at and expires_at - datetime.now(timezone.utc) > _REFRESH_SKEW:
            return creds

        settings = get_settings()
        _, api_base = _base_urls(settings.EBAY_ENVIRONMENT)
        client = await self._get_client()
        try:
            resp = await client.post(
                f"{api_base}/identity/v1/oauth2/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": decrypt_secret(creds["refresh_token"]),
                    "scope": settings.EBAY_SCOPES,
                },
                auth=(settings.EBAY_CLIENT_ID, settings.EBAY_CLIENT_SECRET),
            )
            payload = resp.json()
        except Exception as exc:
            logger.error("[eBay] token refresh failed for connection %s: %r", connection.id, exc, exc_info=True)
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

    async def fetch_orders(
        self, connection: MarketplaceConnection, since: Optional[datetime] = None
    ) -> list[NormalizedOrder]:
        creds = await self._ensure_fresh_token(connection)
        if not creds:
            return []
        settings = get_settings()
        _, api_base = _base_urls(settings.EBAY_ENVIRONMENT)
        # eBay's Fulfillment API date filter needs a literal 'Z' suffix and
        # no microseconds/offset — Python's plain .isoformat() on a tz-aware
        # datetime instead produces "+00:00" (and microseconds, if nonzero),
        # which eBay rejects outright with error 30810 "Invalid date format"
        # (confirmed live, 2026-09-14: the sandbox order query 400'd with
        # exactly that error the first time this ran end-to-end). Same class
        # of "docs say ISO 8601 but the real API is stricter" issue already
        # hit with Amazon's CreatedAfter earlier in this same build.
        creation_date = (since or datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        filter_parts = [f"creationdate:[{creation_date}..]"]
        client = await self._get_client()
        try:
            resp = await client.get(
                f"{api_base}/sell/fulfillment/v1/order",
                headers={"Authorization": f"Bearer {decrypt_secret(creds['access_token'])}"},
                params={"filter": ",".join(filter_parts), "limit": 50},
            )
            if resp.status_code != 200:
                logger.warning("[eBay] GET order -> %d: %s", resp.status_code, resp.text[:200])
                return []
            body = resp.json()
        except Exception as exc:
            logger.error("[eBay] GET order failed: %r", exc, exc_info=True)
            return []

        results = []
        for order in body.get("orders", []):
            total = (order.get("pricingSummary") or {}).get("total") or {}
            results.append(NormalizedOrder(
                external_order_id=order.get("orderId"),
                status=_map_ebay_status(order.get("orderFulfillmentStatus")),
                order_lines=[
                    {"title": li.get("lineItemId"), "quantity": li.get("quantity")}
                    for li in order.get("lineItems", [])
                ],
                total_amount=float(total["value"]) if total.get("value") else None,
                currency=total.get("currency"),
                # eBay's Fulfillment API doesn't expose a real buyer email at
                # all (confirmed via docs) — was being stored in buyer_email
                # before this fix (2026-09-14), mislabeling a username as an
                # email. buyer_name is the honest field for it; buyer_email
                # stays unset (None) for eBay orders.
                buyer_name=(order.get("buyer") or {}).get("username"),
                placed_at=datetime.fromisoformat(order["creationDate"]) if order.get("creationDate") else None,
                raw_metadata=order,
            ))
        return results

    async def fetch_returns(
        self, connection: MarketplaceConnection, since: Optional[datetime] = None
    ) -> list[NormalizedReturn]:
        """Post-Order API's case search — confirmed to exist (plan §2), NOT
        sandbox-validated here. `caseType=RETURN` filters to return cases
        specifically; eBay's Post-Order API also covers cancellations and
        inquiries under the same case model, not pulled in here to keep this
        method scoped to capability #2 only."""
        creds = await self._ensure_fresh_token(connection)
        if not creds:
            return []
        settings = get_settings()
        _, api_base = _base_urls(settings.EBAY_ENVIRONMENT)
        client = await self._get_client()
        try:
            resp = await client.get(
                f"{api_base}/post-order/v2/casemanagement/search",
                headers={"Authorization": f"Bearer {decrypt_secret(creds['access_token'])}"},
                params={"case_type": "RETURN", "limit": 50},
            )
            if resp.status_code != 200:
                logger.warning("[eBay] case search -> %d: %s", resp.status_code, resp.text[:200])
                return []
            body = resp.json()
        except Exception as exc:
            logger.error("[eBay] case search failed: %r", exc, exc_info=True)
            return []

        results = []
        for case in body.get("members", []):
            results.append(NormalizedReturn(
                external_case_id=case.get("caseId"),
                external_order_id=case.get("orderId"),
                link_type="return",
                reason=case.get("reason"),
                status=case.get("status"),
                raw_metadata=case,
            ))
        return results

    def parse_webhook(self, raw_payload: bytes, headers: dict) -> None:
        """eBay does have a Platform Notifications / webhook mechanism, but
        its signature scheme wasn't confirmed in this org's research (Phase 0
        focused on messaging capability, not notification signing). Returns
        None — do not wire a webhook route until that's confirmed, same
        stance as Walmart's connector."""
        return None

    async def send_message(self, connection: MarketplaceConnection, order_or_case_id: str, message: str) -> SendResult:
        """Two real findings from direct doc research (2026-09-14) that
        narrow this a lot from the original "messaging_capability = FULL,
        mechanism unconfirmed" state:

        1. The Post-Order API's CASE resource (what fetch_returns() above
           actually syncs — RETURN case type) has NO standalone "send a
           message" endpoint at all. Comments can only be attached as a
           side-effect of a resolving action (close/issue_refund/appeal) —
           there's no way to just message a buyer about their return
           independent of one of those actions.
        2. The one real two-way messaging endpoint eBay does have —
           POST /post-order/v2/inquiry/{inquiryId}/send_message — is scoped
           to a DIFFERENT resource: "INR" (Item Not Received) inquiries, not
           return/replacement cases. `order_or_case_id` here must be an
           inquiryId, not the caseId fetch_returns() produces — this
           connector doesn't currently fetch inquiries at all, only return
           cases, so there's nothing wired up to supply one yet.

        On top of that: eBay's own docs state this endpoint is explicitly
        "not supported in the Sandbox environment" — meaning even with a
        real inquiryId, this cannot be live-verified against this org's
        sandbox connection the way everything else in this build was.
        Implemented against the documented production shape; flagged as
        unverified because it structurally CAN'T be verified here, not
        because the work wasn't done.
        """
        creds = await self._ensure_fresh_token(connection)
        if not creds:
            return SendResult(success=False, error="could not refresh token")

        settings = get_settings()
        _, api_base = _base_urls(settings.EBAY_ENVIRONMENT)
        if settings.EBAY_ENVIRONMENT == "sandbox":
            return SendResult(success=False, error="eBay's inquiry send_message endpoint is not supported in sandbox — cannot test here, production-only")

        client = await self._get_client()
        try:
            resp = await client.post(
                f"{api_base}/post-order/v2/inquiry/{order_or_case_id}/send_message",
                headers={"Authorization": f"Bearer {decrypt_secret(creds['access_token'])}"},
                json={"message": {"content": message}},
            )
            if resp.status_code != 200:
                logger.warning("[eBay] send_message -> %d: %s", resp.status_code, resp.text[:200])
                return SendResult(success=False, error=f"eBay returned {resp.status_code}")
        except Exception as exc:
            logger.error("[eBay] send_message failed: %r", exc, exc_info=True)
            return SendResult(success=False, error=str(exc))
        return SendResult(success=True)

    def order_url(self, connection: MarketplaceConnection, external_order_id: str) -> Optional[str]:
        """MEDIUM confidence — the /sh/ord/ Seller Hub orders prefix is
        confirmed real, but the exact deep-link query param for one specific
        order (orderid= here) wasn't confirmed against live docs the way
        Shopify/Amazon's were (2026-09-14 research only turned up the
        general Seller Hub orders section, not a documented single-order
        permalink spec). Worth a live click-through once this org has a
        real eBay order to test against — not blindly trusted like the
        other two."""
        return f"https://www.ebay.com/sh/ord/details?orderid={external_order_id}"


ebay_connector = EbayConnector()

__all__ = ["EbayConnector", "ebay_connector"]
