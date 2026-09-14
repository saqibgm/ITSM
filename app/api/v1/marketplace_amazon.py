"""
Amazon connect/callback/status/disconnect routes — pilot batch #2, per
docs/plans/NATIVE_MARKETPLACE_CONNECTORS_PLAN.md §5. No webhook route here —
Amazon has no inbound HTTP webhook mechanism (see connectors/amazon.py's
module docstring); the manual "sync now" endpoint (once added) is this
connector's only sync path until an SQS consumer is built as a separate
mechanism.

OAuth state via Redis, same rationale as marketplace_shopify.py — multiple
API workers, in-process dict would only work if /connect and /callback land
on the same one.
"""

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentUser, require_role
from app.config import get_settings
from app.database import get_db
from app.models.marketplace import MarketplaceConnection
from app.redis_client import get_redis
from app.services.marketplaces.connectors.amazon import amazon_connector
from app.services.marketplaces.connectors.base import MessagingCapability
from app.services.marketplaces.crypto import encrypt_secret

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/marketplaces/amazon", tags=["marketplaces"])

_STATE_TTL_SECONDS = 600
_ADMIN_ROLES = ("admin", "tenant_admin")


@router.post("/connect")
async def amazon_connect(
    current_user: CurrentUser = Depends(require_role(*_ADMIN_ROLES)),
    redis=Depends(get_redis),
) -> dict:
    settings = get_settings()
    if not settings.AMAZON_ENABLED or not settings.AMAZON_APP_ID:
        return JSONResponse(status_code=503, content={"error": "Amazon integration is not configured on this deployment"})

    state = str(uuid.uuid4())
    await redis.setex(
        f"amazon_oauth_state:{state}", _STATE_TTL_SECONDS, json.dumps({"tenant_id": str(current_user.tenant_id)})
    )
    return {"authorize_url": amazon_connector.authorize_url(state)}


@router.get("/callback")
async def amazon_callback(request: Request, db: AsyncSession = Depends(get_db), redis=Depends(get_redis)):
    """Amazon redirects here with state, spapi_oauth_code, selling_partner_id.
    spapi_oauth_code expires 5 minutes after issuance — exchanged immediately."""
    settings = get_settings()
    # See marketplace_shopify.py's callback for why this must be the
    # frontend's own origin, not a bare relative path (2026-09-14 fix).
    frontend_admin = f"{settings.ITSM_FRONTEND_URL}/itsm/admin"
    args = dict(request.query_params)
    state = args.get("state")
    raw_entry = await redis.get(f"amazon_oauth_state:{state}") if state else None
    if not raw_entry:
        return RedirectResponse(f"{frontend_admin}?amazon_error=invalid_state")
    await redis.delete(f"amazon_oauth_state:{state}")
    entry = json.loads(raw_entry)

    # Amazon puts its own `error` param on the redirect when consent itself
    # was rejected — surface it instead of falling through to the generic
    # "missing_code" label (same masking bug found + fixed in eBay's
    # callback, 2026-09-14).
    if args.get("error"):
        return RedirectResponse(f"{frontend_admin}?amazon_error={args['error']}")

    code = args.get("spapi_oauth_code")
    seller_id = args.get("selling_partner_id")
    if not code:
        return RedirectResponse(f"{frontend_admin}?amazon_error=missing_code")

    result = await amazon_connector.connect(entry["tenant_id"], {
        "spapi_oauth_code": code, "selling_partner_id": seller_id,
    })
    if not result.success:
        return RedirectResponse(f"{frontend_admin}?amazon_error={result.error}")

    # connect() already exchanged spapi_oauth_code and hands back the raw
    # payload via result.credentials — re-exchanging it here a second time
    # (the old approach) would fail, since spapi_oauth_code is single-use
    # and expires 5 minutes after issuance (same class of bug fixed in
    # marketplace_shopify.py's callback, 2026-09-14).
    token_resp = result.credentials or {}
    access_token = token_resp.get("access_token")
    refresh_token = token_resp.get("refresh_token")
    if not access_token or not refresh_token:
        return RedirectResponse(f"{frontend_admin}?amazon_error={token_resp.get('error', 'token_failed')}")

    expires_in = token_resp.get("expires_in")
    credentials = {
        "seller_id": seller_id,
        "marketplace_ids": settings.AMAZON_MARKETPLACE_IDS,
        "access_token": encrypt_secret(access_token),
        # Unlike Shopify, Amazon's refresh_token does NOT rotate/expire on
        # its own — stored once at connect time, never touched by a refresh.
        "refresh_token": encrypt_secret(refresh_token),
        "access_token_expires_at": (
            (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat() if expires_in else None
        ),
    }

    existing = (
        await db.execute(
            select(MarketplaceConnection).where(
                MarketplaceConnection.tenant_id == entry["tenant_id"],
                MarketplaceConnection.provider == "amazon",
            )
        )
    ).scalar_one_or_none()
    if existing:
        existing.external_id = seller_id
        existing.credentials = credentials
        existing.status = "connected"
    else:
        db.add(MarketplaceConnection(
            tenant_id=entry["tenant_id"],
            provider="amazon",
            external_id=seller_id,
            credentials=credentials,
            status="connected",
            messaging_capability=MessagingCapability.OUTBOUND_ONLY.value,
        ))
    await db.commit()

    logger.info("[Amazon] tenant %s connected seller %s", entry["tenant_id"], seller_id)
    return RedirectResponse(f"{frontend_admin}?connected=amazon")


@router.get("/connection")
async def amazon_connection_status(
    current_user: CurrentUser = Depends(require_role(*_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    conn = (
        await db.execute(
            select(MarketplaceConnection).where(
                MarketplaceConnection.tenant_id == current_user.tenant_id,
                MarketplaceConnection.provider == "amazon",
            )
        )
    ).scalar_one_or_none()
    if not conn:
        return {"connection": None}
    return {
        "connection": {
            "external_id": conn.external_id,
            "status": conn.status,
            "messaging_capability": conn.messaging_capability,
            "last_synced_at": conn.last_synced_at.isoformat() if conn.last_synced_at else None,
        }
    }


@router.delete("/connection")
async def amazon_disconnect(
    current_user: CurrentUser = Depends(require_role(*_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    conn = (
        await db.execute(
            select(MarketplaceConnection).where(
                MarketplaceConnection.tenant_id == current_user.tenant_id,
                MarketplaceConnection.provider == "amazon",
            )
        )
    ).scalar_one_or_none()
    if not conn:
        return {"success": False}
    conn.status = "disconnected"
    await db.commit()
    return {"success": True}


__all__ = ["router"]
