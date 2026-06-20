"""Celery tasks for outbound webhook delivery (S6B).

Tasks
-----
deliver_webhook(delivery_id)
    Execute the HTTP POST for a single WebhookDelivery row.
    max_retries=0 — retry logic lives inside WebhookService.deliver().

dispatch_webhook_event(tenant_id, event_type, payload)
    Async wrapper called from service layer after DB commit.
    Fan-outs to all matching endpoints via WebhookService.dispatch_event().

retry_failed_webhooks()
    Beat task (every 5 min): find retrying deliveries with
    next_retry_at <= NOW() and re-enqueue them.
"""

import asyncio
import logging
from typing import Any

from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# deliver_webhook — actual HTTP call
# ---------------------------------------------------------------------------


@celery_app.task(
    name="app.workers.tasks_webhooks.deliver_webhook",
    bind=True,
    queue="default",
    max_retries=0,
)
def deliver_webhook(self, delivery_id: str) -> None:
    """Execute the outbound HTTP POST for a single WebhookDelivery.

    Retry logic is handled inside WebhookService.deliver() — this task
    never retries itself (max_retries=0).  All exceptions are caught and
    logged so that Celery does not mark the task as failed externally.
    """
    try:
        asyncio.run(_deliver_async(delivery_id))
    except Exception as exc:
        logger.error(
            "deliver_webhook_task_error",
            extra={"delivery_id": delivery_id, "error": str(exc)},
        )


async def _deliver_async(delivery_id: str) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker

    from app.config import get_settings
    from app.services.webhook_service import webhook_service

    settings = get_settings()
    engine = create_async_engine(settings.DATABASE_URL, pool_size=5, max_overflow=2)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    try:
        async with async_session() as db:
            async with db.begin():
                await webhook_service.deliver(db, delivery_id)
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# dispatch_webhook_event — fan-out from service layer
# ---------------------------------------------------------------------------


@celery_app.task(
    name="app.workers.tasks_webhooks.dispatch_webhook_event",
    queue="default",
)
def dispatch_webhook_event(tenant_id: str, event_type: str, payload: dict) -> None:
    """Fan out a domain event to all matching active webhook endpoints.

    Called via .delay() from service layer after the triggering DB commit.
    """
    try:
        asyncio.run(_dispatch_async(tenant_id, event_type, payload))
    except Exception as exc:
        logger.error(
            "dispatch_webhook_event_task_error",
            extra={"tenant_id": tenant_id, "event_type": event_type, "error": str(exc)},
        )


async def _dispatch_async(tenant_id: str, event_type: str, payload: dict) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker

    from app.config import get_settings
    from app.services.webhook_service import webhook_service

    settings = get_settings()
    engine = create_async_engine(settings.DATABASE_URL, pool_size=5, max_overflow=2)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    try:
        async with async_session() as db:
            async with db.begin():
                await webhook_service.dispatch_event(db, tenant_id, event_type, payload)
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# retry_failed_webhooks — beat task every 5 minutes
# ---------------------------------------------------------------------------


@celery_app.task(
    name="app.workers.tasks_webhooks.retry_failed_webhooks",
    queue="default",
)
def retry_failed_webhooks() -> None:
    """Re-enqueue retrying deliveries whose next_retry_at is in the past.

    Runs every 5 minutes via Celery beat.  Each matched delivery gets a
    fresh deliver_webhook task enqueued; the delivery's next_retry_at is
    cleared so it is not re-picked on the next beat cycle unless the
    worker updates it again.
    """
    try:
        asyncio.run(_retry_async())
    except Exception as exc:
        logger.error(
            "retry_failed_webhooks_task_error",
            extra={"error": str(exc)},
        )


async def _retry_async() -> None:
    from sqlalchemy import and_, func, select, update
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker

    from app.config import get_settings
    from app.models.webhook import WebhookDelivery

    settings = get_settings()
    engine = create_async_engine(settings.DATABASE_URL, pool_size=5, max_overflow=2)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    try:
        async with async_session() as db:
            result = await db.execute(
                select(WebhookDelivery.id).where(
                    and_(
                        WebhookDelivery.status == "retrying",
                        WebhookDelivery.next_retry_at <= func.now(),
                    )
                )
            )
            delivery_ids = [str(row[0]) for row in result.all()]

            if not delivery_ids:
                logger.debug("retry_failed_webhooks_nothing_to_retry")
                return

            # Clear next_retry_at so they won't be picked up again until
            # deliver_webhook sets a new value (or marks them failed).
            await db.execute(
                update(WebhookDelivery)
                .where(WebhookDelivery.id.in_([__import__('uuid').UUID(d) for d in delivery_ids]))
                .values(next_retry_at=None)
            )
            await db.commit()

        logger.info(
            "retry_failed_webhooks_enqueued",
            extra={"count": len(delivery_ids)},
        )

        for did in delivery_ids:
            deliver_webhook.delay(did)

    finally:
        await engine.dispose()
