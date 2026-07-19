"""RCA Governance Celery tasks (specs/08, Phase 2) — overdue scans + policy
safety net. All run on DB server NOW(), matching tasks_sla.py's convention."""

import logging

from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="app.workers.tasks_rca.rca_overdue_scan", bind=True, max_retries=2, default_retry_delay=30)
def rca_overdue_scan(self) -> None:
    import asyncio
    try:
        asyncio.run(_async_rca_overdue_scan())
    except Exception as exc:
        logger.error("rca_overdue_scan_failed", extra={"error": str(exc)})


async def _async_rca_overdue_scan() -> None:
    from sqlalchemy import func, select, update

    from app.database import AsyncSessionLocal
    from app.models.notification import NotificationType
    from app.models.retro import IncidentRetrospective
    from app.services.notification_service import NotificationService

    async with AsyncSessionLocal() as db:
        newly_overdue = (await db.execute(
            select(IncidentRetrospective).where(
                IncidentRetrospective.is_rca_governed.is_(True),
                IncidentRetrospective.due_at.isnot(None),
                IncidentRetrospective.due_at < func.now(),
                IncidentRetrospective.status.notin_(["overdue", "completed", "waived", "rejected"]),
            ).limit(200)
        )).scalars().all()

        for retro in newly_overdue:
            retro.previous_status = retro.status
            retro.status = "overdue"
            # team_id points at a team, not a user — only notify actual users here.
            recipients = [retro.owner_id] if retro.owner_id else []
            if recipients:
                await NotificationService().send_bulk(
                    db, tenant_id=retro.tenant_id, user_ids=recipients, type=NotificationType.rca_overdue,
                    title=f"RCA {retro.rca_number or retro.id} is overdue",
                    entity_type="rca_case", entity_id=retro.id,
                )
        await db.commit()
        if newly_overdue:
            logger.info("rca_overdue_scan_done", extra={"count": len(newly_overdue)})


@celery_app.task(name="app.workers.tasks_rca.rca_due_soon_scan", bind=True, max_retries=2, default_retry_delay=30)
def rca_due_soon_scan(self) -> None:
    import asyncio
    try:
        asyncio.run(_async_rca_due_soon_scan())
    except Exception as exc:
        logger.error("rca_due_soon_scan_failed", extra={"error": str(exc)})


async def _async_rca_due_soon_scan() -> None:
    from datetime import timedelta

    from sqlalchemy import func, select

    from app.database import AsyncSessionLocal
    from app.models.notification import NotificationType
    from app.models.retro import IncidentRetrospective
    from app.redis_client import redis_client
    from app.services.notification_service import NotificationService

    async with AsyncSessionLocal() as db:
        due_soon = (await db.execute(
            select(IncidentRetrospective).where(
                IncidentRetrospective.is_rca_governed.is_(True),
                IncidentRetrospective.due_at.isnot(None),
                IncidentRetrospective.due_at > func.now(),
                IncidentRetrospective.due_at < func.now() + func.make_interval(0, 0, 0, 0, 48),
                IncidentRetrospective.status.notin_(["overdue", "completed", "waived", "rejected"]),
            ).limit(200)
        )).scalars().all()

        notified = 0
        for retro in due_soon:
            dedupe_key = f"rca_due_soon_sent:{retro.id}:48h"
            if await redis_client.get(dedupe_key):
                continue
            if retro.owner_id:
                await NotificationService().send(
                    db, tenant_id=retro.tenant_id, user_id=retro.owner_id, type=NotificationType.rca_due_soon,
                    title=f"RCA {retro.rca_number or retro.id} due soon",
                    entity_type="rca_case", entity_id=retro.id,
                )
                notified += 1
            await redis_client.set(dedupe_key, "1", ex=48 * 3600)
        await db.commit()
        if notified:
            logger.info("rca_due_soon_scan_done", extra={"count": notified})


@celery_app.task(name="app.workers.tasks_rca.rca_action_overdue_scan", bind=True, max_retries=2, default_retry_delay=30)
def rca_action_overdue_scan(self) -> None:
    import asyncio
    try:
        asyncio.run(_async_rca_action_overdue_scan())
    except Exception as exc:
        logger.error("rca_action_overdue_scan_failed", extra={"error": str(exc)})


async def _async_rca_action_overdue_scan() -> None:
    from sqlalchemy import func, select

    from app.database import AsyncSessionLocal
    from app.models.notification import NotificationType
    from app.models.retro import RetroActionItem
    from app.services.notification_service import NotificationService

    async with AsyncSessionLocal() as db:
        newly_overdue = (await db.execute(
            select(RetroActionItem).where(
                RetroActionItem.due_at.isnot(None),
                RetroActionItem.due_at < func.now(),
                RetroActionItem.status.notin_(["done", "cancelled", "accepted_risk", "overdue"]),
            ).limit(200)
        )).scalars().all()

        for item in newly_overdue:
            item.status = "overdue"
            if item.owner_id:
                await NotificationService().send(
                    db, tenant_id=item.tenant_id, user_id=item.owner_id, type=NotificationType.rca_action_overdue,
                    title=f"RCA action item overdue: {item.title or item.description[:60]}",
                    entity_type="rca_case", entity_id=item.retro_id,
                )
        await db.commit()
        if newly_overdue:
            logger.info("rca_action_overdue_scan_done", extra={"count": len(newly_overdue)})


@celery_app.task(name="app.workers.tasks_rca.rca_policy_safety_net_scan", bind=True, max_retries=2, default_retry_delay=30)
def rca_policy_safety_net_scan(self) -> None:
    """Catches incidents/tickets a missed event hook (or a policy created
    after the fact) never evaluated. Idempotent — create_rca is skipped
    whenever a governed RCA already links the source (specs/08 Phase 2)."""
    import asyncio
    try:
        asyncio.run(_async_policy_safety_net())
    except Exception as exc:
        logger.error("rca_policy_safety_net_failed", extra={"error": str(exc)})


async def _async_policy_safety_net() -> None:
    from datetime import timedelta, datetime, timezone

    from sqlalchemy import select

    from app.database import AsyncSessionLocal
    from app.models.incident import Incident
    from app.models.oncall import SeverityLevel
    from app.models.retro import IncidentRetrospective
    from app.services import rca_policy_engine, rca_service

    since = datetime.now(timezone.utc) - timedelta(hours=1)

    async with AsyncSessionLocal() as db:
        recently_resolved = (await db.execute(
            select(Incident).where(Incident.status == "resolved", Incident.resolved_at >= since).limit(200)
        )).scalars().all()

        created = 0
        for incident in recently_resolved:
            existing = (await db.execute(
                select(IncidentRetrospective).where(
                    IncidentRetrospective.incident_id == incident.id,
                    IncidentRetrospective.is_rca_governed.is_(True),
                )
            )).scalar_one_or_none()
            if existing is not None:
                continue

            severity_rank = None
            if incident.severity_id is not None:
                sev = (await db.execute(select(SeverityLevel).where(SeverityLevel.id == incident.severity_id))).scalar_one_or_none()
                severity_rank = sev.rank if sev is not None else None

            ctx = rca_policy_engine.PolicyContext(ticket_type="incident", severity_rank=severity_rank)
            decision = await rca_policy_engine.evaluate(db, incident.tenant_id, ctx)
            if decision is None or not decision.required:
                continue

            from sqlalchemy import func
            due_at = func.now() + func.make_interval(0, 0, 0, decision.due_days)
            await rca_service.create_rca(
                db, incident.tenant_id, None, source_type="incident", incident_id=incident.id,
                title=f"RCA for {incident.incident_number}: {incident.title}",
                severity=f"sev{severity_rank}" if severity_rank else None,
                due_at=due_at, trigger_policy_id=decision.policy_id,
            )
            created += 1

        await db.commit()
        if created:
            logger.info("rca_policy_safety_net_created", extra={"count": created})
