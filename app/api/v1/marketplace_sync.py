"""
Manual "sync now" endpoint — the manual half of "auto and manual add/update"
(the original requirement this whole module exists to satisfy). One route,
works across all 5 connectors via the registry, rather than duplicating a
sync endpoint per provider's route file.

Calls the SAME ingestion.map_order()/map_return_to_ticket() functions the
Celery task (tasks_marketplace_sync.py) calls for the webhook/auto path —
per the plan's design, "auto" and "manual" are two invocations of one
mapping layer, not two implementations to keep in sync.

Outbound messaging (POST /orders/{order_id}/send-message, 2026-09-14) is
real code against each connector's actual send_message() implementation,
but both currently-wired connectors hit an external blocker independent of
this code: Amazon gets a 403 (Messaging role not granted to this app in the
Solution Provider Portal) and eBay's endpoint is documented as unsupported
in sandbox entirely. Shopify/Etsy/Walmart have no messaging API at all
(confirmed platform limitations, not gaps) — messaging_capability == NONE
short-circuits those before ever calling send_message(). Inbound messaging
sync isn't included here — would need a new fetch for eBay's separate
Inquiry resource (distinct from the Return/cancellation Case resource
fetch_returns() already syncs), and there's no real inquiry data in any
connected sandbox account yet to build and verify that against.
"""

import logging
from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentUser, require_role
from app.database import get_db
from app.models.marketplace import MarketplaceConnection, MarketplaceOrder, MarketplaceOrderTicketLink
from app.models.ticket import Ticket, TicketComment, TicketStatus
from app.redis_client import get_redis
from app.services.marketplaces import ingestion
from app.services.marketplaces.connectors.base import MessagingCapability
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

    # Batch-fetch this tenant's connections once (not per-row) — order_url()
    # needs the owning connection for provider-specific fields like
    # Shopify's shop_domain (see connectors/shopify.py's order_url()).
    connections_by_id = {
        c.id: c for c in (
            await db.execute(select(MarketplaceConnection).where(MarketplaceConnection.tenant_id == current_user.tenant_id))
        ).scalars().all()
    }

    def _order_url(o: MarketplaceOrder) -> Optional[str]:
        connection = connections_by_id.get(o.connection_id)
        if connection is None:
            return None
        try:
            return get_connector(o.provider).order_url(connection, o.external_order_id)
        except ValueError:
            return None

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
                "buyer_name": o.buyer_name,
                "placed_at": o.placed_at.isoformat() if o.placed_at else None,
                "updated_at": o.updated_at.isoformat(),
                "external_url": _order_url(o),
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

    # Same batch-fetch-connections approach as list_marketplace_orders above.
    connections_by_id = {
        c.id: c for c in (
            await db.execute(select(MarketplaceConnection).where(MarketplaceConnection.tenant_id == current_user.tenant_id))
        ).scalars().all()
    }

    def _order_url(order: MarketplaceOrder) -> Optional[str]:
        connection = connections_by_id.get(order.connection_id)
        if connection is None:
            return None
        try:
            return get_connector(order.provider).order_url(connection, order.external_order_id)
        except ValueError:
            return None

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
                    "external_url": _order_url(order),
                },
            }
            for link, ticket, order in rows
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


async def _sync_provider_orders(db: AsyncSession, connection: MarketplaceConnection, tenant_id) -> int:
    """One connector's order sync — shared by the per-provider and
    sync-all-connected routes below so there's one implementation, not two
    (same "auto/manual are two invocations of one mapping layer" principle
    this module's docstring states for ingestion.py itself)."""
    connector = get_connector(connection.provider)
    orders = await connector.fetch_orders(connection)
    synced = 0
    for normalized in orders:
        await ingestion.map_order(db, connection, tenant_id, normalized)
        synced += 1
    await db.commit()
    return synced


async def _sync_provider_returns(
    db: AsyncSession, connection: MarketplaceConnection, tenant_id, bot_user_id, redis
) -> tuple[int, int]:
    """One connector's return/replacement sync — returns (created, skipped)."""
    connector = get_connector(connection.provider)
    returns = await connector.fetch_returns(connection)
    created = 0
    skipped = 0
    for normalized in returns:
        link = await ingestion.map_return_to_ticket(db, connection, tenant_id, bot_user_id, redis, normalized)
        if link is not None:
            created += 1
        else:
            skipped += 1
    await db.commit()
    return created, skipped


@router.post("/sync")
async def sync_all_marketplaces(
    kind: Literal["orders", "returns"] = Query("orders"),
    current_user: CurrentUser = Depends(require_role(*_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
) -> dict:
    """Syncs EVERY connected marketplace for this tenant in one call — the
    "Sync" button on the combined Orders/Returns pages (as opposed to
    /{provider}/sync below, which is the admin Marketplaces section's
    per-connector trigger). Registered before /{provider}/sync in this file
    even though the differing path shapes (2 segments vs 3) mean there's no
    real routing ambiguity — kept for the same static-before-parametric
    convention the GET /orders and /returns routes above already follow.

    Re-running this updates existing records rather than duplicating them:
    map_order() upserts by (tenant_id, provider, external_order_id), and
    map_return_to_ticket() uses the return case's external_case_id as the
    ticket's idempotency_key — so a case that already produced a ticket
    won't produce a second one on a repeat sync.
    """
    connections = (
        await db.execute(
            select(MarketplaceConnection).where(
                MarketplaceConnection.tenant_id == current_user.tenant_id,
                MarketplaceConnection.status == "connected",
            )
        )
    ).scalars().all()

    results = []
    if kind == "orders":
        for connection in connections:
            try:
                synced = await _sync_provider_orders(db, connection, current_user.tenant_id)
                results.append({"provider": connection.provider, "synced": synced})
            except Exception:
                logger.error("marketplace_sync_all_orders_failed", extra={"provider": connection.provider}, exc_info=True)
                results.append({"provider": connection.provider, "error": "sync_failed"})
        return {"kind": "orders", "results": results}

    # kind == "returns"
    bot_user_id = await get_or_create_marketplace_bot_user(db, current_user.tenant_id)
    for connection in connections:
        try:
            created, skipped = await _sync_provider_returns(db, connection, current_user.tenant_id, bot_user_id, redis)
            results.append({"provider": connection.provider, "tickets_created": created, "skipped_no_matching_order": skipped})
        except Exception:
            logger.error("marketplace_sync_all_returns_failed", extra={"provider": connection.provider}, exc_info=True)
            results.append({"provider": connection.provider, "error": "sync_failed"})
    return {"kind": "returns", "results": results}


@router.post("/{provider}/sync")
async def sync_marketplace_now(
    provider: str,
    kind: Literal["orders", "returns"] = Query("orders"),
    current_user: CurrentUser = Depends(require_role(*_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
) -> dict:
    try:
        get_connector(provider)  # validates provider is known before touching the DB
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
        synced = await _sync_provider_orders(db, connection, current_user.tenant_id)
        return {"synced": synced, "kind": "orders", "provider": provider}

    # kind == "returns"
    bot_user_id = await get_or_create_marketplace_bot_user(db, current_user.tenant_id)
    created, skipped = await _sync_provider_returns(db, connection, current_user.tenant_id, bot_user_id, redis)
    return {"tickets_created": created, "skipped_no_matching_order": skipped, "kind": "returns", "provider": provider}


class SendMessageRequest(BaseModel):
    message: str


@router.post("/orders/{order_id}/send-message")
async def send_message_to_buyer(
    order_id: UUID,
    body: SendMessageRequest,
    current_user: CurrentUser = Depends(require_role(*_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Outbound messaging (capability #3's send half) — an agent's reply on
    a ticket, relayed to the buyer through whichever marketplace the linked
    order came from. See this module's docstring for the real, currently
    external blockers on both connectors that otherwise have code here
    (Amazon: 403, missing Messaging role grant; eBay: sandbox-unsupported
    endpoint) — this route works correctly today, it just can't complete a
    live send against either connected sandbox account yet.
    """
    order = (
        await db.execute(
            select(MarketplaceOrder).where(
                MarketplaceOrder.id == order_id,
                MarketplaceOrder.tenant_id == current_user.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if order is None:
        return {"error": "order not found"}

    connection = (
        await db.execute(select(MarketplaceConnection).where(MarketplaceConnection.id == order.connection_id))
    ).scalar_one_or_none()
    if connection is None or connection.status != "connected":
        return {"error": f"{order.provider} is not connected for this tenant"}

    try:
        connector = get_connector(order.provider)
    except ValueError:
        return {"error": f"unknown provider '{order.provider}'"}

    if connector.messaging_capability == MessagingCapability.NONE:
        return {"error": f"{order.provider} has no buyer-messaging capability (confirmed platform limitation, not a gap)"}

    result = await connector.send_message(connection, order.external_order_id, body.message)
    if not result.success:
        return {"error": result.error}

    # Record it on the linked ticket (if any) so every agent sees the
    # outbound message in the same thread as everything else about this
    # return/replacement — same reasoning as why marketplace comments land
    # as ordinary TicketComments rather than a separate messaging inbox
    # (see ingestion.map_message_to_comment's docstring, plan §4.5).
    link = (
        await db.execute(
            select(MarketplaceOrderTicketLink).where(MarketplaceOrderTicketLink.order_id == order.id)
        )
    ).scalars().first()
    if link is not None and current_user.local_user_id is not None:
        db.add(TicketComment(
            ticket_id=link.ticket_id,
            author_id=current_user.local_user_id,
            body=body.message,
            is_internal=False,
        ))
        await db.commit()

    return {"success": True, "external_message_id": result.external_message_id}


__all__ = ["router"]
