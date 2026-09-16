"""Allegro connect/callback/status/disconnect routes — messaging-only scope
(2026-09-15), see connectors/allegro.py's module docstring. Standard OAuth2
authorization-code flow with a genuinely separate sandbox domain (not just
a flag on production). No webhook route — Allegro Webhooks' signature
scheme wasn't researched for this pass.
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
from app.services.marketplaces.connectors.allegro import allegro_connector
from app.services.marketplaces.connectors.base import MessagingCapability
from app.services.marketplaces.crypto import encrypt_secret

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/marketplaces/allegro", tags=["marketplaces"])
_STATE_TTL_SECONDS = 600
_ADMIN_ROLES = ("admin", "tenant_admin")


@router.post("/connect")
async def allegro_connect(
    current_user: CurrentUser = Depends(require_role(*_ADMIN_ROLES)),
    redis=Depends(get_redis),
) -> dict:
    settings = get_settings()
    if not settings.ALLEGRO_ENABLED or not settings.ALLEGRO_CLIENT_ID:
        return {"error": "Allegro integration is not configured on this deployment"}

    state = str(uuid.uuid4())
    await redis.setex(f"allegro_oauth_state:{state}", _STATE_TTL_SECONDS, json.dumps({"tenant_id": str(current_user.tenant_id)}))
    return {"authorize_url": allegro_connector.authorize_url(state)}


@router.get("/callback")
async def allegro_callback(request: Request, db: AsyncSession = Depends(get_db), redis=Depends(get_redis)):
    settings = get_settings()
    frontend_admin = f"{settings.ITSM_FRONTEND_URL}/itsm/admin"
    args = dict(request.query_params)
    state = args.get("state")
    raw_entry = await redis.get(f"allegro_oauth_state:{state}") if state else None
    if not raw_entry:
        return RedirectResponse(f"{frontend_admin}?allegro_error=invalid_state")
    await redis.delete(f"allegro_oauth_state:{state}")
    entry = json.loads(raw_entry)

    if args.get("error"):
        return RedirectResponse(f"{frontend_admin}?allegro_error={args['error']}")

    code = args.get("code")
    if not code:
        return RedirectResponse(f"{frontend_admin}?allegro_error=missing_code")

    result = await allegro_connector.connect(entry["tenant_id"], {"code": code})
    if not result.success:
        return RedirectResponse(f"{frontend_admin}?allegro_error={result.error}")

    token_resp = result.credentials or {}
    access_token = token_resp.get("access_token")
    refresh_token = token_resp.get("refresh_token")
    if not access_token or not refresh_token:
        return RedirectResponse(f"{frontend_admin}?allegro_error={token_resp.get('error', 'token_failed')}")

    expires_in = token_resp.get("expires_in")
    credentials = {
        "access_token": encrypt_secret(access_token),
        "refresh_token": encrypt_secret(refresh_token),
        "access_token_expires_at": (
            (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat() if expires_in else None
        ),
    }

    existing = (
        await db.execute(
            select(MarketplaceConnection).where(
                MarketplaceConnection.tenant_id == entry["tenant_id"],
                MarketplaceConnection.provider == "allegro",
            )
        )
    ).scalar_one_or_none()
    if existing:
        existing.credentials = credentials
        existing.status = "connected"
    else:
        db.add(MarketplaceConnection(
            tenant_id=entry["tenant_id"],
            provider="allegro",
            credentials=credentials,
            status="connected",
            messaging_capability=MessagingCapability.FULL.value,
        ))
    await db.commit()
    return RedirectResponse(f"{frontend_admin}?connected=allegro")


@router.get("/connection")
async def allegro_connection_status(
    current_user: CurrentUser = Depends(require_role(*_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    conn = (
        await db.execute(
            select(MarketplaceConnection).where(
                MarketplaceConnection.tenant_id == current_user.tenant_id,
                MarketplaceConnection.provider == "allegro",
            )
        )
    ).scalar_one_or_none()
    if not conn:
        return {"connection": None}
    return {"connection": {"status": conn.status, "messaging_capability": conn.messaging_capability}}


@router.delete("/connection")
async def allegro_disconnect(
    current_user: CurrentUser = Depends(require_role(*_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    conn = (
        await db.execute(
            select(MarketplaceConnection).where(
                MarketplaceConnection.tenant_id == current_user.tenant_id,
                MarketplaceConnection.provider == "allegro",
            )
        )
    ).scalar_one_or_none()
    if not conn:
        return {"success": False}
    conn.status = "disconnected"
    await db.commit()
    return {"success": True}


__all__ = ["router"]
