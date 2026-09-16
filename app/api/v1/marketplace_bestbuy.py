"""Best Buy Marketplace connect/status/disconnect routes — messaging-only
scope (2026-09-16). No /callback route — Mirakl uses a single seller-issued
API key, not an OAuth redirect/consent flow; the tenant submits it directly
via /connect, same shape as this repo's Cdiscount/Wildberries connectors.
No webhook route — Mirakl's webhook signing scheme wasn't researched for
this pass.
"""

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentUser, require_role
from app.config import get_settings
from app.database import get_db
from app.models.marketplace import MarketplaceConnection
from app.services.marketplaces.connectors.base import MessagingCapability
from app.services.marketplaces.connectors.bestbuy import bestbuy_connector
from app.services.marketplaces.crypto import encrypt_secret

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/marketplaces/bestbuy", tags=["marketplaces"])
_ADMIN_ROLES = ("admin", "tenant_admin")


class BestBuyConnectRequest(BaseModel):
    api_key: str


@router.post("/connect")
async def bestbuy_connect(
    body: BestBuyConnectRequest,
    current_user: CurrentUser = Depends(require_role(*_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    settings = get_settings()
    if not settings.BESTBUY_ENABLED:
        return {"error": "Best Buy Marketplace integration is not enabled on this deployment"}

    result = await bestbuy_connector.connect(str(current_user.tenant_id), {"api_key": body.api_key})
    if not result.success:
        return {"success": False, "error": result.error}

    credentials = {"api_key": encrypt_secret(body.api_key)}
    existing = (
        await db.execute(
            select(MarketplaceConnection).where(
                MarketplaceConnection.tenant_id == current_user.tenant_id,
                MarketplaceConnection.provider == "bestbuy",
            )
        )
    ).scalar_one_or_none()
    if existing:
        existing.credentials = credentials
        existing.status = "connected"
    else:
        db.add(MarketplaceConnection(
            tenant_id=current_user.tenant_id,
            provider="bestbuy",
            credentials=credentials,
            status="connected",
            messaging_capability=MessagingCapability.FULL.value,
        ))
    await db.commit()
    return {"success": True}


@router.get("/connection")
async def bestbuy_connection_status(
    current_user: CurrentUser = Depends(require_role(*_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    conn = (
        await db.execute(
            select(MarketplaceConnection).where(
                MarketplaceConnection.tenant_id == current_user.tenant_id,
                MarketplaceConnection.provider == "bestbuy",
            )
        )
    ).scalar_one_or_none()
    if not conn:
        return {"connection": None}
    return {"connection": {"status": conn.status, "messaging_capability": conn.messaging_capability}}


@router.delete("/connection")
async def bestbuy_disconnect(
    current_user: CurrentUser = Depends(require_role(*_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    conn = (
        await db.execute(
            select(MarketplaceConnection).where(
                MarketplaceConnection.tenant_id == current_user.tenant_id,
                MarketplaceConnection.provider == "bestbuy",
            )
        )
    ).scalar_one_or_none()
    if not conn:
        return {"success": False}
    conn.status = "disconnected"
    await db.commit()
    return {"success": True}


__all__ = ["router"]
