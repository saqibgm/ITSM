"""
Lazada connector — MESSAGING-ONLY scope (2026-09-15), same rationale as
mercadolibre.py/allegro.py/cdiscount.py's module docstrings. fetch_orders()
exists only to anchor a MarketplaceOrder row for messages to attach to —
no returns sync, no order_lines detail.

UNVERIFIED — no live sandbox/credentials exist for this org's Lazada
account, and unlike the other connectors in this batch, Lazada's API uses
a request-signing scheme (not just a Bearer token) that is genuinely easy
to get subtly wrong — the most likely connector in this batch to need a
real fix once live-tested. Treat everything here accordingly.

Confirmed via direct research (2026-09-15):
- Real, two-way, order-tied Instant Messaging (IM) API:
  POST /im/session/open (creates a chat session FROM an order_id — the
  order must be 30 days old or newer, a hard platform constraint, not a
  choice made here), POST /im/message/send, GET /im/message/list,
  GET /im/session/list, GET /im/session/get, POST /im/session/read,
  POST /im/message/recall. All confirmed as real documented paths on
  open.lazada.com (via a doc mirror closely tracking the official
  structure — the official page bodies themselves are JS-rendered and
  couldn't be fetched directly, so treat exact request/response FIELD
  NAMES as less certain than the endpoint paths themselves).
- No buyer-note or buyer-email field confirmed on the order schema —
  none populated here (None/empty), not guessed.
- Auth is TWO layers, both real and both need to be right:
  1. OAuth-style authorization-code consent flow (auth.lazada.com) for
     the access_token itself.
  2. EVERY API call (including the token exchange/refresh calls
     themselves, per Lazada's TOP-API-style convention) must also be
     HMAC-SHA256 REQUEST-SIGNED: sort all params alphabetically by key,
     concatenate as bare key+value pairs (no '=' or '&'), prepend the
     API path, HMAC-SHA256 the result with app_secret as the key, hex-
     encode in UPPERCASE. Confirmed algorithm via Lazada's own "Signing
     requests" doc page (lazada-sellercenter.readme.io/docs/signing-
     requests) plus a corroborating open-source signer implementation.
     Getting this signing step wrong makes EVERY call fail with a
     signature-mismatch error, not just messaging ones — this is the
     single most likely thing to need correction once real credentials
     exist.
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

_AUTH_URL = "https://auth.lazada.com/oauth/authorize"
_API_BASE = "https://api.lazada.com/rest"
_REFRESH_SKEW = timedelta(minutes=5)


def _sign(path: str, params: dict, app_secret: str) -> str:
    """HMAC-SHA256 over the API path + sorted, bare-concatenated params —
    see module docstring for the confirmed algorithm and its source."""
    sorted_items = sorted(params.items())
    concatenated = path + "".join(f"{k}{v}" for k, v in sorted_items)
    digest = hmac.new(app_secret.encode(), concatenated.encode(), hashlib.sha256).hexdigest()
    return digest.upper()


class LazadaConnector(CommerceConnector):
    provider = "lazada"
    # FULL — the IM API is genuinely two-way (send + list), confirmed via
    # real doc paths. Unverified live, see module docstring — more so
    # than usual given the HMAC-signing uncertainty.
    messaging_capability = MessagingCapability.FULL

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=15.0)
        return self._client

    async def _signed_request(self, path: str, app_key: str, app_secret: str, extra_params: dict) -> Optional[dict]:
        """Every Lazada API call goes through here — builds the common
        signed-request envelope (app_key, timestamp, sign, sign_method)
        plus whatever params the specific call needs."""
        params = {
            "app_key": app_key,
            "timestamp": str(int(time.time() * 1000)),
            "sign_method": "sha256",
            **extra_params,
        }
        params["sign"] = _sign(path, params, app_secret)
        client = await self._get_client()
        try:
            resp = await client.post(f"{_API_BASE}{path}", data=params)
            body = resp.json()
        except Exception as exc:
            logger.error("[Lazada] request to %s failed: %r", path, exc, exc_info=True)
            return None
        if body.get("code") not in ("0", 0, None):
            logger.warning("[Lazada] %s -> code=%s: %s", path, body.get("code"), body.get("message"))
        return body

    def authorize_url(self, state: str) -> str:
        settings = get_settings()
        params = httpx.QueryParams({
            "response_type": "code",
            "force_auth": "true",
            "client_id": settings.LAZADA_CLIENT_ID,
            "redirect_uri": settings.LAZADA_REDIRECT_URI,
            "state": state,
        })
        return f"{_AUTH_URL}?{params}"

    async def connect(self, tenant_id: str, credentials: dict) -> ConnectionResult:
        settings = get_settings()
        code = credentials.get("code")
        if not code:
            return ConnectionResult(success=False, error="missing code")

        payload = await self._signed_request(
            "/auth/token/create", settings.LAZADA_CLIENT_ID, settings.LAZADA_CLIENT_SECRET, {"code": code},
        )
        if not payload or not payload.get("access_token"):
            return ConnectionResult(success=False, error=(payload or {}).get("message", "token_exchange_failed"))
        return ConnectionResult(success=True, external_id=str(payload.get("seller_id", "")), credentials=payload)

    async def _ensure_fresh_token(self, connection: MarketplaceConnection) -> Optional[dict]:
        creds = connection.credentials
        expires_at_raw = creds.get("access_token_expires_at")
        expires_at = datetime.fromisoformat(expires_at_raw) if expires_at_raw else None
        if expires_at and expires_at - datetime.now(timezone.utc) > _REFRESH_SKEW:
            return creds

        refresh_token = creds.get("refresh_token")
        if not refresh_token:
            logger.warning("[Lazada] connection %s has no refresh_token — must reconnect", connection.id)
            return None

        settings = get_settings()
        payload = await self._signed_request(
            "/auth/token/refresh", settings.LAZADA_CLIENT_ID, settings.LAZADA_CLIENT_SECRET,
            {"refresh_token": decrypt_secret(refresh_token)},
        )
        if not payload or not payload.get("access_token"):
            return None
        creds = dict(creds)
        creds["access_token"] = encrypt_secret(payload["access_token"])
        if payload.get("refresh_token"):
            creds["refresh_token"] = encrypt_secret(payload["refresh_token"])
        expires_in = payload.get("expires_in")
        if expires_in:
            creds["access_token_expires_at"] = (
                datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
            ).isoformat()
        return creds

    async def fetch_orders(
        self, connection: MarketplaceConnection, since: Optional[datetime] = None
    ) -> list[NormalizedOrder]:
        """Messaging-only scope — minimal order fetch, just enough to
        anchor a MarketplaceOrder row for an IM session to reference."""
        creds = await self._ensure_fresh_token(connection)
        if not creds:
            return []
        settings = get_settings()
        payload = await self._signed_request(
            "/orders/get", settings.LAZADA_CLIENT_ID, settings.LAZADA_CLIENT_SECRET,
            {"access_token": decrypt_secret(creds["access_token"]), "sort_by": "created_at", "sort_direction": "DESC"},
        )
        if not payload:
            return []

        results = []
        for order in (payload.get("data") or {}).get("orders", []):
            total = order.get("price")
            results.append(NormalizedOrder(
                external_order_id=str(order.get("order_id")),
                status=str(order.get("statuses", ["new"])[0] if order.get("statuses") else "new"),
                order_lines=[],  # out of scope — see module docstring
                total_amount=float(total) if total is not None else None,
                currency=order.get("currency") or order.get("price_currency"),
                buyer_email=None,   # not confirmed available — see module docstring
                buyer_name=order.get("customer_first_name") or order.get("address_shipping", {}).get("first_name"),
                placed_at=datetime.fromisoformat(order["created_at"].replace("Z", "+00:00")) if order.get("created_at") else None,
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

    async def _open_session(self, connection: MarketplaceConnection, creds: dict, order: MarketplaceOrder) -> Optional[str]:
        """IM sessions are opened FROM an order_id (confirmed) — the 30-day
        age limit is a real, documented platform constraint, not something
        this code enforces or works around."""
        settings = get_settings()
        payload = await self._signed_request(
            "/im/session/open", settings.LAZADA_CLIENT_ID, settings.LAZADA_CLIENT_SECRET,
            {"access_token": decrypt_secret(creds["access_token"]), "order_id": order.external_order_id},
        )
        if not payload:
            return None
        data = payload.get("data") or {}
        return str(data.get("session_id")) if data.get("session_id") else None

    async def send_message(self, connection: MarketplaceConnection, order: MarketplaceOrder, message: str) -> SendResult:
        creds = await self._ensure_fresh_token(connection)
        if not creds:
            return SendResult(success=False, error="could not refresh token")

        session_id = await self._open_session(connection, creds, order)
        if not session_id:
            return SendResult(success=False, error="could not open an IM session for this order (order may be older than Lazada's 30-day messaging window)")

        settings = get_settings()
        payload = await self._signed_request(
            "/im/message/send", settings.LAZADA_CLIENT_ID, settings.LAZADA_CLIENT_SECRET,
            {"access_token": decrypt_secret(creds["access_token"]), "session_id": session_id, "template_id": "1", "content": message},
        )
        if not payload or payload.get("code") not in ("0", 0, None):
            return SendResult(success=False, error=(payload or {}).get("message", "Lazada returned an error"))
        data = payload.get("data") or {}
        return SendResult(success=True, external_message_id=str(data.get("message_id")) if data.get("message_id") else None)

    async def fetch_messages(self, connection: MarketplaceConnection, order: MarketplaceOrder) -> list[NormalizedMessage]:
        creds = await self._ensure_fresh_token(connection)
        if not creds:
            return []
        session_id = await self._open_session(connection, creds, order)
        if not session_id:
            return []

        settings = get_settings()
        payload = await self._signed_request(
            "/im/message/list", settings.LAZADA_CLIENT_ID, settings.LAZADA_CLIENT_SECRET,
            {"access_token": decrypt_secret(creds["access_token"]), "session_id": session_id},
        )
        if not payload:
            return []

        results = []
        for msg in (payload.get("data") or {}).get("messages", []):
            # sender/from field name UNCONFIRMED — "from_account_type" is a
            # reasonable guess (buyer-vs-seller enum), not verbatim-documented.
            sender_type = str(msg.get("from_account_type", "")).upper()
            results.append(NormalizedMessage(
                external_message_id=str(msg.get("message_id")) if msg.get("message_id") else None,
                external_order_id=order.external_order_id,
                external_case_id=session_id,
                body=msg.get("content") or "",
                sent_at=datetime.fromtimestamp(int(msg["created_at"]) / 1000, tz=timezone.utc) if msg.get("created_at") else None,
                raw_metadata={"direction": "inbound" if sender_type == "BUYER" else "outbound"},
            ))
        return results

    def order_url(self, connection: MarketplaceConnection, external_order_id: str) -> Optional[str]:
        """LOW-MEDIUM confidence — inferred seller-center order-detail
        pattern, not confirmed against a documented permalink spec."""
        return f"https://sellercenter.lazada.com/apps/order/detail?order_id={external_order_id}"


lazada_connector = LazadaConnector()

__all__ = ["LazadaConnector", "lazada_connector"]
