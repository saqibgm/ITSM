"""
bol.com connect/status/disconnect routes — thin, email-fallback-only scope
(2026-09-15). No /callback route — client_credentials grant, no redirect
flow, tenant submits their own client_id/client_secret directly, same
shape as this repo's Walmart connector. No webhook route.
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
from app.services.marketplaces.connectors.bolcom import bolcom_connector
from app.services.marketplaces.crypto import encrypt_secret

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/marketplaces/bolcom", tags=["marketplaces"])
_ADMIN_ROLES = ("admin", "tenant_admin")


class BolComConnectRequest(BaseModel):
    client_id: str
    client_secret: str


@router.post("/connect")
async def bolcom_connect(
    body: BolComConnectRequest,
    current_user: CurrentUser = Depends(require_role(*_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    settings = get_settings()
    if not settings.BOLCOM_ENABLED:
        return {"error": "bol.com integration is not enabled on this deployment"}

    result = await bolcom_connector.connect(str(current_user.tenant_id), {
        "client_id": body.client_id, "client_secret": body.client_secret,
    })
    if not result.success:
        return {"success": False, "error": result.error}

    credentials = {
        "client_id": encrypt_secret(body.client_id),
        "client_secret": encrypt_secret(body.client_secret),
    }
    existing = (
        await db.execute(
            select(MarketplaceConnection).where(
                MarketplaceConnection.tenant_id == current_user.tenant_id,
                MarketplaceConnection.provider == "bolcom",
            )
        )
    ).scalar_one_or_none()
    if existing:
        existing.credentials = credentials
        existing.status = "connected"
    else:
        db.add(MarketplaceConnection(
            tenant_id=current_user.tenant_id,
            provider="bolcom",
            external_id=body.client_id,
            credentials=credentials,
            status="connected",
            messaging_capability=MessagingCapability.NONE.value,
        ))
    await db.commit()
    return {"success": True}


@router.get("/connection")
async def bolcom_connection_status(
    current_user: CurrentUser = Depends(require_role(*_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    conn = (
        await db.execute(
            select(MarketplaceConnection).where(
                MarketplaceConnection.tenant_id == current_user.tenant_id,
                MarketplaceConnection.provider == "bolcom",
            )
        )
    ).scalar_one_or_none()
    if not conn:
        return {"connection": None}
    return {"connection": {"status": conn.status, "messaging_capability": conn.messaging_capability}}


@router.delete("/connection")
async def bolcom_disconnect(
    current_user: CurrentUser = Depends(require_role(*_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    conn = (
        await db.execute(
            select(MarketplaceConnection).where(
                MarketplaceConnection.tenant_id == current_user.tenant_id,
                MarketplaceConnection.provider == "bolcom",
            )
        )
    ).scalar_one_or_none()
    if not conn:
        return {"success": False}
    conn.status = "disconnected"
    await db.commit()
    return {"success": True}


__all__ = ["router"]
