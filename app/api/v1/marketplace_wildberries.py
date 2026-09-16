"""
Wildberries connect/status/disconnect routes — messaging-only scope
(2026-09-15). No /callback route — a single long-lived API token
generated per-seller in the WB seller portal, submitted directly via
/connect, same shape as this repo's Walmart connector. No webhook route
— signing scheme wasn't researched for this pass.
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
from app.services.marketplaces.connectors.wildberries import wildberries_connector
from app.services.marketplaces.crypto import encrypt_secret

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/marketplaces/wildberries", tags=["marketplaces"])
_ADMIN_ROLES = ("admin", "tenant_admin")


class WildberriesConnectRequest(BaseModel):
    api_token: str
    environment: str = "sandbox"


@router.post("/connect")
async def wildberries_connect(
    body: WildberriesConnectRequest,
    current_user: CurrentUser = Depends(require_role(*_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    settings = get_settings()
    if not settings.WILDBERRIES_ENABLED:
        return {"error": "Wildberries integration is not enabled on this deployment"}

    result = await wildberries_connector.connect(str(current_user.tenant_id), {
        "api_token": body.api_token, "environment": body.environment,
    })
    if not result.success:
        return {"success": False, "error": result.error}

    credentials = {
        "api_token": encrypt_secret(body.api_token),
        "environment": body.environment,
    }
    existing = (
        await db.execute(
            select(MarketplaceConnection).where(
                MarketplaceConnection.tenant_id == current_user.tenant_id,
                MarketplaceConnection.provider == "wildberries",
            )
        )
    ).scalar_one_or_none()
    if existing:
        existing.credentials = credentials
        existing.status = "connected"
    else:
        db.add(MarketplaceConnection(
            tenant_id=current_user.tenant_id,
            provider="wildberries",
            credentials=credentials,
            status="connected",
            messaging_capability=MessagingCapability.FULL.value,
        ))
    await db.commit()
    return {"success": True}


@router.get("/connection")
async def wildberries_connection_status(
    current_user: CurrentUser = Depends(require_role(*_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    conn = (
        await db.execute(
            select(MarketplaceConnection).where(
                MarketplaceConnection.tenant_id == current_user.tenant_id,
                MarketplaceConnection.provider == "wildberries",
            )
        )
    ).scalar_one_or_none()
    if not conn:
        return {"connection": None}
    return {"connection": {"status": conn.status, "messaging_capability": conn.messaging_capability}}


@router.delete("/connection")
async def wildberries_disconnect(
    current_user: CurrentUser = Depends(require_role(*_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    conn = (
        await db.execute(
            select(MarketplaceConnection).where(
                MarketplaceConnection.tenant_id == current_user.tenant_id,
                MarketplaceConnection.provider == "wildberries",
            )
        )
    ).scalar_one_or_none()
    if not conn:
        return {"success": False}
    conn.status = "disconnected"
    await db.commit()
    return {"success": True}


__all__ = ["router"]
