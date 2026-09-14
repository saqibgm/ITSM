"""
Connector-agnostic ingestion/mapping layer.

Per docs/plans/MARKETPLACE_ITSM_INTEGRATION_PLAN.md §2-§3 and
NATIVE_MARKETPLACE_CONNECTORS_PLAN.md §1: this is the ONE place a
NormalizedOrder/NormalizedReturn/NormalizedMessage (connectors/base.py) turns
into a MarketplaceOrder upsert, a Ticket + MarketplaceOrderTicketLink, or a
TicketComment. Called from both directions — the inbound webhook route (once
added) and the manual "sync now" endpoint (once added) — so "auto" and "manual"
are two invocations of this one mapping layer, not two implementations.

Deliberately NOT wired to any connector yet (Phase 1 scaffold, per the plan's
roadmap — connectors land in Phase 2). A few real decisions are left as TODOs
below rather than guessed at, following this codebase's own documented pattern
(SHOPIFY_INTEGRATION_PLAN.md, AMAZON_INTEGRATION_PLAN.md) of nailing exact
business rules against a live connector rather than in the abstract:

- requester_id for a marketplace-triggered ticket (TicketService.create_ticket
  requires one) — needs a per-tenant "system"/service identity convention,
  resolved once Phase 2's first connector (Amazon or Shopify) is being wired in.
- category_id / priority defaults for return/replacement tickets — placeholder
  values below; real defaults are a business decision (plan §3, §10 decision 2),
  not an engineering one.
"""

import logging
from typing import Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.marketplace import MarketplaceConnection, MarketplaceOrder, MarketplaceOrderTicketLink
from app.models.ticket import TicketComment, TicketPriority, TicketType
from app.services.marketplaces.connectors.base import NormalizedMessage, NormalizedOrder, NormalizedReturn
from app.services.ticket_service import CreateTicketData, TicketService

logger = logging.getLogger(__name__)

_ticket_service = TicketService()


async def map_order(
    db: AsyncSession, connection: MarketplaceConnection, tenant_id: UUID, order: NormalizedOrder
) -> MarketplaceOrder:
    """Silent upsert — capability #1. Does not create a Ticket by default,
    per the plan's conservative-default event-mapping table (§3): routine
    order events are high-volume and not an ops concern on their own.
    """
    existing = (
        await db.execute(
            select(MarketplaceOrder).where(
                MarketplaceOrder.tenant_id == tenant_id,
                MarketplaceOrder.provider == connection.provider,
                MarketplaceOrder.external_order_id == order.external_order_id,
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        existing.status = order.status
        existing.order_lines = order.order_lines
        existing.total_amount = order.total_amount
        existing.currency = order.currency
        existing.buyer_email = order.buyer_email or existing.buyer_email
        existing.buyer_name = order.buyer_name or existing.buyer_name
        existing.raw_metadata = order.raw_metadata
        return existing

    row = MarketplaceOrder(
        tenant_id=tenant_id,
        connection_id=connection.id,
        provider=connection.provider,
        external_order_id=order.external_order_id,
        status=order.status,
        buyer_email=order.buyer_email,
        buyer_name=order.buyer_name,
        order_lines=order.order_lines,
        total_amount=order.total_amount,
        currency=order.currency,
        placed_at=order.placed_at,
        raw_metadata=order.raw_metadata,
    )
    db.add(row)
    await db.flush()
    return row


async def map_return_to_ticket(
    db: AsyncSession,
    connection: MarketplaceConnection,
    tenant_id: UUID,
    requester_id: UUID,  # TODO(Phase 2): resolve a per-tenant system identity here
    redis,
    return_case: NormalizedReturn,
) -> Optional[MarketplaceOrderTicketLink]:
    """Return/replacement sync — capability #2. Auto-creates a service_request
    Ticket linked to the order via MarketplaceOrderTicketLink, per §3's default
    mapping. Reuses TicketService.create_ticket (idempotency_key=the case's
    external id) rather than inserting a Ticket row directly, so this ticket
    gets the exact same SLA-assignment/automation-rule/outbound-webhook
    machinery any other ticket does — that's the whole point of routing
    through the real ticket-creation path instead of a bespoke insert.
    """
    order = (
        await db.execute(
            select(MarketplaceOrder).where(
                MarketplaceOrder.tenant_id == tenant_id,
                MarketplaceOrder.provider == connection.provider,
                MarketplaceOrder.external_order_id == return_case.external_order_id,
            )
        )
    ).scalar_one_or_none()

    if order is None:
        # A return/replacement event arrived before (or without) its order ever
        # syncing — log and skip rather than guess at a synthetic order. Real
        # handling (backfill the order first?) is a Phase 2 decision once this
        # is observed against a live connector, not designed blind here.
        logger.warning(
            "marketplace_return_no_matching_order",
            extra={"provider": connection.provider, "external_order_id": return_case.external_order_id},
        )
        return None

    data = CreateTicketData(
        title=f"{return_case.link_type.capitalize()} requested — order {return_case.external_order_id} ({connection.provider})",
        description=return_case.reason or "(no reason provided by marketplace)",
        type=TicketType.service_request,
        priority=TicketPriority.medium,  # TODO: real default is a business decision, plan §10 item 2
        idempotency_key=f"marketplace:{connection.provider}:{return_case.external_case_id}",
    )
    ticket = await _ticket_service.create_ticket(tenant_id, requester_id, data, redis, db)

    link = MarketplaceOrderTicketLink(
        order_id=order.id, ticket_id=ticket.id, link_type=return_case.link_type
    )
    db.add(link)
    await db.flush()
    return link


async def map_message_to_comment(
    db: AsyncSession, tenant_id: UUID, marketplace_bot_user_id: UUID, message: NormalizedMessage
) -> Optional[TicketComment]:
    """Messaging sync (inbound half of capability #3). Finds the ticket linked
    to the message's order/case via MarketplaceOrderTicketLink and appends a
    TicketComment — the existing ticket stays the single place an agent reads
    the conversation, no separate messaging inbox (per plan §4.5).

    ``marketplace_bot_user_id``: TicketComment.author_id is a required FK to a
    real User row (RESTRICT, no NULL) — there's no 'external author' concept in
    the current schema. Rather than add one (author-type discriminator), this
    resolves to a synthetic per-tenant "Marketplace" system user, provisioned
    once per tenant (TODO Phase 2: establish that provisioning path — likely
    alongside tenant onboarding, same place SLA policies/TenantSequence rows
    get seeded in the IAM webhook's _provision_tenant()). No separate
    idempotency column needed on TicketComment itself — MarketplaceEvent's
    existing UNIQUE(provider, external_event_id) constraint already prevents
    a duplicated inbound message from being processed twice upstream of this
    call.

    If no linked ticket exists yet (a pre-return buyer inquiry), the plan's §3
    table calls for auto-creating a holding ticket — not implemented in this
    scaffold; needs a real connector's actual message shape to design against.
    """
    link = None
    if message.external_order_id:
        order = (
            await db.execute(
                select(MarketplaceOrder).where(
                    MarketplaceOrder.tenant_id == tenant_id,
                    MarketplaceOrder.external_order_id == message.external_order_id,
                )
            )
        ).scalar_one_or_none()
        if order is not None:
            link = (
                await db.execute(
                    select(MarketplaceOrderTicketLink).where(
                        MarketplaceOrderTicketLink.order_id == order.id
                    )
                )
            ).scalars().first()

    if link is None:
        logger.warning(
            "marketplace_message_no_linked_ticket",
            extra={"external_order_id": message.external_order_id, "external_case_id": message.external_case_id},
        )
        return None

    comment = TicketComment(
        ticket_id=link.ticket_id,
        author_id=marketplace_bot_user_id,
        body=message.body,
        is_internal=False,
    )
    db.add(comment)
    await db.flush()
    return comment


__all__ = ["map_order", "map_return_to_ticket", "map_message_to_comment"]
