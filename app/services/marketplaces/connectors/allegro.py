"""
Allegro connector — MESSAGING-ONLY scope (2026-09-15), same rationale as
mercadolibre.py's module docstring (explicit request to add real buyer
communication for marketplaces beyond the original 5-connector pilot
batch). fetch_orders() here exists only to anchor a MarketplaceOrder row
for messages to attach to — no returns sync, no order_lines detail.

UNVERIFIED — no live sandbox/credentials exist for this org's Allegro
account. Written directly against real, cited documentation — same
starting point every pilot-batch connector had before live sandbox
testing caught real docs-vs-reality gaps. Treat every endpoint/field name
here as needing the same kind of live check before trusting it in
production.

Confirmed via direct research (2026-09-15), pulled directly from
developer.allegro.pl (not inferred):
- Real, two-way, order-tied messaging API — the "Centrum wiadomości"
  (Messaging Center) resource. GET /messaging/threads (list threads),
  GET/POST /messaging/threads/{threadId}/messages (read/reply within an
  existing thread), POST /messaging/messages (send — ambiguous in the
  docs whether this can ORIGINATE a new thread or only reply; implemented
  here as reply-to-existing-thread only, the safer reading). Messages
  relate to an order via a relatesTo.order.id field.
- Two API versions exist: public.v1 (stable, used here) and beta.v1 (adds
  post-purchase issue handling). A real bug was found in beta.v1 during
  research — messages sent via a seller's email reply come back with a
  bare "USER" role (no buyer/seller distinction) — avoided by using
  public.v1 only.
- messageToSeller — a real, checkout-time buyer note field, same category
  as Etsy's message_from_buyer / eBay's buyerCheckoutNotes (one-time,
  not a live channel). Confirmed on the checkout-form/order object.
- buyer.email — a real, usable buyer email confirmed on the order object
  (unlike eBay/Amazon, no PII-gating mentioned in the docs found).
- OAuth 2.0 with a GENUINE separate sandbox environment — auth and API
  hosts both switch to an entirely different domain
  (*.allegrosandbox.pl), not just a header/flag on production, same
  pattern this org's Walmart connector already handles for its own
  sandbox/production split.
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

_REFRESH_SKEW = timedelta(minutes=5)


def _base_urls(environment: str) -> tuple[str, str]:
    """Returns (auth_base, api_base) — Allegro's sandbox is a genuinely
    separate domain for BOTH auth and API, not a flag on production."""
    if environment == "sandbox":
        return "https://allegro.pl.allegrosandbox.pl", "https://api.allegro.pl.allegrosandbox.pl"
    return "https://allegro.pl", "https://api.allegro.pl"


class AllegroConnector(CommerceConnector):
    provider = "allegro"
    # FULL — the Messaging Center is genuinely two-way (list/read/reply),
    # confirmed via Allegro's own docs. Unverified live, see module docstring.
    messaging_capability = MessagingCapability.FULL

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=15.0)
        return self._client

    def authorize_url(self, state: str) -> str:
        settings = get_settings()
        auth_base, _ = _base_urls(settings.ALLEGRO_ENVIRONMENT)
        params = httpx.QueryParams({
            "response_type": "code",
            "client_id": settings.ALLEGRO_CLIENT_ID,
            "redirect_uri": settings.ALLEGRO_REDIRECT_URI,
            "state": state,
        })
        return f"{auth_base}/auth/oauth/authorize?{params}"

    async def connect(self, tenant_id: str, credentials: dict) -> ConnectionResult:
        settings = get_settings()
        code = credentials.get("code")
        if not code:
            return ConnectionResult(success=False, error="missing code")

        auth_base, _ = _base_urls(settings.ALLEGRO_ENVIRONMENT)
        client = await self._get_client()
        try:
            resp = await client.post(
                f"{auth_base}/auth/oauth/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": settings.ALLEGRO_REDIRECT_URI,
                },
                auth=(settings.ALLEGRO_CLIENT_ID, settings.ALLEGRO_CLIENT_SECRET),
            )
            payload = resp.json()
        except Exception as exc:
            logger.error("[Allegro] token exchange failed: %r", exc, exc_info=True)
            return ConnectionResult(success=False, error=str(exc))

        if not payload.get("access_token"):
            return ConnectionResult(success=False, error=payload.get("error", "token_exchange_failed"))
        return ConnectionResult(success=True, credentials=payload)

    async def _ensure_fresh_token(self, connection: MarketplaceConnection) -> Optional[dict]:
        creds = connection.credentials
        expires_at_raw = creds.get("access_token_expires_at")
        expires_at = datetime.fromisoformat(expires_at_raw) if expires_at_raw else None
        if expires_at and expires_at - datetime.now(timezone.utc) > _REFRESH_SKEW:
            return creds

        refresh_token = creds.get("refresh_token")
        if not refresh_token:
            logger.warning("[Allegro] connection %s has no refresh_token — must reconnect", connection.id)
            return None

        settings = get_settings()
        auth_base, _ = _base_urls(settings.ALLEGRO_ENVIRONMENT)
        client = await self._get_client()
        try:
            resp = await client.post(
                f"{auth_base}/auth/oauth/token",
                data={"grant_type": "refresh_token", "refresh_token": decrypt_secret(refresh_token)},
                auth=(settings.ALLEGRO_CLIENT_ID, settings.ALLEGRO_CLIENT_SECRET),
            )
            payload = resp.json()
        except Exception as exc:
            logger.error("[Allegro] token refresh failed for connection %s: %r", connection.id, exc, exc_info=True)
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
        """Messaging-only scope — minimal order fetch (checkout-forms
        resource), just enough to anchor a MarketplaceOrder row and carry
        the buyer_note (messageToSeller)."""
        creds = await self._ensure_fresh_token(connection)
        if not creds:
            return []
        settings = get_settings()
        _, api_base = _base_urls(settings.ALLEGRO_ENVIRONMENT)
        client = await self._get_client()
        try:
            resp = await client.get(
                f"{api_base}/order/checkout-forms",
                headers={
                    "Authorization": f"Bearer {decrypt_secret(creds['access_token'])}",
                    "Accept": "application/vnd.allegro.public.v1+json",
                },
            )
            if resp.status_code != 200:
                logger.warning("[Allegro] GET checkout-forms -> %d: %s", resp.status_code, resp.text[:200])
                return []
            body = resp.json()
        except Exception as exc:
            logger.error("[Allegro] GET checkout-forms failed: %r", exc, exc_info=True)
            return []

        results = []
        for order in body.get("checkoutForms", []):
            summary = order.get("summary") or {}
            total = (summary.get("totalToPay") or {}).get("amount")
            buyer = order.get("buyer") or {}
            results.append(NormalizedOrder(
                external_order_id=str(order.get("id")),
                status=str(order.get("status") or "new"),
                order_lines=[],  # out of scope — see module docstring
                total_amount=float(total) if total is not None else None,
                currency=(summary.get("totalToPay") or {}).get("currency"),
                buyer_email=buyer.get("email"),
                buyer_name=buyer.get("login"),
                buyer_note=order.get("messageToSeller") or None,
                placed_at=datetime.fromisoformat(order["updatedAt"].replace("Z", "+00:00")) if order.get("updatedAt") else None,
                raw_metadata=order,
            ))
        return results

    async def fetch_returns(
        self, connection: MarketplaceConnection, since: Optional[datetime] = None
    ) -> list[NormalizedReturn]:
        """Out of scope — messaging-only connector, see module docstring."""
        return []

    def parse_webhook(self, raw_payload: bytes, headers: dict) -> None:
        """Allegro does have a webhook (Allegro Webhooks) mechanism, but
        signature verification wasn't researched for this messaging-only
        pass — same conservative stance as this batch's other connectors."""
        return None

    async def _find_thread_id(self, connection: MarketplaceConnection, creds: dict, order: MarketplaceOrder) -> Optional[str]:
        """Looks up an existing message thread related to this order.
        UNVERIFIED — the exact query-filter syntax for scoping GET
        /messaging/threads to one order wasn't confirmed in research (only
        that threads relate to orders via relatesTo.order.id in the
        response shape); this filters client-side over the returned page
        rather than trusting an unconfirmed server-side filter param."""
        settings = get_settings()
        _, api_base = _base_urls(settings.ALLEGRO_ENVIRONMENT)
        client = await self._get_client()
        try:
            resp = await client.get(
                f"{api_base}/messaging/threads",
                headers={
                    "Authorization": f"Bearer {decrypt_secret(creds['access_token'])}",
                    "Accept": "application/vnd.allegro.public.v1+json",
                },
            )
            if resp.status_code != 200:
                return None
            body = resp.json()
        except Exception:
            return None

        for thread in body.get("threads", []):
            related_order = ((thread.get("relatesTo") or {}).get("order") or {}).get("id")
            if related_order and str(related_order) == order.external_order_id:
                return thread.get("id")
        return None

    async def send_message(self, connection: MarketplaceConnection, order: MarketplaceOrder, message: str) -> SendResult:
        creds = await self._ensure_fresh_token(connection)
        if not creds:
            return SendResult(success=False, error="could not refresh token")

        thread_id = await self._find_thread_id(connection, creds, order)
        if not thread_id:
            return SendResult(success=False, error="no existing message thread found for this order — this connector can only reply to a thread the buyer already started, not originate a new one (unconfirmed whether Allegro's API supports that at all)")

        settings = get_settings()
        _, api_base = _base_urls(settings.ALLEGRO_ENVIRONMENT)
        client = await self._get_client()
        try:
            resp = await client.post(
                f"{api_base}/messaging/threads/{thread_id}/messages",
                headers={
                    "Authorization": f"Bearer {decrypt_secret(creds['access_token'])}",
                    "Content-Type": "application/vnd.allegro.public.v1+json",
                },
                json={"text": message},
            )
            if resp.status_code not in (200, 201):
                logger.warning("[Allegro] send_message -> %d: %s", resp.status_code, resp.text[:200])
                return SendResult(success=False, error=f"Allegro returned {resp.status_code}")
            body = resp.json()
        except Exception as exc:
            logger.error("[Allegro] send_message failed: %r", exc, exc_info=True)
            return SendResult(success=False, error=str(exc))
        return SendResult(success=True, external_message_id=str(body.get("id")) if body.get("id") else None)

    async def fetch_messages(self, connection: MarketplaceConnection, order: MarketplaceOrder) -> list[NormalizedMessage]:
        creds = await self._ensure_fresh_token(connection)
        if not creds:
            return []
        thread_id = await self._find_thread_id(connection, creds, order)
        if not thread_id:
            return []

        settings = get_settings()
        _, api_base = _base_urls(settings.ALLEGRO_ENVIRONMENT)
        client = await self._get_client()
        try:
            resp = await client.get(
                f"{api_base}/messaging/threads/{thread_id}/messages",
                headers={
                    "Authorization": f"Bearer {decrypt_secret(creds['access_token'])}",
                    "Accept": "application/vnd.allegro.public.v1+json",
                },
            )
            if resp.status_code != 200:
                logger.warning("[Allegro] GET messages -> %d: %s", resp.status_code, resp.text[:200])
                return []
            body = resp.json()
        except Exception as exc:
            logger.error("[Allegro] GET messages failed: %r", exc, exc_info=True)
            return []

        results = []
        for msg in body.get("messages", []):
            author = msg.get("author") or {}
            results.append(NormalizedMessage(
                external_message_id=str(msg.get("id")) if msg.get("id") else None,
                external_order_id=order.external_order_id,
                external_case_id=thread_id,
                body=msg.get("text") or "",
                sent_at=datetime.fromisoformat(msg["createdAt"].replace("Z", "+00:00")) if msg.get("createdAt") else None,
                # direction computed here (not by the generic sync loop —
                # see ebay.py's fetch_messages for why). author.role is the
                # confirmed field to distinguish buyer vs seller — the
                # beta.v1 bug (bare "USER" role) mentioned in the module
                # docstring is avoided by using public.v1, but role could
                # still be missing on some rows; defaults to "outbound"
                # (i.e. assumed ours) when unclear, the safer default since
                # misclassifying our own message as inbound is more
                # confusing than the reverse.
                raw_metadata={"direction": "inbound" if author.get("role") == "BUYER" else "outbound"},
            ))
        return results

    def order_url(self, connection: MarketplaceConnection, external_order_id: str) -> Optional[str]:
        """MEDIUM confidence — Allegro's seller panel order-detail pattern,
        inferred from common URL conventions, not confirmed against a
        documented permalink spec."""
        return f"https://allegro.pl/sale/orders/all/{external_order_id}"


allegro_connector = AllegroConnector()

__all__ = ["AllegroConnector", "allegro_connector"]
