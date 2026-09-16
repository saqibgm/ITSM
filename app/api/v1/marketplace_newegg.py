"""Newegg connect/status/disconnect routes — thin, email-fallback-only
scope (2026-09-16). No /callback route — static seller_id/api_key/secret_key
credentials, not an OAuth redirect/consent flow; submitted directly via
/connect, same shape as this repo's Cdiscount connector. No webhook route —
Newegg's API has no messaging capability to notify on (see
connectors/newegg.py's module docstring).
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
from app.services.marketplaces.connectors.newegg import newegg_connector
from app.services.marketplaces.crypto import encrypt_secret

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/marketplaces/newegg", tags=["marketplaces"])
_ADMIN_ROLES = ("admin", "tenant_admin")


class NeweggConnectRequest(BaseModel):
    seller_id: str
    api_key: str
    secret_key: str


@router.post("/connect")
async def newegg_connect(
    body: NeweggConnectRequest,
    current_user: CurrentUser = Depends(require_role(*_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    settings = get_settings()
    if not settings.NEWEGG_ENABLED:
        return {"error": "Newegg integration is not enabled on this deployment"}

    result = await newegg_connector.connect(str(current_user.tenant_id), {
        "seller_id": body.seller_id, "api_key": body.api_key, "secret_key": body.secret_key,
    })
    if not result.success:
        return {"success": False, "error": result.error}

    credentials = {
        "api_key": encrypt_secret(body.api_key),
        "secret_key": encrypt_secret(body.secret_key),
    }
    existing = (
        await db.execute(
            select(MarketplaceConnection).where(
                MarketplaceConnection.tenant_id == current_user.tenant_id,
                MarketplaceConnection.provider == "newegg",
            )
        )
    ).scalar_one_or_none()
    if existing:
        existing.credentials = credentials
        existing.external_id = body.seller_id
        existing.status = "connected"
    else:
        db.add(MarketplaceConnection(
            tenant_id=current_user.tenant_id,
            provider="newegg",
            external_id=body.seller_id,
            credentials=credentials,
            status="connected",
            messaging_capability=MessagingCapability.NONE.value,
        ))
    await db.commit()
    return {"success": True}


@router.get("/connection")
async def newegg_connection_status(
    current_user: CurrentUser = Depends(require_role(*_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    conn = (
        await db.execute(
            select(MarketplaceConnection).where(
                MarketplaceConnection.tenant_id == current_user.tenant_id,
                MarketplaceConnection.provider == "newegg",
            )
        )
    ).scalar_one_or_none()
    if not conn:
        return {"connection": None}
    return {"connection": {"status": conn.status, "messaging_capability": conn.messaging_capability}}


@router.delete("/connection")
async def newegg_disconnect(
    current_user: CurrentUser = Depends(require_role(*_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    conn = (
        await db.execute(
            select(MarketplaceConnection).where(
                MarketplaceConnection.tenant_id == current_user.tenant_id,
                MarketplaceConnection.provider == "newegg",
            )
        )
    ).scalar_one_or_none()
    if not conn:
        return {"success": False}
    conn.status = "disconnected"
    await db.commit()
    return {"success": True}


__all__ = ["router"]
