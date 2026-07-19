"""SLA breach detection task — runs every 5 minutes via Celery beat.

Rules:
- ALWAYS use DB server NOW() for time comparisons — never Python datetime.
- Batch-process in chunks of 100 to avoid long-running queries.
- On breach: set sla_breached = True, notify assignee and team lead.
- Log breach count at INFO level each run.
"""

import logging
from uuid import UUID

from sqlalchemy import and_, select, update
from sqlalchemy.orm import Session

from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(
    name="app.workers.tasks_sla.check_sla_breaches",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
)
def check_sla_breaches(self) -> None:
    """Find and flag tickets that have breached their SLA.

    Criteria (evaluated entirely in the DB using server NOW()):
      - sla_paused_at IS NULL            -- clock is running
      - sla_breached = FALSE
      - status NOT IN ('resolved', 'closed', 'cancelled')
      - (sla_response_due < NOW() OR sla_resolve_due < NOW())

    For each breached ticket:
      1. SET sla_breached = TRUE
      2. Enqueue breach notifications to assignee + team lead
    """
    import asyncio

    try:
        asyncio.run(_async_check_sla_breaches())
    except Exception as exc:
        logger.error(
            "sla_breach_check_failed",
            extra={"attempt": self.request.retries + 1, "error": str(exc)},
        )
        raise self.retry(exc=exc)


async def _async_check_sla_breaches() -> None:
    """Async implementation — uses separate DB session from Celery worker pool."""
    from sqlalchemy import func, text
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker

    from app.config import get_settings
    from app.models.ticket import Ticket, TicketStatus

    settings = get_settings()
    engine = create_async_engine(settings.DATABASE_URL, pool_size=5, max_overflow=2)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    BATCH_SIZE = 100
    terminal_statuses = [
        TicketStatus.resolved.value,
        TicketStatus.closed.value,
        TicketStatus.cancelled.value,
    ]

    total_breached = 0
    offset = 0

    try:
        async with async_session() as db:
            while True:
                # Find breachable tickets using DB server NOW() — never Python datetime
                result = await db.execute(
                    select(Ticket.id, Ticket.assignee_id, Ticket.team_id, Ticket.tenant_id,
                           Ticket.priority, Ticket.requester_id, Ticket.type)
                    .where(
                        and_(
                            Ticket.sla_paused_at.is_(None),
                            Ticket.sla_breached.is_(False),
                            Ticket.status.notin_(terminal_statuses),
                            (
                                (Ticket.sla_response_due < func.now())
                                | (Ticket.sla_resolve_due < func.now())
                            ),
                        )
                    )
                    .limit(BATCH_SIZE)
                    .offset(offset)
                )
                rows = result.all()

                if not rows:
                    break

                ticket_ids = [row.id for row in rows]

                # Mark all as breached in one UPDATE
                await db.execute(
                    update(Ticket)
                    .where(Ticket.id.in_(ticket_ids))
                    .values(sla_breached=True)
                )

                # Default escalation (no per-tenant rule needed): bump priority
                # one level toward critical and record a TicketEscalation. Tenant
                # automation rules on ticket_sla_breached still run on top.
                from app.models.ticket import TicketEscalation
                from app.services.sla_service import escalated_priority
                for row in rows:
                    new_pri = escalated_priority(row.priority)
                    if new_pri != row.priority:
                        await db.execute(
                            update(Ticket).where(Ticket.id == row.id).values(priority=new_pri)
                        )
                    db.add(TicketEscalation(
                        ticket_id=row.id,
                        escalated_by=row.requester_id,   # system action
                        escalated_to=row.assignee_id,
                        reason="SLA breach — auto-escalated",
                    ))

                    # RCA policy evaluation (specs/08 Phase 2) — best-effort,
                    # never blocks SLA-breach flagging.
                    try:
                        await _maybe_require_rca_for_ticket(db, row)
                    except Exception:
                        pass

                await db.commit()

                total_breached += len(ticket_ids)

                # Enqueue notifications and automation for each breached ticket
                for row in rows:
                    _enqueue_breach_notifications(
                        tenant_id=row.tenant_id,
                        ticket_id=row.id,
                        assignee_id=row.assignee_id,
                        team_id=row.team_id,
                    )
                    try:
                        from app.workers.tasks_automation import run_ticket_automation
                        run_ticket_automation.delay(str(row.id), "ticket_sla_breached")
                    except Exception:
                        pass

                if len(rows) < BATCH_SIZE:
                    break

                offset += BATCH_SIZE

    finally:
        await engine.dispose()

    if total_breached > 0:
        logger.info(
            "sla_breaches_detected",
            extra={"count": total_breached},
        )
    else:
        logger.debug("sla_breach_check_no_breaches")


async def _maybe_require_rca_for_ticket(db, row) -> None:
    """RCA policy evaluation (specs/08 Phase 2) for a just-breached ticket.
    Best-effort — exceptions are swallowed by the caller."""
    from app.models.retro import IncidentRetrospective
    from app.services import rca_policy_engine, rca_service
    from sqlalchemy import func, select as sa_select

    existing = (await db.execute(
        sa_select(IncidentRetrospective).where(
            IncidentRetrospective.source_ticket_id == row.id,
            IncidentRetrospective.is_rca_governed.is_(True),
        )
    )).scalar_one_or_none()
    if existing is not None:
        return

    priority_str = row.priority.value if hasattr(row.priority, "value") else str(row.priority)
    ticket_type_str = row.type.value if hasattr(row.type, "value") else str(row.type)
    ctx = rca_policy_engine.PolicyContext(
        ticket_type=ticket_type_str, priority=priority_str, sla_breached=True, team_id=row.team_id,
    )
    decision = await rca_policy_engine.evaluate(db, row.tenant_id, ctx)
    if decision is None or not decision.required:
        return

    due_at = func.now() + func.make_interval(0, 0, 0, decision.due_days)
    await rca_service.create_rca(
        db, row.tenant_id, None, source_type="ticket", source_ticket_id=row.id,
        title=f"RCA for SLA-breached ticket (priority: {priority_str})",
        due_at=due_at, trigger_policy_id=decision.policy_id,
    )


@celery_app.task(
    name="app.workers.tasks_sla.scan_sla_instances",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
)
def scan_sla_instances(self) -> None:
    """Warn + breach detection over ``sla_instances`` (Phase 7 / S7.2).

    Runs entirely on DB server NOW(). Skips paused instances. Fires warn events
    at each configured threshold %, marks breaches, and attributes them via the
    OLA/UC underpinning chain. Additive to ``check_sla_breaches`` (which keeps
    the ticket ``sla_*`` cache for tickets without instances yet).
    """
    import asyncio

    try:
        asyncio.run(_async_scan_sla_instances())
    except Exception as exc:
        logger.error("sla_instance_scan_failed",
                     extra={"attempt": self.request.retries + 1, "error": str(exc)})
        raise self.retry(exc=exc)


async def _async_scan_sla_instances() -> None:
    from sqlalchemy import func, text
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker

    from app.config import get_settings
    from app.models.sla import SLAEvent, SLAInstance, SLATarget
    from app.services import sla_runtime

    settings = get_settings()
    engine = create_async_engine(settings.DATABASE_URL, pool_size=5, max_overflow=2)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    warned = breached = 0
    try:
        async with async_session() as db:
            # Platform-worker connection sees all tenants (RLS fail-open when the
            # GUC is unset), which is what we want for a global scan.
            # --- Warnings: crossed threshold % not yet in warned_pct ---
            running = (await db.execute(
                select(
                    SLAInstance, SLATarget.warn_thresholds_pct,
                    func.extract("epoch", func.now() - SLAInstance.created_at).label("elapsed"),
                    func.extract("epoch", SLAInstance.due_at - SLAInstance.created_at).label("total"),
                )
                .join(SLATarget, SLATarget.id == SLAInstance.target_id)
                .where(SLAInstance.status == "running", SLAInstance.paused_at.is_(None))
            )).all()
            for inst, thresholds, elapsed, total in running:
                if not total or total <= 0:
                    continue
                pct = float(elapsed) / float(total) * 100.0
                already = set(inst.warned_pct or [])
                to_fire = sorted(t for t in (thresholds or []) if pct >= t and t not in already)
                for t in to_fire:
                    db.add(SLAEvent(instance_id=inst.id, event="warned", reason=f"{t}% consumed"))
                    inst.warned_pct = list(already | {t})
                    warned += 1

            # --- Breaches: running + not paused + due_at < now() ---
            due = (await db.execute(
                select(SLAInstance).where(
                    SLAInstance.status == "running",
                    SLAInstance.paused_at.is_(None),
                    SLAInstance.due_at < func.now(),
                )
            )).scalars().all()
            for inst in due:
                inst.status = "breached"
                inst.breached_at = func.now()
                db.add(SLAEvent(instance_id=inst.id, event="breached"))
                breached += 1
            await db.flush()

            # --- Attribution pass (after all breaches marked) ---
            for inst in due:
                await sla_runtime.attribute_breach(db, inst)

            # --- HITL ground truth: mark predictions for breached instances ---
            if due:
                from app.models.sla import AISLAPrediction
                from sqlalchemy import update as _update
                await db.execute(
                    _update(AISLAPrediction)
                    .where(AISLAPrediction.instance_id.in_([i.id for i in due]))
                    .values(actual_breached=True)
                )

            await db.commit()
    finally:
        await engine.dispose()

    if warned or breached:
        logger.info("sla_instance_scan", extra={"warned": warned, "breached": breached})
    else:
        logger.debug("sla_instance_scan_clean")


@celery_app.task(
    name="app.workers.tasks_sla.flush_sla_metrics_daily",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
)
def flush_sla_metrics_daily(self) -> None:
    """Nightly per-tenant SLA rollup into sla_metrics_daily (Phase 7 / S7.3).

    Recomputes yesterday AND today (today is partial but keeps the live dashboard
    fresh; the idempotent upsert makes re-runs safe)."""
    import asyncio

    try:
        asyncio.run(_async_flush_sla_metrics_daily())
    except Exception as exc:
        logger.error("sla_metrics_flush_failed",
                     extra={"attempt": self.request.retries + 1, "error": str(exc)})
        raise self.retry(exc=exc)


async def _async_flush_sla_metrics_daily() -> None:
    from datetime import date, timedelta

    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker

    from app.config import get_settings
    from app.services.sla_metrics import compute_sla_metrics_for_date

    settings = get_settings()
    engine = create_async_engine(settings.DATABASE_URL, pool_size=2, max_overflow=1)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    today = date.today()
    try:
        async with async_session() as db:
            for d in (today - timedelta(days=1), today):
                await compute_sla_metrics_for_date(db, d)
            await db.commit()
    finally:
        await engine.dispose()
    logger.info("sla_metrics_flushed")


@celery_app.task(
    name="app.workers.tasks_sla.predict_sla_breaches",
    bind=True, max_retries=3, default_retry_delay=30,
)
def predict_sla_breaches(self) -> None:
    """Score open SLA instances for breach risk (Phase 7 / S7.3 tail)."""
    import asyncio
    try:
        asyncio.run(_async_predict_sla_breaches())
    except Exception as exc:
        logger.error("sla_predict_failed",
                     extra={"attempt": self.request.retries + 1, "error": str(exc)})
        raise self.retry(exc=exc)


async def _async_predict_sla_breaches() -> None:
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker

    from app.config import get_settings
    from app.services.sla_prediction import score_open_instances

    settings = get_settings()
    engine = create_async_engine(settings.DATABASE_URL, pool_size=2, max_overflow=1)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with async_session() as db:
            n = await score_open_instances(db)
            await db.commit()
        logger.info("sla_predictions_scored", extra={"count": n})
    finally:
        await engine.dispose()


def _enqueue_breach_notifications(
    *,
    tenant_id: UUID,
    ticket_id: UUID,
    assignee_id: UUID | None,
    team_id: UUID | None,
) -> None:
    """Dispatch breach notification tasks without blocking the checker loop."""
    import asyncio

    try:
        asyncio.run(_async_send_breach_notifications(
            tenant_id=tenant_id,
            ticket_id=ticket_id,
            assignee_id=assignee_id,
            team_id=team_id,
        ))
    except Exception as exc:
        logger.warning(
            "breach_notification_enqueue_failed",
            extra={"ticket_id": str(ticket_id), "error": str(exc)},
        )


async def _async_send_breach_notifications(
    *,
    tenant_id: UUID,
    ticket_id: UUID,
    assignee_id: UUID | None,
    team_id: UUID | None,
) -> None:
    """Send SLA breach notifications to assignee and team lead."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker

    from app.config import get_settings
    from app.models.identity import Team
    from app.models.notification import NotificationType
    from app.models.ticket import Ticket
    from app.services.notification_service import NotificationService

    settings = get_settings()
    engine = create_async_engine(settings.DATABASE_URL, pool_size=2, max_overflow=1)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    notif_service = NotificationService()

    try:
        async with async_session() as db:
            # Fetch ticket number for the notification title
            t_result = await db.execute(
                select(Ticket.ticket_number).where(Ticket.id == ticket_id)
            )
            ticket_number = t_result.scalar_one_or_none() or str(ticket_id)

            title = f"SLA breached on ticket {ticket_number}"
            body = "This ticket has exceeded its SLA response or resolution deadline."

            recipients: list[UUID] = []

            if assignee_id is not None:
                recipients.append(assignee_id)

            if team_id is not None:
                team_result = await db.execute(
                    select(Team.lead_id).where(Team.id == team_id)
                )
                lead_id = team_result.scalar_one_or_none()
                if lead_id is not None and lead_id not in recipients:
                    recipients.append(lead_id)

            if recipients:
                await notif_service.send_bulk(
                    db=db,
                    tenant_id=tenant_id,
                    user_ids=recipients,
                    type=NotificationType.ticket_sla_breached,
                    title=title,
                    body=body,
                    entity_type="ticket",
                    entity_id=ticket_id,
                )
                await db.commit()
    finally:
        await engine.dispose()
