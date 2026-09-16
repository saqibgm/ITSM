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

Messaging (2026-09-15, superseding the Phase 0 "likely full, not sandbox-
validated" note): the Post-Order API's Inquiry resource (what send_message
originally tried) is confirmed DEAD in Sandbox — both search and send 404/are
documented as unsupported there, no way to verify or use it in this
environment. The REAL, WORKING mechanism is eBay's legacy XML Trading API —
AddMemberMessageAAQToPartner (send) and GetMemberMessages (read) — genuine
order-tied two-way buyer-seller messaging, confirmed LIVE against this org's
real sandbox connection: both calls return HTTP 200 with real structured
XML responses using the SAME OAuth token via the X-EBAY-API-IAF-TOKEN
header (no separate "Auth'n'Auth" token needed). AddMemberMessageAAQToPartner
returned a genuine business-logic error (ErrorCode 16202, "Invalid item —
we did not find your item in our system") against a fake test ItemID —
confirming the endpoint/auth/request-shape all work, it just needs a real
listing ID. GetMemberMessages returned Ack=Success with 0 messages (no real
data yet, but a clean, working response). Trading API is item-scoped, not
order-scoped — needs a line item's legacyItemId, not the Fulfillment API's
orderId, hence the extra field capture in fetch_orders() below.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from xml.etree import ElementTree
from xml.sax.saxutils import escape as xml_escape

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

# eBay's legacy Trading API is XML/SOAP-flavored, not REST/JSON like every
# other call in this connector — namespace needed to parse response elements
# via ElementTree.
_TRADING_XML_NS = {"e": "urn:ebay:apis:eBLBaseComponents"}


def _trading_base_url(environment: str) -> str:
    """Trading API's sandbox/production split — same hostnames as the REST
    Sell APIs (_base_urls' api_base), but called out separately since the
    two API families are otherwise unrelated (different auth header, XML vs
    JSON) and it'd be confusing to reuse _base_urls' tuple-of-two shape for
    a single URL."""
    return "https://api.sandbox.ebay.com" if environment == "sandbox" else "https://api.ebay.com"


def _representative_item_id(order: MarketplaceOrder) -> Optional[str]:
    """Trading API's messaging calls (AddMemberMessageAAQToPartner,
    GetMemberMessages) are ITEM-scoped, not order-scoped — need a line
    item's legacyItemId, captured in order_lines by fetch_orders() below.
    Uses the first line item as representative; a real limitation for
    multi-item orders spanning different listings (the Trading API has no
    concept of 'message about this whole order'), not worked around here."""
    for line in order.order_lines or []:
        item_id = line.get("legacy_item_id")
        if item_id:
            return str(item_id)
    return None

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
                    # title was mistakenly set to the raw lineItemId before
                    # this fix (2026-09-15) — real product title is on
                    # li["title"] per the Fulfillment API's LineItem schema.
                    # legacyItemId is the classic numeric ItemID the Trading
                    # API's messaging calls need (confirmed real field,
                    # distinct from lineItemId) — captured here since
                    # fetch_orders() is the only place this data is fetched.
                    {"title": li.get("title"), "quantity": li.get("quantity"), "legacy_item_id": li.get("legacyItemId")}
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
                buyer_note=order.get("buyerCheckoutNotes") or None,  # real field, confirmed via eBay's Order type — a checkout-time note, not a live channel
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

    async def send_message(self, connection: MarketplaceConnection, order: MarketplaceOrder, message: str) -> SendResult:
        """Uses the Trading API's AddMemberMessageAAQToPartner — see module
        docstring for why this replaced the original Post-Order Inquiry
        attempt (that resource is dead in Sandbox entirely; this one is
        confirmed live and working). Item-scoped, not order-scoped — needs
        a legacyItemId from one of this order's line items."""
        item_id = _representative_item_id(order)
        if not item_id:
            return SendResult(success=False, error="no legacyItemId on record for this order's line items — cannot address the Trading API messaging call")

        buyer_username = order.buyer_name  # eBay's buyer_name IS the eBay username, not a display name — see fetch_orders()' buyer_name note
        if not buyer_username:
            return SendResult(success=False, error="no buyer username on record for this order")

        creds = await self._ensure_fresh_token(connection)
        if not creds:
            return SendResult(success=False, error="could not refresh token")

        settings = get_settings()
        xml_body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<AddMemberMessageAAQToPartnerRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
            f"<ItemID>{xml_escape(item_id)}</ItemID>"
            "<MemberMessage>"
            "<Subject>Regarding your order</Subject>"
            f"<Body>{xml_escape(message)}</Body>"
            f"<RecipientID>{xml_escape(buyer_username)}</RecipientID>"
            "<QuestionType>General</QuestionType>"
            "</MemberMessage>"
            "</AddMemberMessageAAQToPartnerRequest>"
        )
        client = await self._get_client()
        try:
            resp = await client.post(
                f"{_trading_base_url(settings.EBAY_ENVIRONMENT)}/ws/api.dll",
                content=xml_body,
                headers={
                    "X-EBAY-API-SITEID": "0",
                    "X-EBAY-API-COMPATIBILITY-LEVEL": "1155",
                    "X-EBAY-API-CALL-NAME": "AddMemberMessageAAQToPartner",
                    "X-EBAY-API-IAF-TOKEN": decrypt_secret(creds["access_token"]),
                    "Content-Type": "text/xml",
                },
            )
        except Exception as exc:
            logger.error("[eBay] AddMemberMessageAAQToPartner failed: %r", exc, exc_info=True)
            return SendResult(success=False, error=str(exc))

        try:
            root = ElementTree.fromstring(resp.text)
        except ElementTree.ParseError:
            logger.warning("[eBay] AddMemberMessageAAQToPartner non-XML response (status %d): %s", resp.status_code, resp.text[:200])
            return SendResult(success=False, error=f"eBay returned a non-XML response (status {resp.status_code})")

        ack = root.findtext("e:Ack", namespaces=_TRADING_XML_NS)
        if ack not in ("Success", "Warning"):
            short_msg = root.findtext(".//e:Errors/e:ShortMessage", namespaces=_TRADING_XML_NS) or "unknown error"
            long_msg = root.findtext(".//e:Errors/e:LongMessage", namespaces=_TRADING_XML_NS) or ""
            logger.warning("[eBay] AddMemberMessageAAQToPartner failed: %s %s", short_msg, long_msg)
            return SendResult(success=False, error=f"{short_msg} {long_msg}".strip())

        return SendResult(success=True)

    async def fetch_messages(self, connection: MarketplaceConnection, order: MarketplaceOrder) -> list[NormalizedMessage]:
        """Trading API's GetMemberMessages — confirmed live (Ack=Success,
        0 messages since no real conversation exists yet) against this
        org's sandbox connection, 2026-09-15. Same item-scoping limitation
        as send_message above. Direction isn't in NormalizedMessage's own
        shape — callers determine inbound-vs-outbound by comparing
        raw_metadata['sender_id'] against order.buyer_name (the eBay
        username), which this connector already uses as the durable buyer
        identifier (see fetch_orders())."""
        item_id = _representative_item_id(order)
        if not item_id:
            return []

        creds = await self._ensure_fresh_token(connection)
        if not creds:
            return []

        settings = get_settings()
        xml_body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<GetMemberMessagesRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
            f"<ItemID>{item_id}</ItemID>"
            "<MailMessageType>All</MailMessageType>"
            "<DetailLevel>ReturnMessages</DetailLevel>"
            "</GetMemberMessagesRequest>"
        )
        client = await self._get_client()
        try:
            resp = await client.post(
                f"{_trading_base_url(settings.EBAY_ENVIRONMENT)}/ws/api.dll",
                content=xml_body,
                headers={
                    "X-EBAY-API-SITEID": "0",
                    "X-EBAY-API-COMPATIBILITY-LEVEL": "1155",
                    "X-EBAY-API-CALL-NAME": "GetMemberMessages",
                    "X-EBAY-API-IAF-TOKEN": decrypt_secret(creds["access_token"]),
                    "Content-Type": "text/xml",
                },
            )
        except Exception as exc:
            logger.error("[eBay] GetMemberMessages failed: %r", exc, exc_info=True)
            return []

        try:
            root = ElementTree.fromstring(resp.text)
        except ElementTree.ParseError:
            logger.warning("[eBay] GetMemberMessages non-XML response (status %d): %s", resp.status_code, resp.text[:200])
            return []

        if root.findtext("e:Ack", namespaces=_TRADING_XML_NS) not in ("Success", "Warning"):
            logger.warning("[eBay] GetMemberMessages -> %s", resp.text[:300])
            return []

        results = []
        for exchange in root.findall(".//e:MemberMessageExchange", namespaces=_TRADING_XML_NS):
            sender_id = exchange.findtext(".//e:SenderID", namespaces=_TRADING_XML_NS)
            body_text = exchange.findtext(".//e:Body", namespaces=_TRADING_XML_NS) or ""
            created_raw = exchange.findtext(".//e:CreationDate", namespaces=_TRADING_XML_NS)
            question_id = exchange.findtext(".//e:QuestionId", namespaces=_TRADING_XML_NS)
            sent_at = None
            if created_raw:
                try:
                    sent_at = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
                except ValueError:
                    pass
            results.append(NormalizedMessage(
                external_message_id=question_id or f"{item_id}:{created_raw}",
                external_order_id=order.external_order_id,
                external_case_id=None,
                body=body_text,
                sent_at=sent_at,
                raw_metadata={"sender_id": sender_id},
            ))
        return results

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
