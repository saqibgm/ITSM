"""
Shopify connect/callback/webhook routes — first connector of the native
marketplace-integration module (V3-Marketplaces), per
docs/plans/NATIVE_MARKETPLACE_CONNECTORS_PLAN.md §5.

OAuth state storage uses Redis (get_redis), not the in-process dict the
chatbot repo's bp_shopify.py used — itsm-service runs multiple API workers,
so an in-process dict would only work for whichever worker happened to
receive the callback request, not necessarily the one that handled /connect.
Short TTL (10 min) covers how long Shopify's consent screen realistically
stays open; no sweep needed since it's Redis TTL, not a dict needing manual
expiry.
"""

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import RedirectResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentUser, require_role
from app.config import get_settings
from app.database import get_db
from app.models.marketplace import MarketplaceConnection, MarketplaceEvent
from app.redis_client import get_redis
from app.services.marketplaces.connectors.base import MessagingCapability
from app.services.marketplaces.connectors.shopify import shopify_connector
from app.services.marketplaces.crypto import encrypt_secret

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/marketplaces/shopify", tags=["marketplaces"])
webhook_router = APIRouter(tags=["marketplaces-webhooks"])

_STATE_TTL_SECONDS = 600
_ADMIN_ROLES = ("admin", "tenant_admin")


class ShopifyConnectRequest(BaseModel):
    # Shopify's OAuth authorize URL is per-shop (https://{shop_domain}/admin/
    # oauth/authorize) — the only one of the 5 connectors that needs an extra
    # field before /connect can even build a redirect URL. Was a bare
    # query-string `shop_domain: str` param before (2026-09-14 fix) — FastAPI
    # rejected an empty-body POST with a 422 whose body shape
    # ({"detail": [...]}）the frontend's generic error handler didn't expect,
    # surfacing as a literal "[object Object]" instead of a real message. A
    # proper request-body model fixes both the 422 shape and makes the
    # required field explicit rather than an easy-to-miss query param.
    shop_domain: str


@router.post("/connect")
async def shopify_connect(
    body: ShopifyConnectRequest,
    current_user: CurrentUser = Depends(require_role(*_ADMIN_ROLES)),
    redis=Depends(get_redis),
) -> dict:
    """Mint an OAuth state for the caller's tenant, return the Shopify
    authorize URL for the frontend to navigate the browser to (Shopify's
    consent screen requires a real top-level redirect, not XHR)."""
    settings = get_settings()
    if not settings.SHOPIFY_ENABLED or not settings.SHOPIFY_CLIENT_ID:
        return JSONResponse(status_code=503, content={"error": "Shopify integration is not configured on this deployment"})

    shop_domain = body.shop_domain.strip().lower()
    if not shop_domain.endswith(".myshopify.com"):
        return JSONResponse(status_code=400, content={"error": "shop_domain must look like 'your-store.myshopify.com'"})

    state = str(uuid.uuid4())
    await redis.setex(
        f"shopify_oauth_state:{state}",
        _STATE_TTL_SECONDS,
        json.dumps({"tenant_id": str(current_user.tenant_id), "shop_domain": shop_domain}),
    )
    return {"authorize_url": shopify_connector.authorize_url(shop_domain, state)}


@router.get("/callback")
async def shopify_callback(
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
):
    """Shopify redirects here after merchant consent — no auth dependency
    (Shopify calls this directly), state token is the security boundary
    instead, same shape as the chatbot repo's bp_shopify.shopify_callback."""
    settings = get_settings()
    args = dict(request.query_params)
    state = args.get("state")
    raw_entry = await redis.get(f"shopify_oauth_state:{state}") if state else None
    if not raw_entry:
        return RedirectResponse("/admin/marketplaces?shopify_error=invalid_state")
    await redis.delete(f"shopify_oauth_state:{state}")
    entry = json.loads(raw_entry)

    if not shopify_connector.verify_oauth_hmac(args, settings.SHOPIFY_CLIENT_SECRET):
        logger.warning("[Shopify] OAuth callback HMAC verification failed (state=%s)", state)
        return RedirectResponse("/admin/marketplaces?shopify_error=invalid_signature")

    code = args.get("code")
    shop_domain = args.get("shop") or entry["shop_domain"]
    if not code:
        return RedirectResponse("/admin/marketplaces?shopify_error=missing_code")

    result = await shopify_connector.connect(entry["tenant_id"], {"shop_domain": shop_domain, "code": code})
    if not result.success:
        return RedirectResponse(f"/admin/marketplaces?shopify_error={result.error}")

    # Re-exchange to get the full token payload for storage — connect() only
    # validates and returns success/shop_domain, doesn't persist (kept
    # deliberately DB-session-free so it stays testable in isolation).
    # TODO(cleanup): this re-does the HTTP call connect() already made;
    # acceptable for this scaffold, worth refactoring connect() to return the
    # full payload once a second connector's OAuth shape confirms the right
    # shared signature (Amazon's LWA flow returns a differently-shaped payload).
    settings_client = get_settings()
    import httpx
    async with httpx.AsyncClient(timeout=15.0) as client:
        token_resp = (await client.post(
            f"https://{shop_domain}/admin/oauth/access_token",
            json={
                "client_id": settings_client.SHOPIFY_CLIENT_ID,
                "client_secret": settings_client.SHOPIFY_CLIENT_SECRET,
                "code": code,
                "expiring": 1,
            },
        )).json()

    access_token = token_resp.get("access_token")
    if not access_token:
        return RedirectResponse(f"/admin/marketplaces?shopify_error={token_resp.get('error', 'token_failed')}")

    expires_in = token_resp.get("expires_in")
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat() if expires_in else None
    credentials = {
        "shop_domain": shop_domain,
        "access_token": encrypt_secret(access_token),
        "refresh_token": encrypt_secret(token_resp["refresh_token"]) if token_resp.get("refresh_token") else None,
        "access_token_expires_at": expires_at,
        "scopes": token_resp.get("scope", ""),
    }

    existing = (
        await db.execute(
            select(MarketplaceConnection).where(
                MarketplaceConnection.tenant_id == entry["tenant_id"],
                MarketplaceConnection.provider == "shopify",
            )
        )
    ).scalar_one_or_none()
    if existing:
        existing.external_id = shop_domain
        existing.credentials = credentials
        existing.status = "connected"
    else:
        db.add(MarketplaceConnection(
            tenant_id=entry["tenant_id"],
            provider="shopify",
            external_id=shop_domain,
            credentials=credentials,
            status="connected",
            messaging_capability=MessagingCapability.NONE.value,
        ))
    await db.commit()

    logger.info("[Shopify] tenant %s connected shop %s", entry["tenant_id"], shop_domain)
    return RedirectResponse("/admin/marketplaces?connected=shopify")


@router.get("/connection")
async def shopify_connection_status(
    current_user: CurrentUser = Depends(require_role(*_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    conn = (
        await db.execute(
            select(MarketplaceConnection).where(
                MarketplaceConnection.tenant_id == current_user.tenant_id,
                MarketplaceConnection.provider == "shopify",
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
async def shopify_disconnect(
    current_user: CurrentUser = Depends(require_role(*_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    conn = (
        await db.execute(
            select(MarketplaceConnection).where(
                MarketplaceConnection.tenant_id == current_user.tenant_id,
                MarketplaceConnection.provider == "shopify",
            )
        )
    ).scalar_one_or_none()
    if not conn:
        return {"success": False}
    conn.status = "disconnected"
    await db.commit()
    return {"success": True}


# ---------------------------------------------------------------------------
# Inbound webhook — no auth dependency, HMAC is the security boundary
# ---------------------------------------------------------------------------


@webhook_router.post("/webhooks/marketplace/shopify")
async def shopify_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    """Mirrors app/api/v1/webhooks.py's iam_webhook shape: read raw body →
    verify → idempotency check → log event → enqueue Celery task → return
    fast. Shopify's HMAC secret is the app-wide client_secret (one Shopify
    "app" shared across tenants — same model the OAuth flow uses), so this
    doesn't need a per-connection secret lookup before verifying, unlike a
    scheme where each tenant has their own webhook secret."""
    raw_body = await request.body()
    settings = get_settings()
    provided = request.headers.get("X-Shopify-Hmac-Sha256", "")

    if not shopify_connector.verify_webhook_hmac(raw_body, provided, settings.SHOPIFY_CLIENT_SECRET):
        logger.warning("shopify_webhook_hmac_failed")
        return JSONResponse(status_code=401, content={"error": "invalid signature"})

    topic = request.headers.get("X-Shopify-Topic", "")
    shop_domain = request.headers.get("X-Shopify-Shop-Domain", "")

    connection = (
        await db.execute(
            select(MarketplaceConnection).where(
                MarketplaceConnection.provider == "shopify",
                MarketplaceConnection.external_id == shop_domain,
            )
        )
    ).scalar_one_or_none()
    if connection is None:
        # Not one of our connected shops (or a GDPR shop/redact for an
        # already-disconnected/purged tenant) — 200 so Shopify doesn't retry.
        logger.info("shopify_webhook_unknown_shop", extra={"shop_domain": shop_domain})
        return JSONResponse(status_code=200, content={"received": True})

    payload = json.loads(raw_body) if raw_body else {}
    external_event_id = f"{topic}:{payload.get('id', uuid.uuid4())}"

    existing_event = (
        await db.execute(
            select(MarketplaceEvent).where(
                MarketplaceEvent.provider == "shopify",
                MarketplaceEvent.external_event_id == external_event_id,
            )
        )
    ).scalar_one_or_none()
    if existing_event is not None:
        return JSONResponse(status_code=200, content={"received": True})

    event = MarketplaceEvent(
        tenant_id=connection.tenant_id,
        connection_id=connection.id,
        provider="shopify",
        external_event_id=external_event_id,
        event_type=topic,
        payload=payload,
        status="received",
    )
    db.add(event)
    await db.commit()
    await db.refresh(event)

    from app.workers.tasks_marketplace_sync import process_marketplace_event
    process_marketplace_event.delay(str(event.id))

    return JSONResponse(status_code=200, content={"received": True})


__all__ = ["router", "webhook_router"]
