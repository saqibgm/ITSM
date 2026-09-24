"""TikTok Shop connect/callback/status/disconnect routes (2026-09-18).
Standard OAuth2 authorization-code flow, but every subsequent API call
also needs HMAC request-signing (handled inside connectors/tiktokshop.py).
register_webhooks() is called at the end of the callback to subscribe to
the real, payload-carrying NEW_MESSAGE webhook — the actual route lives in
marketplace_tiktokshop_webhook.py, a separate file since it needs its own
Authorization-header signature scheme (different algorithm from API-
request signing), not the generic marketplace-webhook shape.
"""

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentUser, require_role
from app.config import get_settings
from app.database import get_db
from app.models.marketplace import MarketplaceConnection
from app.redis_client import get_redis
from app.services.marketplaces.connectors.base import MessagingCapability
from app.services.marketplaces.connectors.tiktokshop import tiktokshop_connector
from app.services.marketplaces.crypto import encrypt_secret

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/marketplaces/tiktokshop", tags=["marketplaces"])
_STATE_TTL_SECONDS = 600
_ADMIN_ROLES = ("admin", "tenant_admin")


@router.post("/connect")
async def tiktokshop_connect(
    current_user: CurrentUser = Depends(require_role(*_ADMIN_ROLES)),
    redis=Depends(get_redis),
) -> dict:
    settings = get_settings()
    if not settings.TIKTOKSHOP_ENABLED or not settings.TIKTOKSHOP_CLIENT_ID:
        return {"error": "TikTok Shop integration is not configured on this deployment"}

    state = str(uuid.uuid4())
    await redis.setex(f"tiktokshop_oauth_state:{state}", _STATE_TTL_SECONDS, json.dumps({"tenant_id": str(current_user.tenant_id)}))
    return {"authorize_url": tiktokshop_connector.authorize_url(state)}


@router.get("/callback")
async def tiktokshop_callback(request: Request, db: AsyncSession = Depends(get_db), redis=Depends(get_redis)):
    settings = get_settings()
    frontend_admin = f"{settings.ITSM_FRONTEND_URL}/itsm/admin"
    args = dict(request.query_params)
    state = args.get("state")
    raw_entry = await redis.get(f"tiktokshop_oauth_state:{state}") if state else None
    if not raw_entry:
        return RedirectResponse(f"{frontend_admin}?tiktokshop_error=invalid_state")
    await redis.delete(f"tiktokshop_oauth_state:{state}")
    entry = json.loads(raw_entry)

    if args.get("error"):
        return RedirectResponse(f"{frontend_admin}?tiktokshop_error={args['error']}")

    code = args.get("code")
    if not code:
        return RedirectResponse(f"{frontend_admin}?tiktokshop_error=missing_code")

    result = await tiktokshop_connector.connect(entry["tenant_id"], {"code": code})
    if not result.success:
        return RedirectResponse(f"{frontend_admin}?tiktokshop_error={result.error}")

    token_resp = result.credentials or {}
    access_token = token_resp.get("access_token")
    if not access_token:
        return RedirectResponse(f"{frontend_admin}?tiktokshop_error=token_failed")

    expires_in = token_resp.get("access_token_expire_in")
    credentials = {
        "access_token": encrypt_secret(access_token),
        "shop_id": token_resp.get("shop_id"),
        "shop_cipher": token_resp.get("shop_cipher"),
        "access_token_expires_at": (
            (datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))).isoformat() if expires_in else None
        ),
    }
    if token_resp.get("refresh_token"):
        credentials["refresh_token"] = encrypt_secret(token_resp["refresh_token"])

    connection = (
        await db.execute(
            select(MarketplaceConnection).where(
                MarketplaceConnection.tenant_id == entry["tenant_id"],
                MarketplaceConnection.provider == "tiktokshop",
            )
        )
    ).scalar_one_or_none()
    if connection:
        connection.external_id = result.external_id
        connection.credentials = credentials
        connection.status = "connected"
    else:
        connection = MarketplaceConnection(
            tenant_id=entry["tenant_id"],
            provider="tiktokshop",
            external_id=result.external_id,
            credentials=credentials,
            status="connected",
            messaging_capability=MessagingCapability.FULL.value,
        )
        db.add(connection)
    await db.commit()
    await db.refresh(connection)

    # NEW_MESSAGE webhook subscription (2026-09-18) — best-effort, doesn't
    # block the connect flow on failure (messaging still works via
    # fetch_messages()'s manual/poll path even if the push side doesn't
    # register cleanly).
    try:
        await tiktokshop_connector.register_webhooks(connection)
    except Exception as exc:
        logger.error("[TikTokShop] register_webhooks failed for connection %s: %r", connection.id, exc, exc_info=True)

    return RedirectResponse(f"{frontend_admin}?connected=tiktokshop")


@router.get("/connection")
async def tiktokshop_connection_status(
    current_user: CurrentUser = Depends(require_role(*_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    conn = (
        await db.execute(
            select(MarketplaceConnection).where(
                MarketplaceConnection.tenant_id == current_user.tenant_id,
                MarketplaceConnection.provider == "tiktokshop",
            )
        )
    ).scalar_one_or_none()
    if not conn:
        return {"connection": None}
    return {"connection": {"external_id": conn.external_id, "status": conn.status, "messaging_capability": conn.messaging_capability}}


@router.delete("/connection")
async def tiktokshop_disconnect(
    current_user: CurrentUser = Depends(require_role(*_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    conn = (
        await db.execute(
            select(MarketplaceConnection).where(
                MarketplaceConnection.tenant_id == current_user.tenant_id,
                MarketplaceConnection.provider == "tiktokshop",
            )
        )
    ).scalar_one_or_none()
    if not conn:
        return {"success": False}
    conn.status = "disconnected"
    await db.commit()
    return {"success": True}


__all__ = ["router"]
