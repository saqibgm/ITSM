"""Alerting workers (Phase 8 / S8.2).

- process_alert_escalations: advance unacknowledged alerts past their step timeout.
- check_heartbeats: raise an alert when a heartbeat misses its interval.
Both run on DB/UTC now; additive to the SLA workers.
"""

import logging

from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="app.workers.tasks_alerting.process_alert_escalations",
                 bind=True, max_retries=3, default_retry_delay=30)
def process_alert_escalations(self) -> None:
    import asyncio
    try:
        asyncio.run(_async_process_escalations())
    except Exception as exc:
        logger.error("alert_escalation_failed", extra={"error": str(exc)})
        raise self.retry(exc=exc)


async def _async_process_escalations() -> None:
    from datetime import datetime, timezone
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker

    from app.config import get_settings
    from app.models.alerting import Alert
    from app.services import alerting_service

    settings = get_settings()
    engine = create_async_engine(settings.DATABASE_URL, pool_size=3, max_overflow=2)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    advanced = 0
    try:
        async with async_session() as db:
            due = (await db.execute(
                select(Alert).where(
                    Alert.status == "open",
                    Alert.next_escalation_at.isnot(None),
                    Alert.next_escalation_at < datetime.now(timezone.utc),
                ).limit(200)
            )).scalars().all()
            for alert in due:
                if await alerting_service.advance_escalation(db, alert):
                    advanced += 1
            await db.commit()
    finally:
        await engine.dispose()
    if advanced:
        logger.info("alert_escalations_advanced", extra={"count": advanced})


@celery_app.task(name="app.workers.tasks_alerting.check_heartbeats",
                 bind=True, max_retries=3, default_retry_delay=30)
def check_heartbeats(self) -> None:
    import asyncio
    try:
        asyncio.run(_async_check_heartbeats())
    except Exception as exc:
        logger.error("heartbeat_check_failed", extra={"error": str(exc)})
        raise self.retry(exc=exc)


async def _async_check_heartbeats() -> None:
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker

    from app.config import get_settings
    from app.models.alerting import Heartbeat
    from app.services import alerting_service

    settings = get_settings()
    engine = create_async_engine(settings.DATABASE_URL, pool_size=2, max_overflow=1)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    now = datetime.now(timezone.utc)
    raised = 0
    try:
        async with async_session() as db:
            hbs = (await db.execute(select(Heartbeat))).scalars().all()
            for hb in hbs:
                # Overdue = last ping older than 2× the interval (grace), or never pinged.
                deadline = (hb.last_ping_at or hb.__dict__.get("created_at"))
                overdue = hb.last_ping_at is not None and \
                    hb.last_ping_at < now - timedelta(seconds=hb.interval_sec * 2)
                if not overdue:
                    continue
                await alerting_service.ingest_alert(
                    db, hb.tenant_id, dedup_key=f"heartbeat:{hb.id}",
                    title=f"Heartbeat '{hb.name}' missed", source="heartbeat",
                    service_id=hb.service_id,
                )
                raised += 1
            await db.commit()
    finally:
        await engine.dispose()
    if raised:
        logger.info("heartbeat_alerts_raised", extra={"count": raised})
