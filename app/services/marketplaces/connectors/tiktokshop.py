"""
TikTok Shop connector (2026-09-18) — part of the 7-marketplace confirmed
target scope (Amazon, Best Buy, Walmart, eBay, Shopify, Temu, TikTok Shop).

UNVERIFIED — no live seller account/credentials exist for this org's TikTok
Shop account. Built directly against real, fetched TikTok Shop Partner
Center documentation (partner.tiktokshop.com/docv2) and a third-party
production-implementation writeup for the parts whose official pages were
JS-rendered and couldn't be fetched directly. Endpoint PATHS below are
confirmed at varying confidence — flagged per-endpoint; the send-message
path specifically is constructed by convention (matching a confirmed
sibling endpoint's naming, `.../messages/send`) rather than directly
fetched, since its doc page didn't render for automated retrieval.

Confirmed via direct research (2026-09-18):
- Real, two-way Customer Service API for buyer-seller conversations —
  genuinely the best-documented messaging capability found in this whole
  build alongside eBay's new REST Message API:
  * POST /customer_service/202309/conversations — create/reopen a
    conversation with a buyer (buyer_user_id).
  * GET /customer_service/202309/conversations/{conversation_id}/messages
    — message history, seller.customer_service scope, page_size <= 10,
    next_page_token pagination. Confirmed exact path.
  * POST /customer_service/202309/messages/send — send a message.
    Confirmed to EXIST (its own dedicated doc page,
    partner.tiktokshop.com/docv2/page/send-message-202309) but the page
    itself was JS-rendered/unfetchable — path constructed by convention,
    not directly confirmed. Flagged in send_message() below.
  Access to this API is APPROVAL-GATED — "inactive by default," per
  TikTok's own docs — same category of business/approval blocker as
  several other connectors in this build, not an engineering gap.
- Real webhook mechanism, and unlike eBay's equivalent (this same batch),
  the notification PAYLOAD genuinely carries message content, not just a
  ping: PUT /event/202309/webhooks (event_type=NEW_MESSAGE) subscribes;
  delivered payload confirmed to include tts_notification_id, shop_id,
  message_id, conversation_id, index, create_time, type, visibility, and
  sender info — the actual message TEXT field's exact name wasn't
  confirmed in research, so normalize_event() below tries several
  plausible keys and falls back to a live GetConversationMessages call
  only if none match, rather than silently dropping the message.
- Orders: POST /order/202309/orders/search (confirmed path).
- Returns: NOT implemented — no returns/reverse-order endpoint was
  confirmed in this research pass. fetch_returns() is an honest stub
  (returns []) rather than a fabricated call against an unconfirmed
  endpoint, same stance as amazon.py's fetch_returns().
- Auth: standard OAuth2 authorization-code flow — authorize URL
  https://services.tiktokshop.com/open/authorize, token URL
  https://auth.tiktok-shops.com/api/v2/token/get (both confirmed). The
  token response is SELLER-level, not shop-level — GET
  /authorization/202309/shops (confirmed path) resolves the actual
  shop_id/shop_cipher a token can act on. This connector takes the FIRST
  authorized shop returned — a real simplification for sellers with
  multiple TikTok Shops under one account, flagged rather than hidden.
- Signing: HMAC-SHA256 over (app_secret + path + sorted, bare-
  concatenated QUERY params, excluding sign/access_token + app_secret),
  keyed by app_secret, uppercase hex — applied to every API call's query
  string (JSON body, where present, is NOT included in the signed
  string). Reconstructed from a paraphrased description of TikTok's own
  signing doc (page itself JS-rendered) cross-checked against the general
  shape common to this API family — flagged as the single most likely
  thing to need a live-tested correction, same caveat this codebase
  already gives Lazada's signing implementation.
- Webhook signature (DIFFERENT algorithm from API-request signing, per
  TikTok's own docs): Authorization header = HMAC-SHA256(app_secret,
  app_key + raw_body), lowercase hex.
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

_API_BASE = "https://open-api.tiktokglobalshop.com"
_AUTH_URL = "https://services.tiktokshop.com/open/authorize"
_TOKEN_URL = "https://auth.tiktok-shops.com/api/v2/token/get"
_REFRESH_SKEW = timedelta(minutes=5)


def _sign(path: str, query_params: dict, app_secret: str) -> str:
    """See module docstring for the confirmed-by-paraphrase algorithm and
    its uncertainty. sign/access_token are excluded from the signed set —
    both confirmed-common exclusions across this API family."""
    signable = {k: v for k, v in query_params.items() if k not in ("sign", "access_token")}
    sorted_items = sorted(signable.items())
    base = app_secret + path + "".join(f"{k}{v}" for k, v in sorted_items) + app_secret
    return hmac.new(app_secret.encode(), base.encode(), hashlib.sha256).hexdigest().upper()


class TikTokShopConnector(CommerceConnector):
    provider = "tiktokshop"
    # FULL — the Customer Service API is genuinely two-way (create, list,
    # send, real webhook). Unverified live and approval-gated, see module
    # docstring.
    messaging_capability = MessagingCapability.FULL

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=15.0)
        return self._client

    async def _call(
        self, method: str, path: str, app_key: str, app_secret: str,
        access_token: Optional[str] = None, shop_cipher: Optional[str] = None,
        extra_query: Optional[dict] = None, json_body: Optional[dict] = None,
    ) -> Optional[dict]:
        query = {"app_key": app_key, "timestamp": str(int(time.time())), **(extra_query or {})}
        if shop_cipher:
            query["shop_cipher"] = shop_cipher
        if access_token:
            query["access_token"] = access_token
        query["sign"] = _sign(path, query, app_secret)

        headers = {}
        if access_token:
            headers["x-tts-access-token"] = access_token

        client = await self._get_client()
        try:
            resp = await client.request(method, f"{_API_BASE}{path}", params=query, json=json_body, headers=headers)
            body = resp.json()
        except Exception as exc:
            logger.error("[TikTokShop] %s %s failed: %r", method, path, exc, exc_info=True)
            return None
        if body.get("code") not in (0, None):
            logger.warning("[TikTokShop] %s %s -> code=%s: %s", method, path, body.get("code"), body.get("message"))
        return body

    def authorize_url(self, state: str) -> str:
        settings = get_settings()
        params = httpx.QueryParams({
            "app_key": settings.TIKTOKSHOP_CLIENT_ID,
            "state": state,
            "redirect_uri": settings.TIKTOKSHOP_REDIRECT_URI,
        })
        return f"{_AUTH_URL}?{params}"

    async def connect(self, tenant_id: str, credentials: dict) -> ConnectionResult:
        settings = get_settings()
        code = credentials.get("code")
        if not code:
            return ConnectionResult(success=False, error="missing code")

        client = await self._get_client()
        try:
            resp = await client.get(_TOKEN_URL, params={
                "app_key": settings.TIKTOKSHOP_CLIENT_ID,
                "app_secret": settings.TIKTOKSHOP_CLIENT_SECRET,
                "auth_code": code,
                "grant_type": "authorized_code",
            })
            payload = resp.json()
        except Exception as exc:
            logger.error("[TikTokShop] token exchange failed: %r", exc, exc_info=True)
            return ConnectionResult(success=False, error=str(exc))

        data = payload.get("data") or {}
        access_token = data.get("access_token")
        if not access_token:
            return ConnectionResult(success=False, error=payload.get("message", "token_exchange_failed"))

        # Token is seller-level — resolve the actual shop(s) it can act on.
        # Takes the FIRST authorized shop (see module docstring).
        shops_body = await self._call(
            "GET", "/authorization/202309/shops", settings.TIKTOKSHOP_CLIENT_ID, settings.TIKTOKSHOP_CLIENT_SECRET,
            access_token=access_token,
        )
        shops = ((shops_body or {}).get("data") or {}).get("shops") or []
        if not shops:
            return ConnectionResult(success=False, error="no authorized shops returned for this token")
        shop = shops[0]

        result = {**data, "shop_id": shop.get("id"), "shop_cipher": shop.get("cipher")}
        return ConnectionResult(success=True, external_id=str(shop.get("id") or ""), credentials=result)

    async def _ensure_fresh_token(self, connection: MarketplaceConnection) -> Optional[dict]:
        creds = connection.credentials
        expires_at_raw = creds.get("access_token_expires_at")
        expires_at = datetime.fromisoformat(expires_at_raw) if expires_at_raw else None
        if expires_at and expires_at - datetime.now(timezone.utc) > _REFRESH_SKEW:
            return creds

        refresh_token = creds.get("refresh_token")
        if not refresh_token:
            logger.warning("[TikTokShop] connection %s has no refresh_token — must reconnect", connection.id)
            return None

        settings = get_settings()
        client = await self._get_client()
        try:
            resp = await client.get(_TOKEN_URL, params={
                "app_key": settings.TIKTOKSHOP_CLIENT_ID,
                "app_secret": settings.TIKTOKSHOP_CLIENT_SECRET,
                "refresh_token": decrypt_secret(refresh_token),
                "grant_type": "refresh_token",
            })
            payload = resp.json()
        except Exception as exc:
            logger.error("[TikTokShop] token refresh failed for connection %s: %r", connection.id, exc, exc_info=True)
            return None

        data = payload.get("data") or {}
        if not data.get("access_token"):
            return None
        creds = dict(creds)
        creds["access_token"] = encrypt_secret(data["access_token"])
        if data.get("refresh_token"):
            creds["refresh_token"] = encrypt_secret(data["refresh_token"])
        expires_in = data.get("access_token_expire_in")
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
        body = await self._call(
            "POST", "/order/202309/orders/search", settings.TIKTOKSHOP_CLIENT_ID, settings.TIKTOKSHOP_CLIENT_SECRET,
            access_token=decrypt_secret(creds["access_token"]), shop_cipher=creds.get("shop_cipher"),
            extra_query={"page_size": "50"}, json_body={},
        )
        if not body:
            return []

        orders = ((body.get("data") or {}).get("orders")) or []
        results = []
        for order in orders:
            payment = order.get("payment") or {}
            total = payment.get("total_amount")
            created_raw = order.get("create_time")
            recipient = order.get("recipient_address") or {}
            results.append(NormalizedOrder(
                external_order_id=str(order.get("id") or ""),
                status=str(order.get("status") or "new"),
                order_lines=[],  # out of scope for v1 — see module docstring
                total_amount=float(total) if total is not None else None,
                currency=payment.get("currency"),
                buyer_email=None,  # not confirmed available
                buyer_name=recipient.get("name"),
                placed_at=datetime.fromtimestamp(int(created_raw), tz=timezone.utc) if created_raw else None,
                # buyer_user_id (if present) is the key send_message() needs
                # as the Customer Service API's conversation target —
                # stashed here, not a NormalizedOrder first-class field.
                raw_metadata=order,
            ))
        return results

    async def fetch_returns(
        self, connection: MarketplaceConnection, since: Optional[datetime] = None
    ) -> list[NormalizedReturn]:
        """Stub — no returns/reverse-order endpoint was confirmed in this
        research pass (unlike orders, which was). Returns empty rather
        than fabricating a call against an unconfirmed endpoint, same
        stance as amazon.py's fetch_returns()."""
        logger.info(
            "tiktokshop_fetch_returns_not_implemented",
            extra={"connection_id": str(connection.id), "reason": "no confirmed returns endpoint in this research pass"},
        )
        return []

    def parse_webhook(self, raw_payload: bytes, headers: dict) -> None:
        """Not the live path — see marketplace_tiktokshop_webhook.py, which
        verifies the Authorization header itself (needs app_secret from
        settings, available at the route layer) and calls normalize_event()
        directly, same shape as eBay's real webhook route in this batch."""
        return None

    def _buyer_user_id(self, order: MarketplaceOrder) -> Optional[str]:
        buyer_id = (order.raw_metadata or {}).get("buyer_user_id") or (order.raw_metadata or {}).get("user_id")
        return str(buyer_id) if buyer_id else None

    async def _get_or_create_conversation(self, creds: dict, buyer_user_id: str) -> Optional[str]:
        settings = get_settings()
        body = await self._call(
            "POST", "/customer_service/202309/conversations", settings.TIKTOKSHOP_CLIENT_ID, settings.TIKTOKSHOP_CLIENT_SECRET,
            access_token=decrypt_secret(creds["access_token"]), shop_cipher=creds.get("shop_cipher"),
            json_body={"buyer_user_id": buyer_user_id},
        )
        if not body:
            return None
        data = body.get("data") or {}
        conversation_id = data.get("conversation_id")
        return str(conversation_id) if conversation_id else None

    async def send_message(self, connection: MarketplaceConnection, order: MarketplaceOrder, message: str) -> SendResult:
        buyer_user_id = self._buyer_user_id(order)
        if not buyer_user_id:
            return SendResult(success=False, error="no buyer_user_id on record for this order — cannot address the Customer Service API")

        creds = await self._ensure_fresh_token(connection)
        if not creds:
            return SendResult(success=False, error="could not refresh token")

        conversation_id = await self._get_or_create_conversation(creds, buyer_user_id)
        if not conversation_id:
            return SendResult(success=False, error="could not open a conversation for this buyer")

        settings = get_settings()
        # Path constructed by convention, not directly confirmed — see
        # module docstring.
        body = await self._call(
            "POST", "/customer_service/202309/messages/send", settings.TIKTOKSHOP_CLIENT_ID, settings.TIKTOKSHOP_CLIENT_SECRET,
            access_token=decrypt_secret(creds["access_token"]), shop_cipher=creds.get("shop_cipher"),
            json_body={"conversation_id": conversation_id, "content": message, "message_type": "TEXT"},
        )
        if not body or body.get("code") not in (0, None):
            return SendResult(success=False, error=(body or {}).get("message", "TikTok Shop returned an error"))
        data = body.get("data") or {}
        return SendResult(success=True, external_message_id=str(data.get("message_id")) if data.get("message_id") else None)

    async def fetch_messages(self, connection: MarketplaceConnection, order: MarketplaceOrder) -> list[NormalizedMessage]:
        buyer_user_id = self._buyer_user_id(order)
        if not buyer_user_id:
            return []
        creds = await self._ensure_fresh_token(connection)
        if not creds:
            return []

        conversation_id = await self._get_or_create_conversation(creds, buyer_user_id)
        if not conversation_id:
            return []

        settings = get_settings()
        body = await self._call(
            "GET", f"/customer_service/202309/conversations/{conversation_id}/messages",
            settings.TIKTOKSHOP_CLIENT_ID, settings.TIKTOKSHOP_CLIENT_SECRET,
            access_token=decrypt_secret(creds["access_token"]), shop_cipher=creds.get("shop_cipher"),
            extra_query={"page_size": "10"},
        )
        if not body:
            return []

        messages = ((body.get("data") or {}).get("messages")) or []
        results = []
        for msg in messages:
            sender = (msg.get("sender") or {})
            sender_id = str(sender.get("id") or sender.get("user_id") or "")
            created_raw = msg.get("create_time")
            content = msg.get("content") or msg.get("text") or ""
            results.append(NormalizedMessage(
                external_message_id=str(msg.get("id") or msg.get("message_id") or ""),
                external_order_id=order.external_order_id,
                external_case_id=conversation_id,
                body=content,
                sent_at=datetime.fromtimestamp(int(created_raw), tz=timezone.utc) if created_raw else None,
                raw_metadata={"direction": "inbound" if sender_id == buyer_user_id else "outbound"},
            ))
        return results

    # ------------------------------------------------------------------
    # Notification webhook — real, payload-carrying (2026-09-18), see
    # module docstring. register_webhooks() subscribes; normalize_event()
    # is the route's dispatch target.
    # ------------------------------------------------------------------

    async def register_webhooks(self, connection: MarketplaceConnection) -> None:
        creds = await self._ensure_fresh_token(connection)
        if not creds:
            logger.warning("[TikTokShop] register_webhooks: could not refresh token for connection %s", connection.id)
            return
        settings = get_settings()
        endpoint = f"{settings.TIKTOKSHOP_WEBHOOK_PUBLIC_URL}/api/v1/webhooks/marketplace/tiktokshop/notification"
        body = await self._call(
            "PUT", "/event/202309/webhooks", settings.TIKTOKSHOP_CLIENT_ID, settings.TIKTOKSHOP_CLIENT_SECRET,
            access_token=decrypt_secret(creds["access_token"]), shop_cipher=creds.get("shop_cipher"),
            json_body={"event_type": "NEW_MESSAGE", "address": endpoint},
        )
        if not body or body.get("code") not in (0, None):
            logger.warning("[TikTokShop] webhook subscription failed for connection %s: %s", connection.id, (body or {}).get("message"))
            return
        logger.info("[TikTokShop] registered NEW_MESSAGE webhook for connection %s", connection.id)

    async def normalize_event(
        self, event_type: str, payload: dict, *, db=None, tenant_id=None, connection: Optional[MarketplaceConnection] = None
    ) -> Optional[NormalizedMessage]:
        """Dispatch target for the NEW_MESSAGE webhook. The payload's
        message-TEXT field name wasn't confirmed in research (only
        metadata fields were) — tries several plausible keys, and if none
        are present, falls back to a live GetConversationMessages call
        (the same "notify then fetch" pattern eBay's connector in this
        same batch uses as its ONLY option) rather than dropping the
        message silently."""
        if event_type != "NEW_MESSAGE" or connection is None:
            return None

        conversation_id = str(payload.get("conversation_id") or "")
        message_id = str(payload.get("message_id") or "")
        sender = payload.get("sender") or {}
        sender_id = str(sender.get("id") or sender.get("user_id") or payload.get("sender_id") or "")
        created_raw = payload.get("create_time")
        sent_at = datetime.fromtimestamp(int(created_raw), tz=timezone.utc) if created_raw else None

        body_text = payload.get("content") or payload.get("text") or payload.get("message")
        if not body_text and conversation_id:
            creds = await self._ensure_fresh_token(connection)
            if creds:
                settings = get_settings()
                resp = await self._call(
                    "GET", f"/customer_service/202309/conversations/{conversation_id}/messages",
                    settings.TIKTOKSHOP_CLIENT_ID, settings.TIKTOKSHOP_CLIENT_SECRET,
                    access_token=decrypt_secret(creds["access_token"]), shop_cipher=creds.get("shop_cipher"),
                    extra_query={"page_size": "10"},
                )
                for msg in ((resp or {}).get("data") or {}).get("messages") or []:
                    if str(msg.get("id") or msg.get("message_id") or "") == message_id:
                        body_text = msg.get("content") or msg.get("text") or ""
                        break

        # No order.external_order_id correlation available from the
        # webhook payload alone (no order_id field confirmed) — left None;
        # ingestion's map_message_to_comment/map_fetched_message both
        # already degrade gracefully when external_order_id can't resolve
        # to a MarketplaceOrder (see tasks_marketplace_sync.py).
        return NormalizedMessage(
            external_message_id=message_id or None,
            external_order_id=None,
            external_case_id=conversation_id or None,
            body=body_text or "",
            sent_at=sent_at,
            raw_metadata={"direction": "inbound", "sender_id": sender_id},
        )

    def order_url(self, connection: MarketplaceConnection, external_order_id: str) -> Optional[str]:
        """LOW-MEDIUM confidence — inferred Seller Center order-detail
        pattern, not confirmed against a documented permalink spec."""
        return f"https://seller-us.tiktok.com/order/detail?order_id={external_order_id}"


tiktokshop_connector = TikTokShopConnector()

__all__ = ["TikTokShopConnector", "tiktokshop_connector"]
