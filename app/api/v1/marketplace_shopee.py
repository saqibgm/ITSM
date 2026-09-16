"""Shopee connect/callback/status/disconnect routes — messaging-only scope
(2026-09-16). Standard OAuth2-ish authorization-code flow, but every step
(including the initial authorize redirect) is additionally HMAC-signed —
see connectors/shopee.py's module docstring. No webhook route — Shopee's
push-notification signing scheme wasn't researched for this pass.
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
from app.services.marketplaces.connectors.shopee import shopee_connector
from app.services.marketplaces.crypto import encrypt_secret

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/marketplaces/shopee", tags=["marketplaces"])
_STATE_TTL_SECONDS = 600
_ADMIN_ROLES = ("admin", "tenant_admin")


@router.post("/connect")
async def shopee_connect(
    current_user: CurrentUser = Depends(require_role(*_ADMIN_ROLES)),
    redis=Depends(get_redis),
) -> dict:
    settings = get_settings()
    if not settings.SHOPEE_ENABLED or not settings.SHOPEE_PARTNER_ID:
        return {"error": "Shopee integration is not configured on this deployment"}

    state = str(uuid.uuid4())
    await redis.setex(f"shopee_oauth_state:{state}", _STATE_TTL_SECONDS, json.dumps({"tenant_id": str(current_user.tenant_id)}))
    return {"authorize_url": shopee_connector.authorize_url(state)}


@router.get("/callback")
async def shopee_callback(request: Request, db: AsyncSession = Depends(get_db), redis=Depends(get_redis)):
    settings = get_settings()
    frontend_admin = f"{settings.ITSM_FRONTEND_URL}/itsm/admin"
    args = dict(request.query_params)
    state = args.get("state")
    raw_entry = await redis.get(f"shopee_oauth_state:{state}") if state else None
    if not raw_entry:
        return RedirectResponse(f"{frontend_admin}?shopee_error=invalid_state")
    await redis.delete(f"shopee_oauth_state:{state}")
    entry = json.loads(raw_entry)

    code = args.get("code")
    shop_id = args.get("shop_id")
    if not code or not shop_id:
        return RedirectResponse(f"{frontend_admin}?shopee_error=missing_code_or_shop_id")

    result = await shopee_connector.connect(entry["tenant_id"], {"code": code, "shop_id": shop_id})
    if not result.success:
        return RedirectResponse(f"{frontend_admin}?shopee_error={result.error}")

    token_resp = result.credentials or {}
    access_token = token_resp.get("access_token")
    refresh_token = token_resp.get("refresh_token")
    if not access_token or not refresh_token:
        return RedirectResponse(f"{frontend_admin}?shopee_error={token_resp.get('message', 'token_failed')}")

    expires_in = token_resp.get("expire_in") or token_resp.get("expires_in")
    credentials = {
        "shop_id": shop_id,
        "access_token": encrypt_secret(access_token),
        "refresh_token": encrypt_secret(refresh_token),
        "access_token_expires_at": (
            (datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))).isoformat() if expires_in else None
        ),
    }

    existing = (
        await db.execute(
            select(MarketplaceConnection).where(
                MarketplaceConnection.tenant_id == entry["tenant_id"],
                MarketplaceConnection.provider == "shopee",
            )
        )
    ).scalar_one_or_none()
    if existing:
        existing.credentials = credentials
        existing.external_id = shop_id
        existing.status = "connected"
    else:
        db.add(MarketplaceConnection(
            tenant_id=entry["tenant_id"],
            provider="shopee",
            external_id=shop_id,
            credentials=credentials,
            status="connected",
            messaging_capability=MessagingCapability.FULL.value,
        ))
    await db.commit()
    return RedirectResponse(f"{frontend_admin}?connected=shopee")


@router.get("/connection")
async def shopee_connection_status(
    current_user: CurrentUser = Depends(require_role(*_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    conn = (
        await db.execute(
            select(MarketplaceConnection).where(
                MarketplaceConnection.tenant_id == current_user.tenant_id,
                MarketplaceConnection.provider == "shopee",
            )
        )
    ).scalar_one_or_none()
    if not conn:
        return {"connection": None}
    return {"connection": {"status": conn.status, "messaging_capability": conn.messaging_capability}}


@router.delete("/connection")
async def shopee_disconnect(
    current_user: CurrentUser = Depends(require_role(*_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    conn = (
        await db.execute(
            select(MarketplaceConnection).where(
                MarketplaceConnection.tenant_id == current_user.tenant_id,
                MarketplaceConnection.provider == "shopee",
            )
        )
    ).scalar_one_or_none()
    if not conn:
        return {"success": False}
    conn.status = "disconnected"
    await db.commit()
    return {"success": True}


__all__ = ["router"]
