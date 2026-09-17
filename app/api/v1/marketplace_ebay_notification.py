"""
eBay Notification API webhook route (2026-09-17) — see connectors/ebay.py's
module docstring for the NEW_MESSAGE/BUYER_QUESTION background. A deliberately
separate file/route shape from every other marketplace webhook in this repo:
eBay's Notification API has its own challenge-response registration handshake
and its own digital-signature scheme, neither of which fit the generic
{provider}-webhook pattern (HMAC-over-body or shared-token) used elsewhere.

Per-connection routing: connection_id is a path segment
(/webhooks/marketplace/ebay/notification/{connection_id}), not derived from
notification payload content — mirrors the same per-tenant-URL pattern used
by the Amazon inbound-email bridge (marketplace_amazon_inbound_email.py),
for the same reason: don't depend on an unconfirmed payload field to route
to the right tenant when a confirmed URL-based mechanism is available instead.

Two request shapes hit this SAME path:
1. GET ?challenge_code=... — eBay's registration-time verification. Must
   respond 200 with {"challengeResponse": sha256(challengeCode +
   verificationToken + endpoint).hexdigest()} — confirmed exact mechanism
   and field order via eBay's own docs.
2. POST — an actual notification delivery, X-EBAY-SIGNATURE header holds
   the digital signature (verified via connectors/ebay.py's
   verify_notification_signature()). Body isn't parsed for message content
   (see ebay.py's module docstring on why) — just used to trigger a
   MarketplaceEvent + the existing process_marketplace_event pipeline,
   which live-fetches the actual new message.
"""

import hashlib
import logging
import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models.marketplace import MarketplaceConnection, MarketplaceEvent
from app.services.marketplaces.connectors.ebay import ebay_connector

logger = logging.getLogger(__name__)

webhook_router = APIRouter(tags=["marketplaces-webhooks"])


async def _get_connection(db: AsyncSession, connection_id: str) -> MarketplaceConnection | None:
    try:
        return (
            await db.execute(
                select(MarketplaceConnection).where(
                    MarketplaceConnection.id == connection_id,
                    MarketplaceConnection.provider == "ebay",
                )
            )
        ).scalar_one_or_none()
    except Exception:
        # Malformed UUID in the path, etc. — not a real connection either way.
        return None


@webhook_router.api_route("/webhooks/marketplace/ebay/notification/{connection_id}", methods=["GET", "POST"])
async def ebay_notification(connection_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    connection = await _get_connection(db, connection_id)
    if connection is None:
        # Unknown/disconnected connection — 200 on GET so eBay doesn't
        # retry the challenge forever; 200 on POST so it doesn't retry
        # a notification we can never route anywhere.
        logger.info("ebay_notification_unknown_connection", extra={"connection_id": connection_id})
        return JSONResponse(status_code=200, content={"received": True})

    verification_token = (connection.credentials or {}).get("notification_verification_token")

    if request.method == "GET":
        challenge_code = request.query_params.get("challenge_code")
        if not challenge_code or not verification_token:
            return JSONResponse(status_code=400, content={"error": "missing challenge_code or unregistered connection"})
        settings = get_settings()
        endpoint = f"{settings.EBAY_WEBHOOK_PUBLIC_BASE_URL}/api/v1/webhooks/marketplace/ebay/notification/{connection_id}"
        # Exact concatenation order is mandatory — challengeCode +
        # verificationToken + endpoint — confirmed via eBay's own docs.
        digest = hashlib.sha256(f"{challenge_code}{verification_token}{endpoint}".encode()).hexdigest()
        return JSONResponse(status_code=200, content={"challengeResponse": digest})

    # POST — an actual notification delivery.
    raw_body = await request.body()
    settings = get_settings()
    if settings.EBAY_NOTIFICATION_SIGNATURE_VERIFICATION_ENABLED:
        signature_header = request.headers.get("X-EBAY-SIGNATURE", "")
        if not signature_header:
            logger.warning("ebay_notification_missing_signature", extra={"connection_id": connection_id})
            return JSONResponse(status_code=412, content={"error": "missing X-EBAY-SIGNATURE"})
        verified = await ebay_connector.verify_notification_signature(connection, raw_body, signature_header)
        if not verified:
            return JSONResponse(status_code=412, content={"error": "signature verification failed"})

    # Not parsed for message content (see connectors/ebay.py's module
    # docstring) — the notification's job here is only to trigger a
    # live fetch, so external_event_id has to come from something OTHER
    # than a message/conversation id we don't try to extract. Delivery
    # attempts can repeat the same body, so this dedup window is coarse
    # (per-minute) rather than exact — acceptable since normalize_event()'s
    # live fetch is itself idempotent-ish (re-checking "what's unread right
    # now" doesn't create duplicate MarketplaceMessage rows,
    # map_fetched_message dedupes on external_message_id).
    external_event_id = f"notification:{connection_id}:{int(time.time() // 60)}"

    existing_event = (
        await db.execute(
            select(MarketplaceEvent).where(
                MarketplaceEvent.provider == "ebay",
                MarketplaceEvent.external_event_id == external_event_id,
            )
        )
    ).scalar_one_or_none()
    if existing_event is not None:
        return JSONResponse(status_code=200, content={"received": True})

    event = MarketplaceEvent(
        tenant_id=connection.tenant_id,
        connection_id=connection.id,
        provider="ebay",
        external_event_id=external_event_id,
        event_type="ebay_message_notification",
        payload={},
        status="received",
    )
    db.add(event)
    await db.commit()
    await db.refresh(event)

    from app.workers.tasks_marketplace_sync import process_marketplace_event
    process_marketplace_event.delay(str(event.id))

    return JSONResponse(status_code=200, content={"received": True})


__all__ = ["webhook_router"]
