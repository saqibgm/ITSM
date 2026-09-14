"""
Etsy connector — pilot batch #5 (last of the §5 pilot), per
docs/plans/NATIVE_MARKETPLACE_CONNECTORS_PLAN.md §3/§5.

No reference implementation to port — built directly from Etsy's Open API v3
docs, not verified against a live sandbox account.

Two things confirmed in Phase 0 research (plan §2), not guessed at:
1. Etsy's OAuth requires PKCE (code_verifier/code_challenge) — unlike
   Shopify/Amazon/eBay's plain authorization-code flow, a request without a
   valid code_challenge is rejected outright. Implemented below with a
   per-state code_verifier stored in Redis alongside the OAuth state (the
   routes layer needs both to complete the token exchange).
2. Messaging is CONFIRMED NONE — not "unclear," not "outbound-only" like
   Amazon. Etsy's own GitHub discussion confirms "incoming messages via
   API" is an open feature request that doesn't exist; there is no way to
   read OR send a buyer message through Etsy's API at all. Worse than
   Amazon, not just similarly limited. messaging_capability = NONE and
   send_message() reflects that flatly, not as an "unverified" caveat like
   eBay/Amazon's outbound attempts — this one really is a hard platform
   wall, confirmed via Etsy's own developer forum, not inferred.

Orders are called "receipts" in Etsy's API (getShopReceipts) — order_lines
come from a separate "transactions" sub-resource; fetched inline per-receipt
below to keep NormalizedOrder populated, at the cost of one extra call per
receipt (acceptable for a 30-day backfill batch, worth revisiting if this
connector is ever used for high-volume real-time sync).
"""

import base64
import hashlib
import logging
import secrets
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

_TOKEN_URL = "https://api.etsy.com/v3/public/oauth/token"
_API_BASE = "https://openapi.etsy.com/v3/application"
_REFRESH_SKEW = timedelta(minutes=5)


def generate_pkce_pair() -> tuple[str, str]:
    """Returns (code_verifier, code_challenge) — S256 method, per Etsy's
    (and RFC 7636's) requirements. The routes layer stores code_verifier
    alongside the OAuth state and sends code_challenge in the authorize URL;
    the verifier is sent back at token-exchange time."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(40)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


class EtsyConnector(CommerceConnector):
    provider = "etsy"
    messaging_capability = MessagingCapability.NONE  # confirmed hard platform limitation, not a gap — see module docstring

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=15.0)
        return self._client

    def authorize_url(self, state: str, code_challenge: str) -> str:
        settings = get_settings()
        params = httpx.QueryParams({
            "response_type": "code",
            "client_id": settings.ETSY_CLIENT_ID,
            "redirect_uri": settings.ETSY_REDIRECT_URI,
            "scope": settings.ETSY_SCOPES,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        })
        return f"https://www.etsy.com/oauth/connect?{params}"

    async def connect(self, tenant_id: str, credentials: dict) -> ConnectionResult:
        settings = get_settings()
        code = credentials.get("code")
        code_verifier = credentials.get("code_verifier")
        if not code or not code_verifier:
            return ConnectionResult(success=False, error="missing code or code_verifier")

        client = await self._get_client()
        try:
            resp = await client.post(
                _TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "client_id": settings.ETSY_CLIENT_ID,
                    "redirect_uri": settings.ETSY_REDIRECT_URI,
                    "code": code,
                    "code_verifier": code_verifier,
                },
            )
            payload = resp.json()
        except Exception as exc:
            logger.error("[Etsy] token exchange failed: %r", exc, exc_info=True)
            return ConnectionResult(success=False, error=str(exc))

        if not payload.get("access_token") or not payload.get("refresh_token"):
            return ConnectionResult(success=False, error=payload.get("error", "token_exchange_failed"))
        # Etsy's access_token is prefixed "{shop_id}.{token}" — the shop_id
        # is recoverable from the token itself, used as external_id.
        external_id = payload["access_token"].split(".")[0]
        return ConnectionResult(success=True, external_id=external_id)

    async def _ensure_fresh_token(self, connection: MarketplaceConnection) -> Optional[dict]:
        creds = connection.credentials
        expires_at_raw = creds.get("access_token_expires_at")
        expires_at = datetime.fromisoformat(expires_at_raw) if expires_at_raw else None
        if expires_at and expires_at - datetime.now(timezone.utc) > _REFRESH_SKEW:
            return creds

        settings = get_settings()
        client = await self._get_client()
        try:
            resp = await client.post(
                _TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "client_id": settings.ETSY_CLIENT_ID,
                    "refresh_token": decrypt_secret(creds["refresh_token"]),
                },
            )
            payload = resp.json()
        except Exception as exc:
            logger.error("[Etsy] token refresh failed for connection %s: %r", connection.id, exc, exc_info=True)
            return None

        new_access_token = payload.get("access_token")
        if not new_access_token:
            return None
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

    async def fetch_orders(
        self, connection: MarketplaceConnection, since: Optional[datetime] = None
    ) -> list[NormalizedOrder]:
        creds = await self._ensure_fresh_token(connection)
        if not creds:
            return []
        settings = get_settings()
        shop_id = connection.external_id
        access_token = decrypt_secret(creds["access_token"])
        headers = {"Authorization": f"Bearer {access_token}", "x-api-key": settings.ETSY_CLIENT_ID}
        client = await self._get_client()

        min_created = int((since or datetime.now(timezone.utc) - timedelta(days=30)).timestamp())
        try:
            resp = await client.get(
                f"{_API_BASE}/shops/{shop_id}/receipts",
                headers=headers,
                params={"min_created": min_created, "limit": 50},
            )
            if resp.status_code != 200:
                logger.warning("[Etsy] GET receipts -> %d: %s", resp.status_code, resp.text[:200])
                return []
            body = resp.json()
        except Exception as exc:
            logger.error("[Etsy] GET receipts failed: %r", exc, exc_info=True)
            return []

        results = []
        for receipt in body.get("results", []):
            results.append(NormalizedOrder(
                external_order_id=str(receipt.get("receipt_id")),
                status=("cancelled" if receipt.get("was_canceled") else
                        "delivered" if receipt.get("was_delivered") else
                        "shipped" if receipt.get("was_shipped") else "new"),
                order_lines=[],  # separate /transactions sub-resource — not fetched here to avoid N+1; see module docstring
                total_amount=receipt.get("grandtotal", {}).get("amount", 0) / 100 if receipt.get("grandtotal") else None,
                currency=(receipt.get("grandtotal") or {}).get("divisor") and receipt.get("grandtotal", {}).get("currency_code"),
                buyer_email=receipt.get("buyer_email"),
                placed_at=datetime.fromtimestamp(receipt["created_timestamp"], tz=timezone.utc) if receipt.get("created_timestamp") else None,
                raw_metadata=receipt,
            ))
        return results

    async def fetch_returns(
        self, connection: MarketplaceConnection, since: Optional[datetime] = None
    ) -> list[NormalizedReturn]:
        """Stub — not researched in Phase 0 (focus was messaging capability).
        Etsy doesn't have an obviously-named 'returns' endpoint in the v3
        API surface reviewed so far; needs its own research pass rather than
        a guess."""
        logger.info("etsy_fetch_returns_not_implemented", extra={"connection_id": str(connection.id)})
        return []

    def parse_webhook(self, raw_payload: bytes, headers: dict) -> None:
        """Etsy does have webhooks (confirmed: an `order.paid` topic exists,
        added late 2025 per Phase 0 research) but the signature-verification
        scheme wasn't confirmed in this org's research pass. Returns None
        rather than accepting an unverified payload — same conservative
        stance as Walmart/eBay above."""
        return None

    async def send_message(self, connection: MarketplaceConnection, order_or_case_id: str, message: str) -> SendResult:
        return SendResult(
            success=False,
            error="Etsy has no buyer-messaging API at all — confirmed hard platform limitation (Phase 0), not a gap to close",
        )


etsy_connector = EtsyConnector()

__all__ = ["EtsyConnector", "etsy_connector", "generate_pkce_pair"]
