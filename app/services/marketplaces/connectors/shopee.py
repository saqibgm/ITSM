"""
Shopee connector — MESSAGING-ONLY scope (2026-09-16), same rationale as
mercadolibre.py/allegro.py/etc.'s module docstrings. fetch_orders() exists
only to anchor a MarketplaceOrder row for messages to attach to (buyer
identity, buyer_user_id for chat) — no returns sync, no order_lines detail.

UNVERIFIED — no live sandbox/credentials exist for this org's Shopee
account. This connector went through TWO research passes: the first
(2026-09-15) only reached secondary sources and a generic "Chat API
exists, endpoints unclear" finding — not solid enough to build against.
The second pass (2026-09-16, after the user asked to actually build this
one) found REAL, code-level confirmation from an actual open-source SDK's
TypeScript source (endpoint decorators, not prose descriptions) — the
endpoint paths and request/response fields below come from that, a
meaningfully higher-confidence source than most of this batch's other
connectors, though still not a live sandbox test.

Confirmed via direct research (2026-09-16):
- Real, two-way, buyer-scoped messaging — Shopee's "Seller Chat" API
  (`/api/v2/sellerchat/*`, NOT the older/differently-named `/im/*` RPC
  style seen in a different, likely outdated SDK — the sellerchat
  namespace matches Shopee's current v2 API convention and is the one
  used here):
  * sendMessage — POST /api/v2/sellerchat/send_message. Body: to_id
    (buyer's Shopee user ID — NOT an order_id or session_id), message_type,
    content. To reference a specific order in the message, message_type
    can be 'order' with order_sn embedded in content — confirmed via SDK
    source, exact content sub-shape for the 'order' type not independently
    verified.
  * getMessage — GET /api/v2/sellerchat/get_message?conversation_id=...
  * getConversationList — GET /api/v2/sellerchat/get_conversation_list
  * getOneConversation — GET /api/v2/sellerchat/get_one_conversation
  Like Wildberries, chat here is BUYER-SCOPED (to_id = buyer's user ID),
  not order-scoped by a session — but unlike Wildberries, the buyer's
  user ID is a CONFIRMED real field on the order itself
  (buyer_user_id, via get_order_detail), so there's no fuzzy name-
  matching needed to find who to message — a real reliability advantage
  over Wildberries' connector in this same batch.
- No buyer-note or buyer-email field found on the order schema — none
  populated here.
- Auth: HMAC-SHA256 request signing on EVERY call (including the
  initial authorize redirect itself, unusually) — sign = HMAC-SHA256(
  partner_key, partner_id + path + timestamp [+ access_token + shop_id
  for shop-level calls]), hex digest, passed as the `sign` query param
  alongside partner_id/timestamp/access_token/shop_id. Authorize URL
  confirmed: https://partner.shopeemobile.com/api/v2/shop/auth_partner
  (5-minute link validity). Token exchange endpoint path
  (/api/v2/auth/token/get) matches Shopee's documented namespacing
  convention but wasn't independently confirmed the way the messaging
  endpoints were — flagged as the one still-uncertain piece. access_token
  expires every 4 hours.
"""

import hashlib
import hmac
import logging
import time
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

_AUTH_HOST = "https://partner.shopeemobile.com"
_API_BASE = "https://partner.shopeemobile.com"
_TOKEN_TTL_SKEW = timedelta(minutes=10)  # access_token lasts 4h — generous skew, no sub-5-min risk like Cdiscount/Lazada


def _sign(partner_key: str, path: str, partner_id: str, timestamp: int, access_token: str = "", shop_id: str = "") -> str:
    """HMAC-SHA256 over partner_id+path+timestamp[+access_token+shop_id] —
    see module docstring for the confirmed algorithm shape. Public/no-auth
    calls (like the authorize redirect) omit access_token/shop_id from
    the base string."""
    base = f"{partner_id}{path}{timestamp}{access_token}{shop_id}"
    return hmac.new(partner_key.encode(), base.encode(), hashlib.sha256).hexdigest()


class ShopeeConnector(CommerceConnector):
    provider = "shopee"
    # FULL — sellerchat is genuinely two-way (send + list + read), real
    # code-level confirmation (see module docstring). Unverified live.
    messaging_capability = MessagingCapability.FULL

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=15.0)
        return self._client

    def authorize_url(self, state: str) -> str:
        settings = get_settings()
        timestamp = int(time.time())
        path = "/api/v2/shop/auth_partner"
        sign = _sign(settings.SHOPEE_PARTNER_KEY, path, settings.SHOPEE_PARTNER_ID, timestamp)
        params = httpx.QueryParams({
            "partner_id": settings.SHOPEE_PARTNER_ID,
            "redirect": f"{settings.SHOPEE_REDIRECT_URI}?state={state}",
            "timestamp": str(timestamp),
            "sign": sign,
        })
        return f"{_AUTH_HOST}{path}?{params}"

    async def connect(self, tenant_id: str, credentials: dict) -> ConnectionResult:
        settings = get_settings()
        code = credentials.get("code")
        shop_id = credentials.get("shop_id")
        if not code or not shop_id:
            return ConnectionResult(success=False, error="missing code or shop_id")

        timestamp = int(time.time())
        path = "/api/v2/auth/token/get"  # UNCONFIRMED exact path — see module docstring
        sign = _sign(settings.SHOPEE_PARTNER_KEY, path, settings.SHOPEE_PARTNER_ID, timestamp)
        client = await self._get_client()
        try:
            resp = await client.post(
                f"{_API_BASE}{path}",
                params={"partner_id": settings.SHOPEE_PARTNER_ID, "timestamp": str(timestamp), "sign": sign},
                json={"code": code, "shop_id": int(shop_id), "partner_id": int(settings.SHOPEE_PARTNER_ID)},
            )
            payload = resp.json()
        except Exception as exc:
            logger.error("[Shopee] token exchange failed: %r", exc, exc_info=True)
            return ConnectionResult(success=False, error=str(exc))

        if not payload.get("access_token"):
            return ConnectionResult(success=False, error=payload.get("message", "token_exchange_failed"))
        payload["shop_id"] = shop_id
        return ConnectionResult(success=True, external_id=str(shop_id), credentials=payload)

    async def _ensure_fresh_token(self, connection: MarketplaceConnection) -> Optional[dict]:
        creds = connection.credentials
        expires_at_raw = creds.get("access_token_expires_at")
        expires_at = datetime.fromisoformat(expires_at_raw) if expires_at_raw else None
        if expires_at and expires_at - datetime.now(timezone.utc) > _TOKEN_TTL_SKEW:
            return creds

        refresh_token = creds.get("refresh_token")
        if not refresh_token:
            logger.warning("[Shopee] connection %s has no refresh_token — must reconnect", connection.id)
            return None

        settings = get_settings()
        timestamp = int(time.time())
        path = "/api/v2/auth/access_token/get"
        sign = _sign(settings.SHOPEE_PARTNER_KEY, path, settings.SHOPEE_PARTNER_ID, timestamp)
        client = await self._get_client()
        try:
            resp = await client.post(
                f"{_API_BASE}{path}",
                params={"partner_id": settings.SHOPEE_PARTNER_ID, "timestamp": str(timestamp), "sign": sign},
                json={"refresh_token": decrypt_secret(refresh_token), "shop_id": int(creds["shop_id"]), "partner_id": int(settings.SHOPEE_PARTNER_ID)},
            )
            payload = resp.json()
        except Exception as exc:
            logger.error("[Shopee] token refresh failed for connection %s: %r", connection.id, exc, exc_info=True)
            return None

        new_access_token = payload.get("access_token")
        if not new_access_token:
            return None
        creds = dict(creds)
        creds["access_token"] = encrypt_secret(new_access_token)
        if payload.get("refresh_token"):
            creds["refresh_token"] = encrypt_secret(payload["refresh_token"])
        expires_in = payload.get("expire_in") or payload.get("expires_in")
        if expires_in:
            creds["access_token_expires_at"] = (
                datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
            ).isoformat()
        return creds

    async def _signed_get(self, connection: MarketplaceConnection, creds: dict, path: str, extra_params: dict) -> Optional[dict]:
        settings = get_settings()
        timestamp = int(time.time())
        sign = _sign(settings.SHOPEE_PARTNER_KEY, path, settings.SHOPEE_PARTNER_ID, timestamp, decrypt_secret(creds["access_token"]), str(creds["shop_id"]))
        client = await self._get_client()
        try:
            resp = await client.get(
                f"{_API_BASE}{path}",
                params={
                    "partner_id": settings.SHOPEE_PARTNER_ID, "timestamp": str(timestamp), "sign": sign,
                    "access_token": decrypt_secret(creds["access_token"]), "shop_id": creds["shop_id"],
                    **extra_params,
                },
            )
            if resp.status_code != 200:
                logger.warning("[Shopee] GET %s -> %d: %s", path, resp.status_code, resp.text[:200])
                return None
            return resp.json()
        except Exception as exc:
            logger.error("[Shopee] GET %s failed: %r", path, exc, exc_info=True)
            return None

    async def _signed_post(self, connection: MarketplaceConnection, creds: dict, path: str, body: dict) -> Optional[dict]:
        settings = get_settings()
        timestamp = int(time.time())
        sign = _sign(settings.SHOPEE_PARTNER_KEY, path, settings.SHOPEE_PARTNER_ID, timestamp, decrypt_secret(creds["access_token"]), str(creds["shop_id"]))
        client = await self._get_client()
        try:
            resp = await client.post(
                f"{_API_BASE}{path}",
                params={
                    "partner_id": settings.SHOPEE_PARTNER_ID, "timestamp": str(timestamp), "sign": sign,
                    "access_token": decrypt_secret(creds["access_token"]), "shop_id": creds["shop_id"],
                },
                json=body,
            )
            if resp.status_code != 200:
                logger.warning("[Shopee] POST %s -> %d: %s", path, resp.status_code, resp.text[:200])
                return None
            return resp.json()
        except Exception as exc:
            logger.error("[Shopee] POST %s failed: %r", path, exc, exc_info=True)
            return None

    async def fetch_orders(
        self, connection: MarketplaceConnection, since: Optional[datetime] = None
    ) -> list[NormalizedOrder]:
        """Messaging-only scope — minimal order fetch. buyer_user_id
        (confirmed real field, see module docstring) is the key thing
        captured here — it's what send_message needs as to_id."""
        creds = await self._ensure_fresh_token(connection)
        if not creds:
            return []
        body = await self._signed_get(connection, creds, "/api/v2/order/get_order_list", {"time_range_field": "create_time", "page_size": 50})
        if not body:
            return []
        order_sns = [o["order_sn"] for o in (body.get("response") or {}).get("order_list", []) if o.get("order_sn")]
        if not order_sns:
            return []

        detail_body = await self._signed_get(
            connection, creds, "/api/v2/order/get_order_detail",
            {"order_sn_list": ",".join(order_sns), "response_optional_fields": "buyer_user_id,buyer_username,total_amount,order_status,create_time"},
        )
        if not detail_body:
            return []

        results = []
        for order in (detail_body.get("response") or {}).get("order_list", []):
            results.append(NormalizedOrder(
                external_order_id=str(order.get("order_sn")),
                status=str(order.get("order_status") or "new").lower(),
                order_lines=[],  # out of scope — see module docstring
                total_amount=float(order["total_amount"]) if order.get("total_amount") is not None else None,
                currency=order.get("currency"),
                buyer_email=None,  # not exposed — see module docstring
                buyer_name=order.get("buyer_username"),
                placed_at=datetime.fromtimestamp(order["create_time"], tz=timezone.utc) if order.get("create_time") else None,
                # buyer_user_id stashed in raw_metadata — needed as
                # send_message's to_id, not part of NormalizedOrder's
                # own fields.
                raw_metadata=order,
            ))
        return results

    async def fetch_returns(
        self, connection: MarketplaceConnection, since: Optional[datetime] = None
    ) -> list[NormalizedReturn]:
        """Out of scope — messaging-only connector, see module docstring."""
        return []

    def parse_webhook(self, raw_payload: bytes, headers: dict) -> None:
        return None

    def _buyer_user_id(self, order: MarketplaceOrder) -> Optional[int]:
        buyer_id = (order.raw_metadata or {}).get("buyer_user_id")
        return int(buyer_id) if buyer_id else None

    async def send_message(self, connection: MarketplaceConnection, order: MarketplaceOrder, message: str) -> SendResult:
        buyer_user_id = self._buyer_user_id(order)
        if not buyer_user_id:
            return SendResult(success=False, error="no buyer_user_id on record for this order — cannot address the Seller Chat API")

        creds = await self._ensure_fresh_token(connection)
        if not creds:
            return SendResult(success=False, error="could not refresh token")

        body = await self._signed_post(
            connection, creds, "/api/v2/sellerchat/send_message",
            {"to_id": buyer_user_id, "message_type": "text", "content": {"text": message}},
        )
        if not body or body.get("error"):
            return SendResult(success=False, error=(body or {}).get("message", "Shopee returned an error"))
        resp_data = body.get("response") or {}
        return SendResult(success=True, external_message_id=str(resp_data.get("message_id")) if resp_data.get("message_id") else None)

    async def fetch_messages(self, connection: MarketplaceConnection, order: MarketplaceOrder) -> list[NormalizedMessage]:
        buyer_user_id = self._buyer_user_id(order)
        if not buyer_user_id:
            return []
        creds = await self._ensure_fresh_token(connection)
        if not creds:
            return []

        # getConversationList doesn't take a buyer filter per the SDK
        # source found — fetch the list and match client-side, same
        # "no confirmed server-side filter" caution as Allegro's thread
        # lookup in this same batch.
        conv_body = await self._signed_get(connection, creds, "/api/v2/sellerchat/get_conversation_list", {"direction": "latest", "page_size": 50})
        if not conv_body:
            return []
        conversation_id = None
        for conv in (conv_body.get("response") or {}).get("conversations", []):
            if str(conv.get("to_id")) == str(buyer_user_id):
                conversation_id = conv.get("conversation_id")
                break
        if not conversation_id:
            return []

        msg_body = await self._signed_get(connection, creds, "/api/v2/sellerchat/get_message", {"conversation_id": conversation_id, "page_size": 50})
        if not msg_body:
            return []

        results = []
        for msg in (msg_body.get("response") or {}).get("messages", []):
            from_id = str(msg.get("from_id", ""))
            results.append(NormalizedMessage(
                external_message_id=str(msg.get("message_id")) if msg.get("message_id") else None,
                external_order_id=order.external_order_id,
                external_case_id=str(conversation_id),
                body=(msg.get("content") or {}).get("text") or "",
                sent_at=datetime.fromtimestamp(msg["created_timestamp"], tz=timezone.utc) if msg.get("created_timestamp") else None,
                raw_metadata={"direction": "inbound" if from_id == str(buyer_user_id) else "outbound"},
            ))
        return results

    def order_url(self, connection: MarketplaceConnection, external_order_id: str) -> Optional[str]:
        """LOW-MEDIUM confidence — inferred seller-center order pattern,
        not confirmed against a documented permalink spec."""
        return f"https://seller.shopee.com/portal/sale/order/{external_order_id}"


shopee_connector = ShopeeConnector()

__all__ = ["ShopeeConnector", "shopee_connector"]
