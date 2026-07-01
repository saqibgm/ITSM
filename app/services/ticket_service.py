"""Ticket business logic — state machine, SLA, idempotency."""

import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import DuplicateIdempotencyKeyError, InvalidStateTransitionError
from app.models.ticket import Ticket, TicketPriority, TicketStatus, TicketType
from app.repositories.ticket_repo import TicketRepository

logger = logging.getLogger(__name__)

_IDEMPOTENCY_TTL = 86_400  # 24 hours


@dataclass
class CreateTicketData:
    title: str
    description: str
    type: TicketType
    priority: TicketPriority
    category_id: UUID | None = None
    product_id: UUID | None = None
    # Optional product passed by slug+name (e.g. from the chatbot's product-scoped
    # session). When product_id is absent and a slug is given, create_ticket upserts
    # a Product row for (tenant, slug) and links it. Null slug = no product (blank).
    product_slug: str | None = None
    product_name: str | None = None
    department_id: UUID | None = None
    idempotency_key: str | None = None


class TicketService:

    async def create_ticket(
        self,
        tenant_id: UUID,
        requester_id: UUID,
        data: CreateTicketData,
        redis: Any,
        db: AsyncSession,
    ) -> Ticket:
        repo = TicketRepository(db)

        if data.idempotency_key:
            redis_key = f"idempotency:{tenant_id}:{data.idempotency_key}"

            if redis is not None:
                try:
                    cached = await redis.get(redis_key)
                    if cached:
                        raise DuplicateIdempotencyKeyError(
                            cached_response={"ticket_id": cached.decode()}
                        )
                except DuplicateIdempotencyKeyError:
                    raise
                except Exception:
                    pass

        ticket_number = await repo.next_ticket_number()

        sla_policy_id = None
        team_id = None

        if data.category_id is not None:
            from app.models.ticket import TicketCategory

            cat_result = await db.execute(
                select(TicketCategory).where(
                    TicketCategory.id == data.category_id,
                    TicketCategory.tenant_id == tenant_id,
                )
            )
            cat = cat_result.scalar_one_or_none()
            if cat is not None:
                sla_policy_id = cat.default_sla_policy_id
                team_id = cat.default_team_id

        # Resolve product by slug when no explicit product_id was given: upsert a
        # Product row for (tenant, slug). A null slug leaves the ticket productless.
        product_id = data.product_id
        if product_id is None and data.product_slug:
            from app.models.identity import Product

            existing = await db.execute(
                select(Product).where(
                    Product.tenant_id == tenant_id,
                    Product.slug == data.product_slug,
                )
            )
            product = existing.scalar_one_or_none()
            if product is None:
                product = Product(
                    tenant_id=tenant_id,
                    slug=data.product_slug,
                    name=data.product_name or data.product_slug,
                    is_active=True,
                )
                db.add(product)
                await db.flush()
            product_id = product.id

        ticket = Ticket(
            tenant_id=tenant_id,
            requester_id=requester_id,
            ticket_number=ticket_number,
            type=data.type,
            status=TicketStatus.open,
            priority=data.priority,
            title=data.title,
            description=data.description,
            category_id=data.category_id,
            product_id=product_id,
            department_id=data.department_id,
            sla_policy_id=sla_policy_id,
            team_id=team_id,
            idempotency_key=data.idempotency_key,
        )
        db.add(ticket)
        await db.flush()
        await db.refresh(ticket)

        if data.idempotency_key and redis is not None:
            try:
                await redis.set(
                    f"idempotency:{tenant_id}:{data.idempotency_key}",
                    str(ticket.id),
                    ex=_IDEMPOTENCY_TTL,
                )
            except Exception:
                pass

        await repo.record_history(
            ticket_id=ticket.id,
            actor_id=None,
            field="status",
            old_val=None,
            new_val=TicketStatus.open.value,
        )

        await repo.add_watcher(ticket_id=ticket.id, user_id=requester_id)

        # SLA runtime (Phase 7 / S7.2): if a tenant sla_rule matches this ticket,
        # open per-target sla_instances. Purely ADDITIVE — no rules configured →
        # no-op, and the legacy sla_* fields / breach worker are unaffected. A
        # matching error must never block ticket creation.
        try:
            await self._open_sla_instances(db, ticket)
        except Exception:
            logger.warning("sla_instance_open_failed", extra={"ticket_id": str(ticket.id)})

        # Fire automation rules asynchronously — never let errors propagate
        try:
            from app.workers.tasks_automation import run_ticket_automation
            run_ticket_automation.delay(str(ticket.id), "ticket_created")
        except Exception:
            pass

        # Dispatch outbound webhook event — never let errors propagate
        try:
            from app.workers.tasks_webhooks import dispatch_webhook_event
            dispatch_webhook_event.delay(
                str(ticket.tenant_id),
                "ticket.created",
                {
                    "id": str(ticket.id),
                    "ticket_number": ticket.ticket_number,
                    "status": ticket.status.value,
                    "priority": ticket.priority.value,
                },
            )
        except Exception:
            pass

        return ticket

    async def _open_sla_instances(self, db: AsyncSession, ticket: Ticket) -> None:
        """Match the ticket against the tenant's priority-ordered sla_rules and,
        if one matches, open SLA instances for the chosen agreement's targets.
        No-op when no rules are configured or none match."""
        from app.models.sla import SLAAgreement, SLARule
        from app.services import slm_service, sla_runtime

        rules = (await db.execute(
            select(SLARule)
            .where(SLARule.tenant_id == ticket.tenant_id, SLARule.is_active.is_(True))
            .order_by(SLARule.position)
        )).scalars().all()
        if not rules:
            return

        ctx = {
            "type": ticket.type.value if hasattr(ticket.type, "value") else ticket.type,
            "priority": ticket.priority.value if hasattr(ticket.priority, "value") else ticket.priority,
            "category_id": str(ticket.category_id) if ticket.category_id else None,
            "product_id": str(ticket.product_id) if ticket.product_id else None,
        }
        agreement_id = slm_service.match_agreement_id(rules, ctx)
        if agreement_id is None:
            return
        ag = (await db.execute(
            select(SLAAgreement).where(
                SLAAgreement.id == agreement_id,
                SLAAgreement.tenant_id == ticket.tenant_id,
                SLAAgreement.deleted_at.is_(None),
            )
        )).scalar_one_or_none()
        if ag is not None:
            await sla_runtime.open_instances(db, ticket, ag)

    async def update_ticket(
        self,
        ticket: Ticket,
        updates: dict,
        actor_id: UUID,
        db: AsyncSession,
    ) -> Ticket:
        repo = TicketRepository(db)

        for field_name, new_value in updates.items():
            old_value = getattr(ticket, field_name, None)
            if old_value == new_value:
                continue

            if field_name == "status":
                new_status = TicketStatus(new_value)
                valid = await repo.get_valid_transitions(ticket.status, ticket.type)
                if new_status.value not in valid:
                    raise InvalidStateTransitionError(
                        from_status=ticket.status.value,
                        to_status=new_status.value,
                        valid_transitions=valid,
                    )

                if new_status == TicketStatus.pending and ticket.sla_paused_at is None:
                    ticket.sla_paused_at = func.now()  # type: ignore[assignment]

                elif ticket.status == TicketStatus.pending and new_status != TicketStatus.pending:
                    if ticket.sla_paused_at is not None:
                        elapsed_expr = func.extract(
                            "epoch", func.now() - ticket.sla_paused_at
                        )
                        ticket.sla_paused_duration_sec = (  # type: ignore[assignment]
                            func.coalesce(ticket.sla_paused_duration_sec, 0) + elapsed_expr
                        )
                        if ticket.sla_response_due is not None:
                            ticket.sla_response_due = ticket.sla_response_due + func.make_interval(  # type: ignore[assignment]
                                secs=elapsed_expr
                            )
                        if ticket.sla_resolve_due is not None:
                            ticket.sla_resolve_due = ticket.sla_resolve_due + func.make_interval(  # type: ignore[assignment]
                                secs=elapsed_expr
                            )
                        ticket.sla_paused_at = None  # type: ignore[assignment]

                if new_status == TicketStatus.resolved:
                    ticket.resolved_at = func.now()  # type: ignore[assignment]
                elif new_status == TicketStatus.closed:
                    ticket.closed_at = func.now()  # type: ignore[assignment]

                setattr(ticket, field_name, new_status)
            else:
                setattr(ticket, field_name, new_value)

            await repo.record_history(
                ticket_id=ticket.id,
                actor_id=actor_id,
                field=field_name,
                old_val=old_value.value if hasattr(old_value, "value") else old_value,
                new_val=new_value.value if hasattr(new_value, "value") else new_value,
            )

        db.add(ticket)
        await db.flush()
        await db.refresh(ticket)

        # Fire automation rules asynchronously — never let errors propagate
        try:
            from app.workers.tasks_automation import run_ticket_automation
            run_ticket_automation.delay(
                str(ticket.id),
                "ticket_updated",
                list(updates.keys()),
            )
        except Exception:
            pass

        # Dispatch outbound webhook event — never let errors propagate
        try:
            from app.workers.tasks_webhooks import dispatch_webhook_event
            dispatch_webhook_event.delay(
                str(ticket.tenant_id),
                "ticket.updated",
                {
                    "id": str(ticket.id),
                    "ticket_number": ticket.ticket_number,
                    "status": ticket.status.value,
                    "priority": ticket.priority.value,
                    "changed_fields": list(updates.keys()),
                },
            )
        except Exception:
            pass

        return ticket

    async def assign_ticket(
        self,
        ticket: Ticket,
        assignee_id: UUID | None,
        team_id: UUID | None,
        actor_id: UUID,
        db: AsyncSession,
    ) -> Ticket:
        repo = TicketRepository(db)

        if ticket.assignee_id != assignee_id:
            await repo.record_history(
                ticket_id=ticket.id,
                actor_id=actor_id,
                field="assignee_id",
                old_val=str(ticket.assignee_id) if ticket.assignee_id else None,
                new_val=str(assignee_id) if assignee_id else None,
            )
            ticket.assignee_id = assignee_id  # type: ignore[assignment]
            if assignee_id is not None:
                await repo.add_watcher(ticket_id=ticket.id, user_id=assignee_id)

        if ticket.team_id != team_id:
            await repo.record_history(
                ticket_id=ticket.id,
                actor_id=actor_id,
                field="team_id",
                old_val=str(ticket.team_id) if ticket.team_id else None,
                new_val=str(team_id) if team_id else None,
            )
            ticket.team_id = team_id  # type: ignore[assignment]

        db.add(ticket)
        await db.flush()
        await db.refresh(ticket)
        return ticket
