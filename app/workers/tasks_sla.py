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
                    select(Ticket.id, Ticket.assignee_id, Ticket.team_id, Ticket.tenant_id)
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
