"""
Manual "sync now" endpoint — the manual half of "auto and manual add/update"
(the original requirement this whole module exists to satisfy). One route,
works across all 5 connectors via the registry, rather than duplicating a
sync endpoint per provider's route file.

Calls the SAME ingestion.map_order()/map_return_to_ticket() functions the
Celery task (tasks_marketplace_sync.py) calls for the webhook/auto path —
per the plan's design, "auto" and "manual" are two invocations of one
mapping layer, not two implementations to keep in sync. Messaging sync isn't
included here yet (kind=orders|returns only) — every connector's
send_message() is still unverified/stub (see each connector's module
docstring), so there's nothing safe to trigger manually for that yet either.
"""

import logging
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentUser, require_role
from app.database import get_db
from app.models.marketplace import MarketplaceConnection
from app.redis_client import get_redis
from app.services.marketplaces import ingestion
from app.services.marketplaces.registry import get_connector
from app.services.marketplaces.system_user import get_or_create_marketplace_bot_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/marketplaces", tags=["marketplaces"])
_ADMIN_ROLES = ("admin", "tenant_admin", "agent")  # read/trigger, not connect/disconnect — matches integrations.py's _READ_ROLES shape


@router.post("/{provider}/sync")
async def sync_marketplace_now(
    provider: str,
    kind: Literal["orders", "returns"] = Query("orders"),
    current_user: CurrentUser = Depends(require_role(*_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
) -> dict:
    try:
        connector = get_connector(provider)
    except ValueError:
        return {"error": f"unknown provider '{provider}'"}

    connection = (
        await db.execute(
            select(MarketplaceConnection).where(
                MarketplaceConnection.tenant_id == current_user.tenant_id,
                MarketplaceConnection.provider == provider,
                MarketplaceConnection.status == "connected",
            )
        )
    ).scalar_one_or_none()
    if connection is None:
        return {"error": f"no connected {provider} account for this tenant"}

    if kind == "orders":
        orders = await connector.fetch_orders(connection)
        synced = 0
        for normalized in orders:
            await ingestion.map_order(db, connection, current_user.tenant_id, normalized)
            synced += 1
        await db.commit()
        return {"synced": synced, "kind": "orders", "provider": provider}

    # kind == "returns"
    returns = await connector.fetch_returns(connection)
    bot_user_id = await get_or_create_marketplace_bot_user(db, current_user.tenant_id)
    created = 0
    skipped = 0
    for normalized in returns:
        link = await ingestion.map_return_to_ticket(
            db, connection, current_user.tenant_id, bot_user_id, redis, normalized
        )
        if link is not None:
            created += 1
        else:
            skipped += 1
    await db.commit()
    return {"tickets_created": created, "skipped_no_matching_order": skipped, "kind": "returns", "provider": provider}


__all__ = ["router"]
