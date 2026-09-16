"""
Mercado Libre connector — MESSAGING-ONLY scope (2026-09-15), per an explicit
user request to add real buyer-communication coverage for every marketplace
"other apps/services are doing" it for, not just the original 5-connector
pilot batch. Deliberately narrower than Shopify/Amazon/Walmart/eBay/Etsy:
fetch_orders() here exists only to anchor a MarketplaceOrder row for
messages to attach to (buyer identity, pack_id) — no returns sync, no
status-mapping nuance, no order_lines detail. Do not extend this into a
full order/return connector without redoing it properly against the same
"live-verify everything" discipline the pilot batch went through.

UNVERIFIED — no live sandbox/credentials exist for this org's Mercado Libre
account. Written directly against real, cited documentation (see research
notes below), same starting point Amazon/eBay/Etsy's connectors had before
this org's live sandbox testing caught real docs-vs-reality gaps in each of
them (Amazon's CreatedAfter format, Etsy's buyer_email fallback, eBay's
auth-host split). Treat every endpoint/field name here as needing the same
kind of live check before trusting it in production.

Confirmed via direct research (2026-09-15):
- Real, two-way, order-tied messaging API exists — POST-SALE Messaging API,
  distinct from the pre-purchase Questions API (item-level Q&A, NOT used
  here). GET/POST https://api.mercadolibre.com/messages/packs/{pack_id}/
  sellers/{seller_id} — create/list messages on an order's pack thread.
- pack_id is the correct key, NOT order_id directly — per ML's own docs,
  pack_id is the "cart"/pack an order belongs to, and can be null for
  orders that were never grouped into a pack (i.e. most single-item
  orders). Falls back to the order's own id in that case — an assumption,
  not confirmed against a real null-pack_id order; the messaging endpoint
  may or may not accept a raw order id in the same slot.
- Buyer email is NOT confirmed available on the order object — ML's docs
  note personal buyer/seller data was removed from GET Orders for Mercado
  Envios 2; buyer identity is buyer.nickname (a username, not an email),
  same "no real email" situation as eBay. No email fallback for this
  connector — messaging_capability stays whatever the native API supports,
  no NONE-with-email-fallback path like Shopify/Etsy/Walmart.
- OAuth 2.0, standard authorization-code grant. Auth screen lives on a
  COUNTRY-SPECIFIC domain (auth.mercadolibre.com.ar for Argentina, .com.mx
  for Mexico, etc.) matching whichever Mercado Libre site the seller
  account belongs to — defaulted to .com.ar below via
  MERCADOLIBRE_AUTH_DOMAIN, NOT verified against a real seller account on
  a different site. Token exchange/refresh is on the single global
  api.mercadolibre.com host regardless of site.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from app.config import get_settings
from app.models.marketplace import MarketplaceConnection, MarketplaceOrder
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

_TOKEN_URL = "https://api.mercadolibre.com/oauth/token"
_API_BASE = "https://api.mercadolibre.com"
_REFRESH_SKEW = timedelta(minutes=5)


class MercadoLibreConnector(CommerceConnector):
    provider = "mercadolibre"
    # FULL per Phase 0-style research: the post-sale Messaging API is
    # genuinely two-way (send + list), confirmed via ML's own developer
    # docs — not inferred. Unverified against a live account, same caveat
    # as every other flag in this module docstring.
    messaging_capability = MessagingCapability.FULL

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
            "client_id": settings.MERCADOLIBRE_CLIENT_ID,
            "redirect_uri": settings.MERCADOLIBRE_REDIRECT_URI,
            "state": state,
        })
        return f"{settings.MERCADOLIBRE_AUTH_DOMAIN}/authorization?{params}"

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
                    "client_id": settings.MERCADOLIBRE_CLIENT_ID,
                    "client_secret": settings.MERCADOLIBRE_CLIENT_SECRET,
                    "code": code,
                    "redirect_uri": settings.MERCADOLIBRE_REDIRECT_URI,
                },
                headers={"Accept": "application/json"},
            )
            payload = resp.json()
        except Exception as exc:
            logger.error("[MercadoLibre] token exchange failed: %r", exc, exc_info=True)
            return ConnectionResult(success=False, error=str(exc))

        if not payload.get("access_token"):
            return ConnectionResult(success=False, error=payload.get("message", "token_exchange_failed"))

        # user_id from the token response IS the seller id — used as the
        # {seller_id} path param on every messaging call below. Confirmed
        # field name per ML's OAuth docs (not just inferred from REST
        # convention).
        return ConnectionResult(success=True, external_id=str(payload.get("user_id", "")), credentials=payload)

    async def _ensure_fresh_token(self, connection: MarketplaceConnection) -> Optional[dict]:
        creds = connection.credentials
        expires_at_raw = creds.get("access_token_expires_at")
        expires_at = datetime.fromisoformat(expires_at_raw) if expires_at_raw else None
        if expires_at and expires_at - datetime.now(timezone.utc) > _REFRESH_SKEW:
            return creds

        refresh_token = creds.get("refresh_token")
        if not refresh_token:
            logger.warning("[MercadoLibre] connection %s has no refresh_token — must reconnect", connection.id)
            return None

        settings = get_settings()
        client = await self._get_client()
        try:
            resp = await client.post(
                _TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "client_id": settings.MERCADOLIBRE_CLIENT_ID,
                    "client_secret": settings.MERCADOLIBRE_CLIENT_SECRET,
                    "refresh_token": decrypt_secret(refresh_token),
                },
                headers={"Accept": "application/json"},
            )
            payload = resp.json()
        except Exception as exc:
            logger.error("[MercadoLibre] token refresh failed for connection %s: %r", connection.id, exc, exc_info=True)
            return None

        new_access_token = payload.get("access_token")
        if not new_access_token:
            return None
        creds = dict(creds)
        creds["access_token"] = encrypt_secret(new_access_token)
        # ML rotates refresh_token on every refresh (like Shopify, unlike
        # Amazon) — per ML's own docs, the old refresh_token becomes
        # invalid immediately. Must persist the new one or the connection
        # becomes unrecoverable on the NEXT refresh.
        if payload.get("refresh_token"):
            creds["refresh_token"] = encrypt_secret(payload["refresh_token"])
        expires_in = payload.get("expires_in")
        if expires_in:
            creds["access_token_expires_at"] = (
                datetime.now(timezone.utc) + timedelta(seconds=expires_in)
            ).isoformat()
        return creds

    async def fetch_orders(
        self, connection: MarketplaceConnection, since: Optional[datetime] = None
    ) -> list[NormalizedOrder]:
        """Messaging-only scope — see module docstring. Minimal order fetch,
        just enough to anchor a MarketplaceOrder row: id, pack_id (for
        messaging), buyer nickname, status, total. No line-item detail."""
        creds = await self._ensure_fresh_token(connection)
        if not creds:
            return []
        seller_id = connection.external_id
        client = await self._get_client()
        try:
            resp = await client.get(
                f"{_API_BASE}/orders/search",
                headers={"Authorization": f"Bearer {decrypt_secret(creds['access_token'])}"},
                params={"seller": seller_id, "sort": "date_desc"},
            )
            if resp.status_code != 200:
                logger.warning("[MercadoLibre] GET /orders/search -> %d: %s", resp.status_code, resp.text[:200])
                return []
            body = resp.json()
        except Exception as exc:
            logger.error("[MercadoLibre] GET /orders/search failed: %r", exc, exc_info=True)
            return []

        results = []
        for order in body.get("results", []):
            total = order.get("total_amount")
            results.append(NormalizedOrder(
                external_order_id=str(order.get("id")),
                status=str(order.get("status") or "new"),
                order_lines=[],  # out of scope — see module docstring
                total_amount=float(total) if total is not None else None,
                currency=order.get("currency_id"),
                buyer_email=None,  # not exposed — see module docstring
                buyer_name=(order.get("buyer") or {}).get("nickname"),
                # pack_id stashed in raw_metadata, not order_lines — this
                # connector has no line items to attach it to, unlike
                # eBay's legacy_item_id-per-line pattern.
                placed_at=datetime.fromisoformat(order["date_created"]) if order.get("date_created") else None,
                raw_metadata=order,
            ))
        return results

    async def fetch_returns(
        self, connection: MarketplaceConnection, since: Optional[datetime] = None
    ) -> list[NormalizedReturn]:
        """Out of scope — messaging-only connector, see module docstring."""
        return []

    def parse_webhook(self, raw_payload: bytes, headers: dict) -> None:
        """ML does have a real webhook (IPN/notifications) mechanism, but
        signature verification wasn't researched for this messaging-only
        pass — same conservative stance as Walmart/eBay's connectors."""
        return None

    def _pack_id(self, order: MarketplaceOrder) -> Optional[str]:
        """pack_id lives in raw_metadata (see fetch_orders) — falls back to
        the order's own external_order_id when pack_id is null (most
        single-item orders never get grouped into a pack). UNVERIFIED
        whether the messaging endpoint actually accepts a raw order id in
        the pack_id slot when there's no real pack — see module docstring."""
        pack_id = (order.raw_metadata or {}).get("pack_id")
        return str(pack_id) if pack_id else order.external_order_id

    async def send_message(self, connection: MarketplaceConnection, order: MarketplaceOrder, message: str) -> SendResult:
        creds = await self._ensure_fresh_token(connection)
        if not creds:
            return SendResult(success=False, error="could not refresh token")

        client = await self._get_client()
        try:
            resp = await client.post(
                f"{_API_BASE}/messages/packs/{self._pack_id(order)}/sellers/{connection.external_id}",
                headers={"Authorization": f"Bearer {decrypt_secret(creds['access_token'])}"},
                params={"tag": "post_sale"},
                json={"text": message},
            )
            if resp.status_code not in (200, 201):
                logger.warning("[MercadoLibre] send_message -> %d: %s", resp.status_code, resp.text[:200])
                return SendResult(success=False, error=f"Mercado Libre returned {resp.status_code}")
            body = resp.json()
        except Exception as exc:
            logger.error("[MercadoLibre] send_message failed: %r", exc, exc_info=True)
            return SendResult(success=False, error=str(exc))
        return SendResult(success=True, external_message_id=str(body.get("id")) if body.get("id") else None)

    async def fetch_messages(self, connection: MarketplaceConnection, order: MarketplaceOrder) -> list[NormalizedMessage]:
        creds = await self._ensure_fresh_token(connection)
        if not creds:
            return []

        client = await self._get_client()
        try:
            resp = await client.get(
                f"{_API_BASE}/messages/packs/{self._pack_id(order)}/sellers/{connection.external_id}",
                headers={"Authorization": f"Bearer {decrypt_secret(creds['access_token'])}"},
                params={"tag": "post_sale", "mark_as_read": "false"},
            )
            if resp.status_code != 200:
                logger.warning("[MercadoLibre] GET messages -> %d: %s", resp.status_code, resp.text[:200])
                return []
            body = resp.json()
        except Exception as exc:
            logger.error("[MercadoLibre] GET messages failed: %r", exc, exc_info=True)
            return []

        results = []
        for msg in body.get("messages", []):
            sender_user_id = str((msg.get("from") or {}).get("user_id", ""))
            results.append(NormalizedMessage(
                external_message_id=str(msg.get("id")) if msg.get("id") else None,
                external_order_id=order.external_order_id,
                external_case_id=None,
                body=(msg.get("text") or {}).get("plain") or msg.get("text") or "",
                sent_at=datetime.fromisoformat(msg["message_date"]["received"]) if (msg.get("message_date") or {}).get("received") else None,
                # direction computed here (not by the generic sync loop —
                # see ebay.py's fetch_messages for why): sender's user_id
                # compared against OUR own seller user_id
                # (connection.external_id, set at connect() time).
                raw_metadata={"direction": "outbound" if sender_user_id and sender_user_id == connection.external_id else "inbound"},
            ))
        return results

    def order_url(self, connection: MarketplaceConnection, external_order_id: str) -> Optional[str]:
        """MEDIUM confidence — ML's seller order-detail page pattern,
        inferred from common URL conventions, not confirmed against a
        documented permalink spec."""
        return f"https://myaccount.mercadolibre.com/sales/{external_order_id}"


mercadolibre_connector = MercadoLibreConnector()

__all__ = ["MercadoLibreConnector", "mercadolibre_connector"]
