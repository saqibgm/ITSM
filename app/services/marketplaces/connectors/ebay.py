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

Messaging — REBUILT 2026-09-17 on eBay's REST Message API
(commerce/message/v1), which eBay itself documents as replacing
AddMemberMessageAAQToPartner, AddMemberMessageRTQ,
AddMemberMessagesAAQToBidder, DeleteMyMessages, GetMemberMessages,
GetMyMessages, and ReviseMyMessages — the XML Trading API calls this
connector used until now (confirmed live 2026-09-15, see git history;
replaced rather than kept as a fallback because eBay itself frames the old
calls as superseded, not merely deprecated-but-supported). Confirmed via
eBay's own developer docs (2026-09-17):
- POST /send_message — start or continue a conversation. One of
  conversationId/otherPartyUsername required, plus messageText. An
  OPTIONAL reference{referenceType: "LISTING", referenceId} container ties
  a message to a listing — optional is the key change from Trading API's
  MANDATORY item-scoping, though this connector still passes a
  legacyItemId when one's on hand (best-effort, not required to send).
- GET /conversation — list conversations (conversation_type=FROM_MEMBERS
  for buyer-seller, vs FROM_EBAY for eBay-authored ones).
- GET /conversation/{conversation_id} — messages within one conversation;
  MessageDetail fields: createdDate, messageBody, messageId,
  senderUsername, recipientUsername.
- POST /update_conversation — mark read/archived/deleted.
Auth scope: https://api.ebay.com/oauth/api_scope/commerce.message (added
to EBAY_SCOPES, 2026-09-17 — see config.py's comment on the real risk of
hitting the same invalid_scope entitlement wall as sell.post-order did).

UNVERIFIED against live traffic — no sandbox account has exercised these
specific endpoints yet (unlike the Trading API calls this replaces, which
WERE live-confirmed). The exact JSON field names for getConversations'
list-level items (specifically whether a conversation carries the other
party's username directly, needed to match a conversation to an order's
buyer) weren't confirmed in research — fetch_messages() below is written
defensively (degrades to an empty list rather than guessing a wrong field
name) rather than presented with false confidence.

Real-time push (2026-09-17, genuinely new capability): eBay's Notification
API gained NEW_MESSAGE and BUYER_QUESTION topics in the same Q4 2025
release (v1.6.5, 2025-11-17, confirmed via eBay's release notes) — a real
webhook mechanism, unlike anything previously available for eBay messaging
in this build. See register_webhooks()/normalize_event() below and
marketplace_ebay_notification.py (the inbound route). Treated as a thin
"something changed, go check" ping rather than a payload-carrying event —
the notification body's exact schema for these two specific topics wasn't
confirmed in research (eBay ships full schemas as downloadable AsyncAPI
contracts, not fetchable via this build's research tooling), so rather
than guess field names for the actual message content, a notification
triggers a live getConversations(UNREAD) call to fetch the real thing.
This sidesteps the one unconfirmed piece entirely and is a legitimate,
common "notify then fetch" webhook pattern in its own right.
"""

import base64
import hashlib
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from sqlalchemy import select

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
# getPublicKey's response is cached in-process for this long (eBay's own
# recommendation, see connectors/ebay.py's verify_notification_signature).
_PUBLIC_KEY_CACHE_TTL_SECONDS = 3600
_public_key_cache: dict[str, tuple[float, str, str]] = {}  # kid -> (cached_at, algorithm, pem_or_der_key)


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
        """Still None for the generic marketplace-webhook shape — eBay's
        Notification API is a deliberately separate mechanism (its own
        challenge-response handshake, its own per-connection destination
        URL, its own signature scheme) with its own dedicated route,
        marketplace_ebay_notification.py, which calls normalize_event()
        directly rather than going through this generic entry point. Order/
        return events still have no eBay webhook at all (unchanged from
        before) — this method covers that gap only."""
        return None

    async def send_message(self, connection: MarketplaceConnection, order: MarketplaceOrder, message: str) -> SendResult:
        """REST Message API's sendMessage — see module docstring for why
        this replaced the Trading API calls. Keyed on otherPartyUsername
        (eBay's buyer_name IS the eBay username — see fetch_orders()), not
        conversationId, so this always targets "the conversation with this
        buyer" whether or not one already exists. A legacyItemId is
        attached via the OPTIONAL reference container when available
        (best-effort context, not required to send — the real change from
        Trading API's mandatory item-scoping)."""
        buyer_username = order.buyer_name
        if not buyer_username:
            return SendResult(success=False, error="no buyer username on record for this order")

        creds = await self._ensure_fresh_token(connection)
        if not creds:
            return SendResult(success=False, error="could not refresh token")

        settings = get_settings()
        _, api_base = _base_urls(settings.EBAY_ENVIRONMENT)
        body = {"otherPartyUsername": buyer_username, "messageText": message}
        item_id = _representative_item_id(order)
        if item_id:
            body["reference"] = {"referenceType": "LISTING", "referenceId": item_id}

        client = await self._get_client()
        try:
            resp = await client.post(
                f"{api_base}/commerce/message/v1/send_message",
                headers={
                    "Authorization": f"Bearer {decrypt_secret(creds['access_token'])}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
            if resp.status_code not in (200, 201, 204):
                logger.warning("[eBay] send_message -> %d: %s", resp.status_code, resp.text[:300])
                return SendResult(success=False, error=f"eBay returned {resp.status_code}: {resp.text[:200]}")
            result_body = resp.json() if resp.content else {}
        except Exception as exc:
            logger.error("[eBay] send_message failed: %r", exc, exc_info=True)
            return SendResult(success=False, error=str(exc))

        return SendResult(success=True, external_message_id=result_body.get("messageId") or result_body.get("conversationId"))

    async def fetch_messages(self, connection: MarketplaceConnection, order: MarketplaceOrder) -> list[NormalizedMessage]:
        """REST Message API's getConversations + getConversation — manual/
        backfill path (mirrors the "sync now" pattern every other connector
        uses this method for). UNVERIFIED against live traffic — see module
        docstring on the getConversations list-item field-name uncertainty;
        written defensively (checks several plausible field paths for the
        other party's username) rather than trusting one guessed shape."""
        buyer_username = order.buyer_name
        if not buyer_username:
            return []

        creds = await self._ensure_fresh_token(connection)
        if not creds:
            return []

        settings = get_settings()
        _, api_base = _base_urls(settings.EBAY_ENVIRONMENT)
        headers = {"Authorization": f"Bearer {decrypt_secret(creds['access_token'])}"}
        client = await self._get_client()

        try:
            resp = await client.get(
                f"{api_base}/commerce/message/v1/conversation",
                headers=headers,
                params={"conversation_type": "FROM_MEMBERS"},
            )
            if resp.status_code != 200:
                logger.warning("[eBay] GET conversation -> %d: %s", resp.status_code, resp.text[:300])
                return []
            conversations = (resp.json() or {}).get("conversations", [])
        except Exception as exc:
            logger.error("[eBay] GET conversation failed: %r", exc, exc_info=True)
            return []

        conversation_id = None
        for conv in conversations:
            other = (
                conv.get("otherPartyUsername")
                or (conv.get("otherParty") or {}).get("username")
                or (conv.get("recipient") or {}).get("username")
            )
            if other == buyer_username:
                conversation_id = conv.get("conversationId")
                break
        if conversation_id is None:
            return []

        try:
            resp = await client.get(
                f"{api_base}/commerce/message/v1/conversation/{conversation_id}",
                headers=headers,
                params={"conversation_type": "FROM_MEMBERS"},
            )
            if resp.status_code != 200:
                logger.warning("[eBay] GET conversation/%s -> %d: %s", conversation_id, resp.status_code, resp.text[:300])
                return []
            messages = (resp.json() or {}).get("messages", [])
        except Exception as exc:
            logger.error("[eBay] GET conversation/%s failed: %r", conversation_id, exc, exc_info=True)
            return []

        results = []
        for msg in messages:
            sender = msg.get("senderUsername")
            created_raw = msg.get("createdDate")
            sent_at = None
            if created_raw:
                try:
                    sent_at = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
                except ValueError:
                    pass
            results.append(NormalizedMessage(
                external_message_id=msg.get("messageId"),
                external_order_id=order.external_order_id,
                external_case_id=str(conversation_id),
                body=msg.get("messageBody") or "",
                sent_at=sent_at,
                # direction computed HERE, not by the generic sync loop —
                # eBay's buyer_name IS the eBay username (see
                # fetch_orders()), so a direct string match is correct.
                raw_metadata={"direction": "inbound" if sender and sender == buyer_username else "outbound"},
            ))
        return results

    # ------------------------------------------------------------------
    # Notification API — real webhook-driven intake (2026-09-17), see
    # module docstring. register_webhooks() sets up a per-connection
    # destination + subscribes it to NEW_MESSAGE/BUYER_QUESTION;
    # normalize_event() is the notification route's dispatch target.
    # ------------------------------------------------------------------

    async def register_webhooks(self, connection: MarketplaceConnection) -> None:
        creds = await self._ensure_fresh_token(connection)
        if not creds:
            logger.warning("[eBay] register_webhooks: could not refresh token for connection %s", connection.id)
            return

        settings = get_settings()
        _, api_base = _base_urls(settings.EBAY_ENVIRONMENT)
        headers = {
            "Authorization": f"Bearer {decrypt_secret(creds['access_token'])}",
            "Content-Type": "application/json",
        }
        client = await self._get_client()

        # Verification token: 32-80 chars, [A-Za-z0-9_-] — generated once
        # per connection and persisted, since createDestination needs the
        # SAME token every time it's (re-)registered, and the webhook
        # route needs it to compute the challenge response.
        verification_token = connection.credentials.get("notification_verification_token")
        if not verification_token:
            verification_token = secrets.token_urlsafe(48)[:64]
            connection.credentials = {**connection.credentials, "notification_verification_token": verification_token}

        endpoint = f"{settings.EBAY_WEBHOOK_PUBLIC_BASE_URL}/api/v1/webhooks/marketplace/ebay/notification/{connection.id}"

        try:
            resp = await client.post(
                f"{api_base}/commerce/notification/v1/destination",
                headers=headers,
                json={"name": f"itsm-{connection.id}", "deliveryConfig": {"endpoint": endpoint, "verificationToken": verification_token}},
            )
            if resp.status_code not in (200, 201, 204):
                logger.warning("[eBay] createDestination -> %d: %s", resp.status_code, resp.text[:300])
                return
        except Exception as exc:
            logger.error("[eBay] createDestination failed: %r", exc, exc_info=True)
            return

        # createDestination returns 204 with no body (confirmed via eBay's
        # own docs) — destinationId has to be looked up afterward via
        # getDestinations, matched on the endpoint we just registered.
        try:
            resp = await client.get(f"{api_base}/commerce/notification/v1/destination", headers=headers)
            destinations = (resp.json() or {}).get("destinations", []) if resp.status_code == 200 else []
        except Exception as exc:
            logger.error("[eBay] getDestinations failed: %r", exc, exc_info=True)
            return

        destination_id = None
        for dest in destinations:
            if (dest.get("deliveryConfig") or {}).get("endpoint") == endpoint:
                destination_id = dest.get("destinationId")
                break
        if destination_id is None:
            logger.warning("[eBay] could not find our own destination after creating it (connection %s)", connection.id)
            return

        # topicId isn't a confirmed literal string (eBay's release note used
        # the plain-English names "New Message"/"Buyer Question", not
        # necessarily the API's topicId spelling) — resolved live via
        # getTopics rather than guessed, matched on the topicId itself
        # first (in case it IS exactly "NEW_MESSAGE"/"BUYER_QUESTION"),
        # falling back to a substring match on the description.
        try:
            resp = await client.get(f"{api_base}/commerce/notification/v1/topic", headers=headers)
            topics = (resp.json() or {}).get("topics", []) if resp.status_code == 200 else []
        except Exception as exc:
            logger.error("[eBay] getTopics failed: %r", exc, exc_info=True)
            return

        wanted = {"NEW_MESSAGE": ("new_message", "new message"), "BUYER_QUESTION": ("buyer_question", "buyer question")}
        resolved_topic_ids = []
        for topic in topics:
            topic_id = topic.get("topicId") or ""
            description = (topic.get("description") or "").lower()
            for canonical, needles in wanted.items():
                if topic_id.upper() == canonical or any(n in topic_id.lower() or n in description for n in needles):
                    resolved_topic_ids.append(topic_id)
                    break

        if not resolved_topic_ids:
            logger.warning("[eBay] could not resolve NEW_MESSAGE/BUYER_QUESTION topicIds from getTopics (connection %s) — subscriptions not created", connection.id)
            return

        for topic_id in resolved_topic_ids:
            try:
                resp = await client.post(
                    f"{api_base}/commerce/notification/v1/subscription",
                    headers=headers,
                    json={"topicId": topic_id, "destinationId": destination_id, "status": "ENABLED", "payload": {"format": "JSON"}},
                )
                if resp.status_code not in (200, 201, 204):
                    logger.warning("[eBay] createSubscription(%s) -> %d: %s", topic_id, resp.status_code, resp.text[:300])
            except Exception as exc:
                logger.error("[eBay] createSubscription(%s) failed: %r", topic_id, exc, exc_info=True)

        logger.info("[eBay] registered notification destination + subscriptions for connection %s: %s", connection.id, resolved_topic_ids)

    async def verify_notification_signature(self, connection: MarketplaceConnection, raw_body: bytes, signature_header: str) -> bool:
        """X-EBAY-SIGNATURE verification — base64-decoded header carries a
        key id ('kid') and the signature itself; ECDSA+SHA1 over the raw
        request body, verified against a public key fetched (and cached,
        eBay's own recommended 1hr TTL) via getPublicKey. UNVERIFIED against
        live traffic — see config.py's EBAY_NOTIFICATION_SIGNATURE_
        VERIFICATION_ENABLED for why this can be toggled off if a subtly
        wrong implementation is blocking real notifications during initial
        debugging, without ripping the code out."""
        import json as _json

        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.serialization import load_pem_public_key

        try:
            decoded = _json.loads(base64.b64decode(signature_header))
            kid = decoded["kid"]
            signature = base64.b64decode(decoded["signature"])
        except Exception as exc:
            logger.warning("[eBay] could not parse X-EBAY-SIGNATURE header: %r", exc)
            return False

        cached = _public_key_cache.get(kid)
        if cached and (time.time() - cached[0]) < _PUBLIC_KEY_CACHE_TTL_SECONDS:
            _, algorithm, key_pem = cached
        else:
            creds = await self._ensure_fresh_token(connection)
            if not creds:
                return False
            settings = get_settings()
            _, api_base = _base_urls(settings.EBAY_ENVIRONMENT)
            client = await self._get_client()
            try:
                resp = await client.get(
                    f"{api_base}/commerce/notification/v1/public_key/{kid}",
                    headers={"Authorization": f"Bearer {decrypt_secret(creds['access_token'])}"},
                )
                if resp.status_code != 200:
                    logger.warning("[eBay] getPublicKey(%s) -> %d: %s", kid, resp.status_code, resp.text[:200])
                    return False
                body = resp.json()
                algorithm = body.get("algorithm", "")
                key_pem = body.get("key", "")
            except Exception as exc:
                logger.error("[eBay] getPublicKey(%s) failed: %r", kid, exc, exc_info=True)
                return False
            _public_key_cache[kid] = (time.time(), algorithm, key_pem)

        try:
            public_key = load_pem_public_key(key_pem.encode() if isinstance(key_pem, str) else key_pem)
            public_key.verify(signature, raw_body, ec.ECDSA(hashlib.sha1()))
            return True
        except Exception as exc:
            logger.warning("[eBay] signature verification failed: %r", exc)
            return False

    async def normalize_event(
        self, event_type: str, payload: dict, *, db=None, tenant_id=None, connection: Optional[MarketplaceConnection] = None
    ) -> Optional[NormalizedMessage]:
        """Dispatch target for the Notification API route — event_type is
        always "ebay_message_notification" (set at the route layer, see
        marketplace_ebay_notification.py), not a specific topicId, since
        both NEW_MESSAGE and BUYER_QUESTION funnel into the same "go check
        for new buyer messages" handling (see module docstring for why the
        notification body itself isn't parsed for message content).

        Live-fetches the most recently updated unread member conversation
        and returns its newest message, resolving external_order_id via a
        best-effort DB lookup: first by matching the conversation's other-
        party username against an order's buyer_name (most recent order for
        that buyer), since the Message API's reference container is
        optional and may not be populated. UNVERIFIED against live traffic."""
        if event_type != "ebay_message_notification" or connection is None or db is None:
            return None

        creds = await self._ensure_fresh_token(connection)
        if not creds:
            return None

        settings = get_settings()
        _, api_base = _base_urls(settings.EBAY_ENVIRONMENT)
        headers = {"Authorization": f"Bearer {decrypt_secret(creds['access_token'])}"}
        client = await self._get_client()

        try:
            resp = await client.get(
                f"{api_base}/commerce/message/v1/conversation",
                headers=headers,
                params={"conversation_type": "FROM_MEMBERS", "conversation_status": "UNREAD"},
            )
            if resp.status_code != 200:
                logger.warning("[eBay] notification-triggered GET conversation -> %d: %s", resp.status_code, resp.text[:300])
                return None
            conversations = (resp.json() or {}).get("conversations", [])
        except Exception as exc:
            logger.error("[eBay] notification-triggered GET conversation failed: %r", exc, exc_info=True)
            return None
        if not conversations:
            return None

        conversation = conversations[0]
        conversation_id = conversation.get("conversationId")
        other_party = (
            conversation.get("otherPartyUsername")
            or (conversation.get("otherParty") or {}).get("username")
        )

        try:
            resp = await client.get(
                f"{api_base}/commerce/message/v1/conversation/{conversation_id}",
                headers=headers,
                params={"conversation_type": "FROM_MEMBERS"},
            )
            if resp.status_code != 200:
                return None
            messages = (resp.json() or {}).get("messages", [])
        except Exception as exc:
            logger.error("[eBay] notification-triggered GET conversation/%s failed: %r", conversation_id, exc, exc_info=True)
            return None
        if not messages:
            return None

        newest = max(messages, key=lambda m: m.get("createdDate") or "")
        sender = newest.get("senderUsername") or other_party

        external_order_id = None
        if sender:
            order = (
                await db.execute(
                    select(MarketplaceOrder)
                    .where(MarketplaceOrder.tenant_id == tenant_id, MarketplaceOrder.buyer_name == sender)
                    .order_by(MarketplaceOrder.placed_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if order is not None:
                external_order_id = order.external_order_id

        created_raw = newest.get("createdDate")
        sent_at = None
        if created_raw:
            try:
                sent_at = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
            except ValueError:
                pass

        return NormalizedMessage(
            external_message_id=newest.get("messageId"),
            external_order_id=external_order_id,
            external_case_id=str(conversation_id) if conversation_id else None,
            body=newest.get("messageBody") or "",
            sent_at=sent_at,
            raw_metadata={"direction": "inbound" if sender and sender == other_party else "outbound"},
        )

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
