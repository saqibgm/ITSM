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
from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentUser, require_role
from app.database import get_db
from app.models.marketplace import MarketplaceConnection, MarketplaceOrder, MarketplaceOrderTicketLink
from app.models.ticket import Ticket, TicketStatus
from app.redis_client import get_redis
from app.services.marketplaces import ingestion
from app.services.marketplaces.registry import get_connector
from app.services.marketplaces.system_user import get_or_create_marketplace_bot_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/marketplaces", tags=["marketplaces"])
_ADMIN_ROLES = ("admin", "tenant_admin", "agent")  # read/trigger, not connect/disconnect — matches integrations.py's _READ_ROLES shape


# ---------------------------------------------------------------------------
# List endpoints — the backend half of the "combined orders" / "returns &
# replacement requests" pages. Registered before the parametric
# /{provider}/sync route (static-path-first ordering, same convention as
# tickets.py), even though there's no actual collision today since these are
# GET and /{provider}/sync is POST.
# ---------------------------------------------------------------------------


@router.get("/orders")
async def list_marketplace_orders(
    provider: Optional[str] = Query(None, description="Filter to one marketplace, e.g. 'shopify'"),
    status: Optional[str] = Query(None, description="new | acknowledged | shipped | delivered | cancelled"),
    placed_from: Optional[datetime] = Query(None, description="Only orders placed on/after this timestamp (ISO 8601)"),
    placed_to: Optional[datetime] = Query(None, description="Only orders placed on/before this timestamp (ISO 8601)"),
    current_user: CurrentUser = Depends(require_role(*_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
) -> dict:
    """Combined orders view across every connected marketplace for this
    tenant — the read side of capability #1, backing the frontend's Orders
    page. Filters: marketplace (provider), status, and a placed-date range —
    the three the frontend's filter bar needs."""
    conditions = [MarketplaceOrder.tenant_id == current_user.tenant_id]
    if provider:
        conditions.append(MarketplaceOrder.provider == provider)
    if status:
        conditions.append(MarketplaceOrder.status == status)
    if placed_from:
        conditions.append(MarketplaceOrder.placed_at >= placed_from)
    if placed_to:
        conditions.append(MarketplaceOrder.placed_at <= placed_to)

    total = (
        await db.execute(select(func.count()).select_from(MarketplaceOrder).where(*conditions))
    ).scalar_one()
    rows = (
        await db.execute(
            select(MarketplaceOrder)
            .where(*conditions)
            .order_by(MarketplaceOrder.placed_at.desc().nullslast())
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()

    return {
        "items": [
            {
                "id": str(o.id),
                "provider": o.provider,
                "external_order_id": o.external_order_id,
                "status": o.status,
                "total_amount": float(o.total_amount) if o.total_amount is not None else None,
                "currency": o.currency,
                "buyer_email": o.buyer_email,
                "placed_at": o.placed_at.isoformat() if o.placed_at else None,
                "updated_at": o.updated_at.isoformat(),
            }
            for o in rows
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/returns")
async def list_marketplace_returns(
    provider: Optional[str] = Query(None, description="Filter to one marketplace"),
    link_type: Optional[Literal["return", "replacement"]] = Query(None, description="Return vs. replacement requests"),
    ticket_status: Optional[TicketStatus] = Query(None, description="Filter by the linked ticket's workflow status"),
    linked_from: Optional[datetime] = Query(None, description="Only requests raised on/after this timestamp"),
    linked_to: Optional[datetime] = Query(None, description="Only requests raised on/before this timestamp"),
    current_user: CurrentUser = Depends(require_role(*_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
) -> dict:
    """Return/replacement requests — capability #2's read side, backing the
    frontend's dedicated Returns page (kept separate from the regular
    TicketsPage per the decision to give marketplace-triggered work its own
    view). Joins MarketplaceOrderTicketLink -> Ticket + MarketplaceOrder;
    tenant-scoped via the order/ticket rows since the link table itself
    carries no tenant_id (same precedent as AssetTicketLink). Filters:
    marketplace, return vs. replacement, the linked ticket's own workflow
    status (open/in_progress/resolved/etc — distinct from the marketplace
    order's own status), and a date range on when the request was raised."""
    query = (
        select(MarketplaceOrderTicketLink, Ticket, MarketplaceOrder)
        .join(Ticket, Ticket.id == MarketplaceOrderTicketLink.ticket_id)
        .join(MarketplaceOrder, MarketplaceOrder.id == MarketplaceOrderTicketLink.order_id)
        .where(MarketplaceOrder.tenant_id == current_user.tenant_id)
    )
    if provider:
        query = query.where(MarketplaceOrder.provider == provider)
    if link_type:
        query = query.where(MarketplaceOrderTicketLink.link_type == link_type)
    if ticket_status:
        query = query.where(Ticket.status == ticket_status)
    if linked_from:
        query = query.where(MarketplaceOrderTicketLink.linked_at >= linked_from)
    if linked_to:
        query = query.where(MarketplaceOrderTicketLink.linked_at <= linked_to)

    count_query = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_query)).scalar_one()

    rows = (
        await db.execute(
            query.order_by(MarketplaceOrderTicketLink.linked_at.desc()).limit(limit).offset(offset)
        )
    ).all()

    return {
        "items": [
            {
                "link_type": link.link_type,
                "linked_at": link.linked_at.isoformat(),
                "ticket": {
                    "id": str(ticket.id),
                    "ticket_number": ticket.ticket_number,
                    "title": ticket.title,
                    "status": ticket.status.value if hasattr(ticket.status, "value") else ticket.status,
                    "priority": ticket.priority.value if hasattr(ticket.priority, "value") else ticket.priority,
                },
                "order": {
                    "id": str(order.id),
                    "provider": order.provider,
                    "external_order_id": order.external_order_id,
                },
            }
            for link, ticket, order in rows
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


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
