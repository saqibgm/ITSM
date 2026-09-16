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

GET /messages (2026-09-14) is the read side of a dedicated cross-order
Messaging page — every send now also writes a MarketplaceMessage row
(migration 0041), independent of whether the order has a linked return/
replacement ticket. Before this, an outbound send on a plain order (no
ticket) recorded nothing queryable at all once sent.
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
from app.models.marketplace import MarketplaceConnection, MarketplaceMessage, MarketplaceOrder, MarketplaceOrderTicketLink
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


async def _sync_provider_messages(db: AsyncSession, connection: MarketplaceConnection, tenant_id) -> int:
    """One connector's inbound message sync (2026-09-15) — meaningful for
    every connector whose fetch_messages() isn't the base-class default
    empty list (currently eBay, Mercado Libre, Allegro — see each
    connector's module docstring). Loops the tenant's already-synced
    orders for this connection and pulls each one's message thread — none
    of these APIs expose a single 'give me everything since X' feed to
    page through the way orders/returns sync does; this is N calls for N
    orders (fine at this org's current sandbox order volume, worth
    revisiting before any real production scale, especially against
    eBay's documented 75-calls/60s Trading API rate limit).

    Direction (inbound vs outbound) is decided by each connector itself,
    not here — every connector's notion of "who sent this" is shaped
    differently (eBay: username; Mercado Libre: numeric user_id; Allegro:
    a role enum) to usefully compare generically. Each fetch_messages()
    implementation stashes the verdict in raw_metadata['direction'].
    """
    connector = get_connector(connection.provider)
    orders = (
        await db.execute(
            select(MarketplaceOrder).where(
                MarketplaceOrder.tenant_id == tenant_id,
                MarketplaceOrder.connection_id == connection.id,
            )
        )
    ).scalars().all()

    synced = 0
    for order in orders:
        messages = await connector.fetch_messages(connection, order)
        for normalized in messages:
            direction = (normalized.raw_metadata or {}).get("direction", "outbound")
            row = await ingestion.map_fetched_message(db, tenant_id, order, normalized, direction)
            if row is not None:
                synced += 1
    await db.commit()
    return synced


@router.post("/sync")
async def sync_all_marketplaces(
    kind: Literal["orders", "returns", "messages"] = Query("orders"),
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

    if kind == "returns":
        bot_user_id = await get_or_create_marketplace_bot_user(db, current_user.tenant_id)
        for connection in connections:
            try:
                created, skipped = await _sync_provider_returns(db, connection, current_user.tenant_id, bot_user_id, redis)
                results.append({"provider": connection.provider, "tickets_created": created, "skipped_no_matching_order": skipped})
            except Exception:
                logger.error("marketplace_sync_all_returns_failed", extra={"provider": connection.provider}, exc_info=True)
                results.append({"provider": connection.provider, "error": "sync_failed"})
        return {"kind": "returns", "results": results}

    # kind == "messages"
    for connection in connections:
        try:
            synced = await _sync_provider_messages(db, connection, current_user.tenant_id)
            results.append({"provider": connection.provider, "synced": synced})
        except Exception:
            logger.error("marketplace_sync_all_messages_failed", extra={"provider": connection.provider}, exc_info=True)
            results.append({"provider": connection.provider, "error": "sync_failed"})
    return {"kind": "messages", "results": results}


@router.post("/{provider}/sync")
async def sync_marketplace_now(
    provider: str,
    kind: Literal["orders", "returns", "messages"] = Query("orders"),
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

    if kind == "returns":
        bot_user_id = await get_or_create_marketplace_bot_user(db, current_user.tenant_id)
        created, skipped = await _sync_provider_returns(db, connection, current_user.tenant_id, bot_user_id, redis)
        return {"tickets_created": created, "skipped_no_matching_order": skipped, "kind": "returns", "provider": provider}

    # kind == "messages"
    synced = await _sync_provider_messages(db, connection, current_user.tenant_id)
    return {"synced": synced, "kind": "messages", "provider": provider}


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
    order came from, with an EMAIL FALLBACK (2026-09-14) when the native
    marketplace channel is unavailable or blocked and we have a real buyer
    email on file.

    This isn't a workaround bolted onto every connector — it's the same
    mechanism third-party helpdesks (eDesk et al.) actually use for
    marketplaces with no order-tied messaging API at all (Shopify, Etsy,
    Walmart — confirmed via direct doc research, connectors/*.py's own
    messaging_capability notes): a plain transactional email to the buyer's
    address already captured on the order (MarketplaceOrder.buyer_email),
    via this repo's existing send_email_notification Celery task — no
    marketplace API, no scope/permission wall, nothing to be blocked on.
    Works TODAY for Shopify/Etsy/Walmart, which all capture a genuine buyer
    email. Amazon/eBay's buyer_email is usually empty (PII-gated /not
    exposed at all respectively), so this fallback often can't fire for
    them yet — but their native send_message() is still tried FIRST, so
    nothing regresses once Amazon's Messaging role is granted or eBay moves
    to production.
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

    channel = None
    external_message_id = None
    native_error = None

    if connector.messaging_capability != MessagingCapability.NONE:
        result = await connector.send_message(connection, order, body.message)
        if result.success:
            channel = order.provider
            external_message_id = result.external_message_id
        else:
            native_error = result.error

    if channel is None:
        if not order.buyer_email:
            reason = f" ({native_error})" if native_error else " (confirmed platform limitation, not a gap)"
            return {"error": f"{order.provider} has no working messaging path for this order{reason} — no buyer email on record to fall back to either"}

        from app.workers.tasks_notifications import send_email_notification
        send_email_notification.delay(
            to_email=order.buyer_email,
            template_name="marketplace_order_message",
            context={
                "title": f"Message about your {order.provider} order",
                "body": body.message,
                "buyer_name": order.buyer_name,
                "provider": order.provider,
                "external_order_id": order.external_order_id,
            },
        )
        channel = "email"

    # Always record it in marketplace_messages (migration 0041) — the
    # queryable record behind the standalone Messaging page, independent of
    # whether this order has a linked return/replacement ticket. Before this
    # existed, a send on a plain order recorded nothing at all once it left
    # the request/response cycle.
    db.add(MarketplaceMessage(
        tenant_id=current_user.tenant_id,
        order_id=order.id,
        provider=order.provider,
        direction="outbound",
        body=body.message,
        external_message_id=external_message_id,
        sent_by_user_id=current_user.local_user_id,
    ))

    # ALSO record it on the linked ticket (if any) so every agent sees the
    # outbound message in the same thread as everything else about this
    # return/replacement — same reasoning as why marketplace comments land
    # as ordinary TicketComments rather than a separate messaging inbox
    # (see ingestion.map_message_to_comment's docstring, plan §4.5). This is
    # deliberately IN ADDITION TO the MarketplaceMessage row above, not
    # instead of it — the ticket thread and the Messaging page are two
    # different views onto the same event, not two different sources of truth.
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
    return {"success": True, "channel": channel, "external_message_id": external_message_id}


@router.get("/messages")
async def list_marketplace_messages(
    provider: Optional[str] = Query(None, description="Filter to one marketplace"),
    order_id: Optional[UUID] = Query(None, description="Filter to one order"),
    direction: Optional[Literal["outbound", "inbound"]] = Query(None),
    sent_from: Optional[datetime] = Query(None, description="Only messages sent on/after this timestamp"),
    sent_to: Optional[datetime] = Query(None, description="Only messages sent on/before this timestamp"),
    current_user: CurrentUser = Depends(require_role(*_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
) -> dict:
    """Cross-order Messaging page's read side — every MarketplaceMessage for
    this tenant, joined with its order for buyer/external-id/external_url
    context. See this module's top docstring for why this table exists
    separately from TicketComment."""
    conditions = [MarketplaceMessage.tenant_id == current_user.tenant_id]
    if provider:
        conditions.append(MarketplaceMessage.provider == provider)
    if order_id:
        conditions.append(MarketplaceMessage.order_id == order_id)
    if direction:
        conditions.append(MarketplaceMessage.direction == direction)
    if sent_from:
        conditions.append(MarketplaceMessage.sent_at >= sent_from)
    if sent_to:
        conditions.append(MarketplaceMessage.sent_at <= sent_to)

    query = (
        select(MarketplaceMessage, MarketplaceOrder)
        .join(MarketplaceOrder, MarketplaceOrder.id == MarketplaceMessage.order_id)
        .where(*conditions)
    )
    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    rows = (
        await db.execute(query.order_by(MarketplaceMessage.sent_at.desc()).limit(limit).offset(offset))
    ).all()

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
                "id": str(m.id),
                "direction": m.direction,
                "body": m.body,
                "sent_at": m.sent_at.isoformat(),
                "order": {
                    "id": str(order.id),
                    "provider": order.provider,
                    "external_order_id": order.external_order_id,
                    "external_url": _order_url(order),
                    "buyer_name": order.buyer_name,
                    "buyer_email": order.buyer_email,
                },
            }
            for m, order in rows
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


__all__ = ["router"]
