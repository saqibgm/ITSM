"""Temu connect/callback/status/disconnect routes — orders/returns only,
no messaging API (2026-09-18). Standard OAuth-style authorization-code
flow (see connectors/temu.py's module docstring for the confidence caveat
on the exact authorize URL), but every API call — including the token
exchange/refresh calls themselves — also needs Temu's MD5 request-signing,
handled inside connectors/temu.py, not here. No webhook route — Temu has
no confirmed messaging mechanism to notify on.
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
from app.services.marketplaces.connectors.temu import temu_connector
from app.services.marketplaces.crypto import encrypt_secret

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/marketplaces/temu", tags=["marketplaces"])
_STATE_TTL_SECONDS = 600
_ADMIN_ROLES = ("admin", "tenant_admin")


@router.post("/connect")
async def temu_connect(
    current_user: CurrentUser = Depends(require_role(*_ADMIN_ROLES)),
    redis=Depends(get_redis),
) -> dict:
    settings = get_settings()
    if not settings.TEMU_ENABLED or not settings.TEMU_CLIENT_ID:
        return {"error": "Temu integration is not configured on this deployment"}

    state = str(uuid.uuid4())
    await redis.setex(f"temu_oauth_state:{state}", _STATE_TTL_SECONDS, json.dumps({"tenant_id": str(current_user.tenant_id)}))
    return {"authorize_url": temu_connector.authorize_url(state)}


@router.get("/callback")
async def temu_callback(request: Request, db: AsyncSession = Depends(get_db), redis=Depends(get_redis)):
    settings = get_settings()
    frontend_admin = f"{settings.ITSM_FRONTEND_URL}/itsm/admin"
    args = dict(request.query_params)
    state = args.get("state")
    raw_entry = await redis.get(f"temu_oauth_state:{state}") if state else None
    if not raw_entry:
        return RedirectResponse(f"{frontend_admin}?temu_error=invalid_state")
    await redis.delete(f"temu_oauth_state:{state}")
    entry = json.loads(raw_entry)

    if args.get("error"):
        return RedirectResponse(f"{frontend_admin}?temu_error={args['error']}")

    code = args.get("code")
    if not code:
        return RedirectResponse(f"{frontend_admin}?temu_error=missing_code")

    result = await temu_connector.connect(entry["tenant_id"], {"code": code})
    if not result.success:
        return RedirectResponse(f"{frontend_admin}?temu_error={result.error}")

    token_resp = result.credentials or {}
    access_token = token_resp.get("access_token")
    if not access_token:
        return RedirectResponse(f"{frontend_admin}?temu_error=token_failed")

    expires_in = token_resp.get("expires_in") or token_resp.get("access_token_expire_in")
    credentials = {
        "access_token": encrypt_secret(access_token),
        "access_token_expires_at": (
            (datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))).isoformat() if expires_in else None
        ),
    }
    if token_resp.get("refresh_token"):
        credentials["refresh_token"] = encrypt_secret(token_resp["refresh_token"])

    existing = (
        await db.execute(
            select(MarketplaceConnection).where(
                MarketplaceConnection.tenant_id == entry["tenant_id"],
                MarketplaceConnection.provider == "temu",
            )
        )
    ).scalar_one_or_none()
    if existing:
        existing.external_id = result.external_id
        existing.credentials = credentials
        existing.status = "connected"
    else:
        db.add(MarketplaceConnection(
            tenant_id=entry["tenant_id"],
            provider="temu",
            external_id=result.external_id,
            credentials=credentials,
            status="connected",
            messaging_capability=MessagingCapability.NONE.value,
        ))
    await db.commit()
    return RedirectResponse(f"{frontend_admin}?connected=temu")


@router.get("/connection")
async def temu_connection_status(
    current_user: CurrentUser = Depends(require_role(*_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    conn = (
        await db.execute(
            select(MarketplaceConnection).where(
                MarketplaceConnection.tenant_id == current_user.tenant_id,
                MarketplaceConnection.provider == "temu",
            )
        )
    ).scalar_one_or_none()
    if not conn:
        return {"connection": None}
    return {"connection": {"external_id": conn.external_id, "status": conn.status, "messaging_capability": conn.messaging_capability}}


@router.delete("/connection")
async def temu_disconnect(
    current_user: CurrentUser = Depends(require_role(*_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    conn = (
        await db.execute(
            select(MarketplaceConnection).where(
                MarketplaceConnection.tenant_id == current_user.tenant_id,
                MarketplaceConnection.provider == "temu",
            )
        )
    ).scalar_one_or_none()
    if not conn:
        return {"success": False}
    conn.status = "disconnected"
    await db.commit()
    return {"success": True}


__all__ = ["router"]
