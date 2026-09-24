"""
TikTok Shop NEW_MESSAGE webhook route (2026-09-18) — see connectors/
tiktokshop.py's module docstring. A single, tenant-agnostic endpoint (NOT
per-connection like the eBay/Amazon webhook bridges in this batch) —
unlike those two, TikTok Shop's webhook payload is confirmed to carry
shop_id directly, so the right MarketplaceConnection can be resolved from
payload content rather than needing a per-connection URL to disambiguate.

No challenge-response registration handshake was found in this research
pass (unlike eBay's Notification API) — PUT /event/202309/webhooks appears
to just start delivering once subscribed. If TikTok Shop does require one,
this route doesn't handle it yet; flagged rather than assumed away.

Signature verification (CONFIRMED different algorithm from API-request
signing, see connectors/tiktokshop.py): Authorization header =
HMAC-SHA256(app_secret, app_key + raw_body), lowercase hex.
"""

import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models.marketplace import MarketplaceConnection, MarketplaceEvent

logger = logging.getLogger(__name__)

webhook_router = APIRouter(tags=["marketplaces-webhooks"])


def _verify_signature(raw_body: bytes, authorization_header: str, app_key: str, app_secret: str) -> bool:
    expected = hmac.new(app_secret.encode(), (app_key + raw_body.decode(errors="ignore")).encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, authorization_header)


@webhook_router.post("/webhooks/marketplace/tiktokshop/notification")
async def tiktokshop_notification(request: Request, db: AsyncSession = Depends(get_db)):
    settings = get_settings()
    raw_body = await request.body()

    if not settings.TIKTOKSHOP_CLIENT_ID or not settings.TIKTOKSHOP_CLIENT_SECRET:
        logger.warning("tiktokshop_notification_not_configured")
        return JSONResponse(status_code=503, content={})

    authorization = request.headers.get("Authorization", "")
    if not _verify_signature(raw_body, authorization, settings.TIKTOKSHOP_CLIENT_ID, settings.TIKTOKSHOP_CLIENT_SECRET):
        logger.warning("tiktokshop_notification_bad_signature")
        return JSONResponse(status_code=401, content={})

    try:
        payload = json.loads(raw_body)
    except Exception:
        return JSONResponse(status_code=400, content={})

    data = payload.get("data") or payload
    shop_id = str(payload.get("shop_id") or data.get("shop_id") or "")
    event_type = str(payload.get("type") or payload.get("event_type") or "NEW_MESSAGE")

    if not shop_id:
        logger.info("tiktokshop_notification_missing_shop_id")
        return JSONResponse(status_code=200, content={})

    connection = (
        await db.execute(
            select(MarketplaceConnection).where(
                MarketplaceConnection.provider == "tiktokshop",
                MarketplaceConnection.external_id == shop_id,
            )
        )
    ).scalar_one_or_none()
    if connection is None:
        logger.info("tiktokshop_notification_unknown_shop", extra={"shop_id": shop_id})
        return JSONResponse(status_code=200, content={})

    external_event_id = f"{event_type}:{data.get('message_id') or data.get('tts_notification_id') or shop_id}"
    existing_event = (
        await db.execute(
            select(MarketplaceEvent).where(
                MarketplaceEvent.provider == "tiktokshop",
                MarketplaceEvent.external_event_id == external_event_id,
            )
        )
    ).scalar_one_or_none()
    if existing_event is not None:
        return JSONResponse(status_code=200, content={})

    event = MarketplaceEvent(
        tenant_id=connection.tenant_id,
        connection_id=connection.id,
        provider="tiktokshop",
        external_event_id=external_event_id,
        event_type="NEW_MESSAGE",
        payload=data,
        status="received",
    )
    db.add(event)
    await db.commit()
    await db.refresh(event)

    from app.workers.tasks_marketplace_sync import process_marketplace_event
    process_marketplace_event.delay(str(event.id))

    return JSONResponse(status_code=200, content={})


__all__ = ["webhook_router"]
