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


def _base_urls(environment: str) -> tuple[str, str]:
    """Returns (oauth_base, api_base) — eBay's sandbox and production
    environments live on entirely different hostnames, not a query param
    or header switch like Amazon's sandbox/production split."""
    if environment == "sandbox":
        return "https://api.sandbox.ebay.com", "https://api.sandbox.ebay.com"
    return "https://api.ebay.com", "https://api.ebay.com"


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
        oauth_base, _ = _base_urls(settings.EBAY_ENVIRONMENT)
        params = httpx.QueryParams({
            "client_id": settings.EBAY_CLIENT_ID,
            "redirect_uri": settings.EBAY_REDIRECT_URI,  # RuName, not a URL — see module docstring
            "response_type": "code",
            "scope": settings.EBAY_SCOPES,
            "state": state,
        })
        return f"{oauth_base}/oauth2/authorize?{params}"

    async def connect(self, tenant_id: str, credentials: dict) -> ConnectionResult:
        settings = get_settings()
        code = credentials.get("code")
        if not code:
            return ConnectionResult(success=False, error="missing code")

        oauth_base, _ = _base_urls(settings.EBAY_ENVIRONMENT)
        client = await self._get_client()
        try:
            resp = await client.post(
                f"{oauth_base}/identity/v1/oauth2/token",
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
        return ConnectionResult(success=True)

    async def _ensure_fresh_token(self, connection: MarketplaceConnection) -> Optional[dict]:
        creds = connection.credentials
        expires_at_raw = creds.get("access_token_expires_at")
        expires_at = datetime.fromisoformat(expires_at_raw) if expires_at_raw else None
        if expires_at and expires_at - datetime.now(timezone.utc) > _REFRESH_SKEW:
            return creds

        settings = get_settings()
        oauth_base, _ = _base_urls(settings.EBAY_ENVIRONMENT)
        client = await self._get_client()
        try:
            resp = await client.post(
                f"{oauth_base}/identity/v1/oauth2/token",
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
        filter_parts = [f"creationdate:[{(since or datetime.now(timezone.utc) - timedelta(days=30)).isoformat()}..]"]
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
                status=(order.get("orderFulfillmentStatus") or "new").lower(),
                order_lines=[
                    {"title": li.get("lineItemId"), "quantity": li.get("quantity")}
                    for li in order.get("lineItems", [])
                ],
                total_amount=float(total["value"]) if total.get("value") else None,
                currency=total.get("currency"),
                buyer_email=(order.get("buyer") or {}).get("username"),  # eBay doesn't expose buyer email directly; username is the durable identifier
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
        """UNVERIFIED — no sandbox test performed. Post-Order API's inquiry/
        case comment endpoints are the likely mechanism (plan §2), written
        against documented shape only."""
        creds = await self._ensure_fresh_token(connection)
        if not creds:
            return SendResult(success=False, error="could not refresh token")
        return SendResult(success=False, error="eBay send_message is unverified — needs sandbox validation before use, see module docstring")


ebay_connector = EbayConnector()

__all__ = ["EbayConnector", "ebay_connector"]
