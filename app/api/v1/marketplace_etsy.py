"""Etsy connect/callback/status/disconnect routes — pilot batch #5 (last of
the pilot). PKCE code_verifier stored in Redis alongside the OAuth state
(both needed at token-exchange time) — see connectors/etsy.py's module
docstring for why Etsy needs this and Shopify/Amazon/eBay don't. No webhook
route (see connectors/etsy.py's parse_webhook() docstring).
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
from app.services.marketplaces.connectors.etsy import etsy_connector, generate_pkce_pair
from app.services.marketplaces.crypto import encrypt_secret

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/marketplaces/etsy", tags=["marketplaces"])
_STATE_TTL_SECONDS = 600
_ADMIN_ROLES = ("admin", "tenant_admin")


@router.post("/connect")
async def etsy_connect(
    current_user: CurrentUser = Depends(require_role(*_ADMIN_ROLES)),
    redis=Depends(get_redis),
) -> dict:
    settings = get_settings()
    if not settings.ETSY_ENABLED or not settings.ETSY_CLIENT_ID:
        return {"error": "Etsy integration is not configured on this deployment"}

    state = str(uuid.uuid4())
    code_verifier, code_challenge = generate_pkce_pair()
    await redis.setex(
        f"etsy_oauth_state:{state}", _STATE_TTL_SECONDS,
        json.dumps({"tenant_id": str(current_user.tenant_id), "code_verifier": code_verifier}),
    )
    return {"authorize_url": etsy_connector.authorize_url(state, code_challenge)}


@router.get("/callback")
async def etsy_callback(request: Request, db: AsyncSession = Depends(get_db), redis=Depends(get_redis)):
    settings = get_settings()
    # See marketplace_shopify.py's callback for why this must be the
    # frontend's own origin, not a bare relative path (2026-09-14 fix).
    frontend_admin = f"{settings.ITSM_FRONTEND_URL}/itsm/admin"
    args = dict(request.query_params)
    state = args.get("state")
    raw_entry = await redis.get(f"etsy_oauth_state:{state}") if state else None
    if not raw_entry:
        return RedirectResponse(f"{frontend_admin}?etsy_error=invalid_state")
    await redis.delete(f"etsy_oauth_state:{state}")
    entry = json.loads(raw_entry)

    # Etsy puts its own `error` param on the redirect when consent itself
    # was rejected — surface it instead of falling through to the generic
    # "missing_code" label (same masking bug found + fixed in eBay's
    # callback, 2026-09-14).
    if args.get("error"):
        return RedirectResponse(f"{frontend_admin}?etsy_error={args['error']}")

    code = args.get("code")
    if not code:
        return RedirectResponse(f"{frontend_admin}?etsy_error=missing_code")

    result = await etsy_connector.connect(entry["tenant_id"], {"code": code, "code_verifier": entry["code_verifier"]})
    if not result.success:
        return RedirectResponse(f"{frontend_admin}?etsy_error={result.error}")

    # connect() already exchanged `code` and hands back the raw payload via
    # result.credentials — re-exchanging it here a second time (the old
    # approach) would fail, Etsy's authorization code is single-use (same
    # class of bug fixed in marketplace_shopify.py's callback, 2026-09-14).
    token_resp = result.credentials or {}
    access_token = token_resp.get("access_token")
    refresh_token = token_resp.get("refresh_token")
    if not access_token or not refresh_token:
        return RedirectResponse(f"{frontend_admin}?etsy_error={token_resp.get('error', 'token_failed')}")

    expires_in = token_resp.get("expires_in")
    credentials = {
        "access_token": encrypt_secret(access_token),
        "refresh_token": encrypt_secret(refresh_token),
        "access_token_expires_at": (
            (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat() if expires_in else None
        ),
    }
    shop_id = access_token.split(".")[0]

    existing = (
        await db.execute(
            select(MarketplaceConnection).where(
                MarketplaceConnection.tenant_id == entry["tenant_id"],
                MarketplaceConnection.provider == "etsy",
            )
        )
    ).scalar_one_or_none()
    if existing:
        existing.external_id = shop_id
        existing.credentials = credentials
        existing.status = "connected"
    else:
        db.add(MarketplaceConnection(
            tenant_id=entry["tenant_id"],
            provider="etsy",
            external_id=shop_id,
            credentials=credentials,
            status="connected",
            messaging_capability=MessagingCapability.NONE.value,
        ))
    await db.commit()
    return RedirectResponse(f"{frontend_admin}?connected=etsy")


@router.get("/connection")
async def etsy_connection_status(
    current_user: CurrentUser = Depends(require_role(*_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    conn = (
        await db.execute(
            select(MarketplaceConnection).where(
                MarketplaceConnection.tenant_id == current_user.tenant_id,
                MarketplaceConnection.provider == "etsy",
            )
        )
    ).scalar_one_or_none()
    if not conn:
        return {"connection": None}
    return {"connection": {"external_id": conn.external_id, "status": conn.status, "messaging_capability": conn.messaging_capability}}


@router.delete("/connection")
async def etsy_disconnect(
    current_user: CurrentUser = Depends(require_role(*_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    conn = (
        await db.execute(
            select(MarketplaceConnection).where(
                MarketplaceConnection.tenant_id == current_user.tenant_id,
                MarketplaceConnection.provider == "etsy",
            )
        )
    ).scalar_one_or_none()
    if not conn:
        return {"success": False}
    conn.status = "disconnected"
    await db.commit()
    return {"success": True}


__all__ = ["router"]
