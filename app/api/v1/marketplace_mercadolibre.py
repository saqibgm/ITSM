"""Mercado Libre connect/callback/status/disconnect routes — messaging-only
scope (2026-09-15), see connectors/mercadolibre.py's module docstring.
Standard OAuth2 authorization-code flow, same shape as the eBay/Amazon
routes. No webhook route — signature scheme unresearched for this pass,
same conservative stance as eBay/Walmart.
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
from app.services.marketplaces.connectors.mercadolibre import mercadolibre_connector
from app.services.marketplaces.crypto import encrypt_secret

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/marketplaces/mercadolibre", tags=["marketplaces"])
_STATE_TTL_SECONDS = 600
_ADMIN_ROLES = ("admin", "tenant_admin")


@router.post("/connect")
async def mercadolibre_connect(
    current_user: CurrentUser = Depends(require_role(*_ADMIN_ROLES)),
    redis=Depends(get_redis),
) -> dict:
    settings = get_settings()
    if not settings.MERCADOLIBRE_ENABLED or not settings.MERCADOLIBRE_CLIENT_ID:
        return {"error": "Mercado Libre integration is not configured on this deployment"}

    state = str(uuid.uuid4())
    await redis.setex(
        f"mercadolibre_oauth_state:{state}", _STATE_TTL_SECONDS,
        json.dumps({"tenant_id": str(current_user.tenant_id)}),
    )
    return {"authorize_url": mercadolibre_connector.authorize_url(state)}


@router.get("/callback")
async def mercadolibre_callback(request: Request, db: AsyncSession = Depends(get_db), redis=Depends(get_redis)):
    settings = get_settings()
    # See marketplace_shopify.py's callback for why this must be the
    # frontend's own origin, not a bare relative path.
    frontend_admin = f"{settings.ITSM_FRONTEND_URL}/itsm/admin"
    args = dict(request.query_params)
    state = args.get("state")
    raw_entry = await redis.get(f"mercadolibre_oauth_state:{state}") if state else None
    if not raw_entry:
        return RedirectResponse(f"{frontend_admin}?mercadolibre_error=invalid_state")
    await redis.delete(f"mercadolibre_oauth_state:{state}")
    entry = json.loads(raw_entry)

    if args.get("error"):
        return RedirectResponse(f"{frontend_admin}?mercadolibre_error={args['error']}")

    code = args.get("code")
    if not code:
        return RedirectResponse(f"{frontend_admin}?mercadolibre_error=missing_code")

    result = await mercadolibre_connector.connect(entry["tenant_id"], {"code": code})
    if not result.success:
        return RedirectResponse(f"{frontend_admin}?mercadolibre_error={result.error}")

    token_resp = result.credentials or {}
    access_token = token_resp.get("access_token")
    refresh_token = token_resp.get("refresh_token")
    if not access_token or not refresh_token:
        return RedirectResponse(f"{frontend_admin}?mercadolibre_error={token_resp.get('error', 'token_failed')}")

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
                MarketplaceConnection.provider == "mercadolibre",
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
            provider="mercadolibre",
            external_id=result.external_id,
            credentials=credentials,
            status="connected",
            messaging_capability=MessagingCapability.FULL.value,
        ))
    await db.commit()
    return RedirectResponse(f"{frontend_admin}?connected=mercadolibre")


@router.get("/connection")
async def mercadolibre_connection_status(
    current_user: CurrentUser = Depends(require_role(*_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    conn = (
        await db.execute(
            select(MarketplaceConnection).where(
                MarketplaceConnection.tenant_id == current_user.tenant_id,
                MarketplaceConnection.provider == "mercadolibre",
            )
        )
    ).scalar_one_or_none()
    if not conn:
        return {"connection": None}
    return {"connection": {"external_id": conn.external_id, "status": conn.status, "messaging_capability": conn.messaging_capability}}


@router.delete("/connection")
async def mercadolibre_disconnect(
    current_user: CurrentUser = Depends(require_role(*_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    conn = (
        await db.execute(
            select(MarketplaceConnection).where(
                MarketplaceConnection.tenant_id == current_user.tenant_id,
                MarketplaceConnection.provider == "mercadolibre",
            )
        )
    ).scalar_one_or_none()
    if not conn:
        return {"success": False}
    conn.status = "disconnected"
    await db.commit()
    return {"success": True}


__all__ = ["router"]
