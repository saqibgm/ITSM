"""
Temu connector — ORDERS/RETURNS ONLY, confirmed no buyer-messaging API
(2026-09-18). Part of the 7-marketplace confirmed target scope (Amazon,
Best Buy, Walmart, eBay, Shopify, Temu, TikTok Shop) — unlike the earlier
"messaging-only batch" connectors in this file's siblings, Temu genuinely
has nothing to build a messaging path against, so this connector covers
capability #1/#2 (orders, returns) only, same category as Shopify/Etsy/
Walmart/Newegg's messaging_capability = NONE.

UNVERIFIED — no live seller account/credentials exist for this org's Temu
account. Built directly against real, fetched sources: Temu's own Partner
Platform documentation pages (partner.temu.com/documentation — JS-rendered,
couldn't be fetched directly) mirrored by a third-party open-source docs
repo (github.com/opastorello/temu-api-docs) whose method-name index was
fetchable and cross-checked against multiple independent sources for the
base URL and signing scheme. Exact request/response FIELD NAMES for the
order/after-sales methods are less certain than the confirmed method names
and base URL — flagged per-field below, not presented with false confidence.

Confirmed via direct research (2026-09-18):
- No messaging/chat/conversation API exists. The only method anywhere in
  Temu's API surface containing "message" is `bg.tmc.message.update`, a
  WEBHOOK for system event notifications (order/inventory changes) — not
  a customer-messaging endpoint. Re-confirmed fresh (not just carried over
  from this build's earlier "Skip" recommendation) specifically because
  this connector is now in the required 7-marketplace scope — the
  conclusion is the same: there is nothing to build.
- API gateway: a single RPC-style endpoint (Alibaba/Taobao "TOP API"
  family, same lineage as Lazada's API in this same codebase, though
  NOT the same signing algorithm — see below), confirmed via multiple
  independent sources: POST https://openapi-b-global.temu.com/openapi/router
  — every call goes here with a `type` param selecting the method
  (e.g. type=bg.order.list.v2.get), not distinct REST paths.
- Auth: OAuth-style authorization (manual or callback flow per Temu's own
  "Seller Authorization Guide") exchanged via the `bg.open.accesstoken.create`
  method (confirmed method name) called through the same router.
- Signing (confirmed via Temu's own "Signature Method for API request"
  doc, cross-checked against a third-party mirror): sort ALL request
  params alphabetically by key, concatenate as bare key+value pairs (no
  '=' or '&'), wrap the result with app_secret on BOTH ends
  (app_secret + concatenated + app_secret), MD5 hash, uppercase hex.
  Different from Lazada's HMAC-SHA256-with-path-prefix scheme in this same
  codebase — do not reuse that connector's _sign() logic here.
- Order methods (confirmed method names, response field names best-effort):
  bg.order.list.v2.get (list), bg.order.detail.v2.get (detail, not called
  here — list response is used directly, matching this batch's other
  "minimal order fetch" connectors).
- After-sales/returns: bg.aftersales.aftersales.list.get (confirmed method
  name).
- No buyer-email field confirmed anywhere in the order schema (consistent
  with other China-origin marketplaces in this build's research, e.g.
  Shopee/Wildberries) — left unset, not guessed. buyer_name comes from
  the order's customer-name field (best-effort key).
"""

import hashlib
import logging
import time
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

_API_ROUTER_URL = "https://openapi-b-global.temu.com/openapi/router"
_REFRESH_SKEW = timedelta(minutes=5)


def _sign(params: dict, app_secret: str) -> str:
    """MD5(app_secret + sorted bare-concatenated params + app_secret),
    uppercase hex — see module docstring for the confirmed algorithm and
    its source. NOT the same shape as Lazada's HMAC-SHA256 scheme
    elsewhere in this codebase, despite both being TOP-API-family."""
    sorted_items = sorted(params.items())
    concatenated = app_secret + "".join(f"{k}{v}" for k, v in sorted_items) + app_secret
    return hashlib.md5(concatenated.encode()).hexdigest().upper()


class TemuConnector(CommerceConnector):
    provider = "temu"
    # CONFIRMED none — see module docstring. Same category as Shopify/
    # Etsy/Walmart/Newegg: relies entirely on itsm-service's email
    # fallback IF a buyer email is ever confirmed available (it isn't,
    # today) — until then, orders/returns sync is the whole of what this
    # connector offers, which is still real value for capabilities #1/#2.
    messaging_capability = MessagingCapability.NONE

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=15.0)
        return self._client

    async def _call(self, method_type: str, app_key: str, app_secret: str, extra_params: dict) -> Optional[dict]:
        """Every Temu API call goes through the single router endpoint —
        builds the common signed-request envelope (type, app_key,
        timestamp, data_type) plus whatever params the method needs."""
        params = {
            "type": method_type,
            "app_key": app_key,
            "timestamp": str(int(time.time())),
            "data_type": "JSON",
            **extra_params,
        }
        params["sign"] = _sign(params, app_secret)
        client = await self._get_client()
        try:
            resp = await client.post(_API_ROUTER_URL, data=params)
            body = resp.json()
        except Exception as exc:
            logger.error("[Temu] call to %s failed: %r", method_type, exc, exc_info=True)
            return None
        if body.get("success") is False:
            logger.warning("[Temu] %s -> error_code=%s: %s", method_type, body.get("error_code"), body.get("error_msg"))
        return body

    def authorize_url(self, state: str) -> str:
        """MEDIUM confidence — Temu's "Seller Authorization Guide" confirms
        both a manual and a callback authorization flow exist through the
        Seller Center, but the exact authorize-URL host/path wasn't
        directly fetchable (JS-rendered doc page). Constructed by analogy
        with the confirmed callback flow's redirect_url mechanism; needs
        live validation before trusting this exact URL shape."""
        settings = get_settings()
        params = httpx.QueryParams({
            "app_key": settings.TEMU_CLIENT_ID,
            "redirect_uri": settings.TEMU_REDIRECT_URI,
            "state": state,
        })
        return f"https://seller.temu.com/oauth/authorize?{params}"

    async def connect(self, tenant_id: str, credentials: dict) -> ConnectionResult:
        settings = get_settings()
        code = credentials.get("code")
        if not code:
            return ConnectionResult(success=False, error="missing code")

        payload = await self._call(
            "bg.open.accesstoken.create", settings.TEMU_CLIENT_ID, settings.TEMU_CLIENT_SECRET, {"code": code},
        )
        if not payload:
            return ConnectionResult(success=False, error="token_exchange_failed")
        result = payload.get("result") or payload
        access_token = result.get("access_token")
        if not access_token:
            return ConnectionResult(success=False, error=payload.get("error_msg", "token_exchange_failed"))
        return ConnectionResult(success=True, external_id=str(result.get("mall_id") or result.get("seller_id") or ""), credentials=result)

    async def _ensure_fresh_token(self, connection: MarketplaceConnection) -> Optional[dict]:
        creds = connection.credentials
        expires_at_raw = creds.get("access_token_expires_at")
        expires_at = datetime.fromisoformat(expires_at_raw) if expires_at_raw else None
        if expires_at and expires_at - datetime.now(timezone.utc) > _REFRESH_SKEW:
            return creds
        if not expires_at:
            # Some TOP-family platforms issue long-lived/non-expiring
            # tokens on this grant type — if no expiry was ever recorded,
            # treat the stored token as still good rather than blocking on
            # a refresh_token this connector isn't confirmed to receive.
            return creds

        refresh_token = creds.get("refresh_token")
        if not refresh_token:
            logger.warning("[Temu] connection %s has no refresh_token — must reconnect", connection.id)
            return None

        settings = get_settings()
        payload = await self._call(
            "bg.open.accesstoken.refresh", settings.TEMU_CLIENT_ID, settings.TEMU_CLIENT_SECRET,
            {"refresh_token": decrypt_secret(refresh_token)},
        )
        if not payload:
            return None
        result = payload.get("result") or payload
        if not result.get("access_token"):
            return None
        creds = dict(creds)
        creds["access_token"] = encrypt_secret(result["access_token"])
        if result.get("refresh_token"):
            creds["refresh_token"] = encrypt_secret(result["refresh_token"])
        expires_in = result.get("expires_in") or result.get("access_token_expire_in")
        if expires_in:
            creds["access_token_expires_at"] = (
                datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
            ).isoformat()
        return creds

    async def fetch_orders(
        self, connection: MarketplaceConnection, since: Optional[datetime] = None
    ) -> list[NormalizedOrder]:
        creds = await self._ensure_fresh_token(connection)
        if not creds:
            return []
        settings = get_settings()
        payload = await self._call(
            "bg.order.list.v2.get", settings.TEMU_CLIENT_ID, settings.TEMU_CLIENT_SECRET,
            {"access_token": decrypt_secret(creds["access_token"]), "page": "1", "pageSize": "50"},
        )
        if not payload:
            return []

        result = payload.get("result") or {}
        orders = result.get("orderList") or result.get("list") or []
        results = []
        for order in orders:
            total = order.get("orderAmount") or order.get("totalAmount")
            created_raw = order.get("parentOrderTime") or order.get("createdTime")
            results.append(NormalizedOrder(
                external_order_id=str(order.get("parentOrderSn") or order.get("orderSn") or order.get("order_id") or ""),
                status=str(order.get("orderStatus") or order.get("status") or "new"),
                order_lines=[],  # out of scope — see module docstring
                total_amount=float(total) if total is not None else None,
                currency=order.get("currency"),
                buyer_email=None,  # not confirmed available — see module docstring
                buyer_name=order.get("buyerName") or (order.get("address") or {}).get("receiverName"),
                placed_at=datetime.fromtimestamp(int(created_raw) / 1000, tz=timezone.utc) if created_raw else None,
                raw_metadata=order,
            ))
        return results

    async def fetch_returns(
        self, connection: MarketplaceConnection, since: Optional[datetime] = None
    ) -> list[NormalizedReturn]:
        creds = await self._ensure_fresh_token(connection)
        if not creds:
            return []
        settings = get_settings()
        payload = await self._call(
            "bg.aftersales.aftersales.list.get", settings.TEMU_CLIENT_ID, settings.TEMU_CLIENT_SECRET,
            {"access_token": decrypt_secret(creds["access_token"]), "page": "1", "pageSize": "50"},
        )
        if not payload:
            return []

        result = payload.get("result") or {}
        cases = result.get("aftersalesList") or result.get("list") or []
        results = []
        for case in cases:
            results.append(NormalizedReturn(
                external_case_id=str(case.get("aftersalesSn") or case.get("id") or ""),
                external_order_id=str(case.get("parentOrderSn") or case.get("orderSn") or ""),
                link_type="return",
                reason=case.get("reason") or case.get("applyReasonText"),
                status=case.get("status") or case.get("aftersalesStatus"),
                raw_metadata=case,
            ))
        return results

    def parse_webhook(self, raw_payload: bytes, headers: dict) -> None:
        """bg.tmc.message.update is a real webhook mechanism, but it's for
        order/inventory event notifications, not anything messaging-related
        despite the method name (see module docstring) — not wired here,
        same "no webhook route until a real need + confirmed signing exists"
        stance as most connectors in this build. send_message()/
        fetch_messages() are intentionally not overridden — the base
        class's default (not-supported / empty list) is the honest answer,
        confirmed via research rather than left unimplemented by omission."""
        return None

    def order_url(self, connection: MarketplaceConnection, external_order_id: str) -> Optional[str]:
        """LOW confidence — inferred seller-center order-detail pattern,
        not confirmed against a documented permalink spec."""
        return f"https://seller.temu.com/order/detail?orderSn={external_order_id}"


temu_connector = TemuConnector()

__all__ = ["TemuConnector", "temu_connector"]
