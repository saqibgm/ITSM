"""Support Session Recording Celery tasks (specs/08, Phase 1+2).

AI summary generation always runs here — never inline in a request handler
(NFR §10.1). recording_link_health_check is a simple URL-liveness probe for
Phase 1/2; real Graph-permission-based reachability checking is Phase 3.
"""

import logging

from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="app.workers.tasks_recordings.generate_summary_task",
                 bind=True, max_retries=2, default_retry_delay=30)
def generate_summary_task(self, recording_id: str) -> None:
    import asyncio
    try:
        asyncio.run(_async_generate_summary(recording_id))
    except Exception as exc:
        logger.error("recording_summary_task_failed", extra={"recording_id": recording_id, "error": str(exc)})


async def _async_generate_summary(recording_id: str) -> None:
    from uuid import UUID

    from sqlalchemy import select

    from app.database import AsyncSessionLocal
    from app.models.recording import SupportRecording
    from app.redis_client import get_worker_redis_client
    from app.services import recording_service

    async with AsyncSessionLocal() as db:
        rec = (await db.execute(select(SupportRecording).where(SupportRecording.id == UUID(recording_id)))).scalar_one_or_none()
        if rec is None:
            return
        await recording_service.generate_recording_summary(db, rec.tenant_id, rec, get_worker_redis_client())
        await db.commit()


@celery_app.task(name="app.workers.tasks_recordings.recording_link_health_check",
                 bind=True, max_retries=1)
def recording_link_health_check(self) -> None:
    import asyncio
    try:
        asyncio.run(_async_health_check())
    except Exception as exc:
        logger.error("recording_link_health_check_failed", extra={"error": str(exc)})


async def _async_health_check() -> None:
    import httpx
    from sqlalchemy import select

    from app.database import AsyncSessionLocal
    from app.models.notification import NotificationType
    from app.models.recording import SupportRecording
    from app.models.ticket import Ticket
    from app.models.recording import TicketRecordingLink
    from app.services.notification_service import NotificationService

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(SupportRecording).where(
                SupportRecording.storage_mode == "external_reference",
                SupportRecording.status.notin_(["inaccessible", "deleted", "archived"]),
            ).limit(200)
        )).scalars().all()

        checked = inaccessible = 0
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as client:
            for rec in rows:
                checked += 1
                try:
                    resp = await client.head(rec.recording_url)
                    ok = resp.status_code < 400
                except Exception:
                    ok = False
                if not ok:
                    rec.status = "inaccessible"
                    inaccessible += 1
                    link = (await db.execute(
                        select(TicketRecordingLink).where(TicketRecordingLink.recording_id == rec.id)
                    )).scalars().first()
                    if link is not None:
                        ticket = (await db.execute(select(Ticket).where(Ticket.id == link.ticket_id))).scalar_one_or_none()
                        recipients = [uid for uid in (rec.created_by, ticket.assignee_id if ticket else None) if uid]
                        if recipients:
                            await NotificationService().send_bulk(
                                db, tenant_id=rec.tenant_id, user_ids=recipients,
                                type=NotificationType.recording_inaccessible,
                                title=f"Recording '{rec.title}' is no longer accessible",
                                entity_type="support_recording", entity_id=rec.id,
                            )
        await db.commit()
        if checked:
            logger.info("recording_link_health_check_done", extra={"checked": checked, "inaccessible": inaccessible})
