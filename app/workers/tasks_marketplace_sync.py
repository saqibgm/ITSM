"""Celery tasks for native marketplace-integration event processing (V3-Marketplaces).

Mirrors tasks_webhooks.py's shape (inline async session per task, try/except
logging, no self-retry — retry semantics belong in the service layer once
written) but for the INBOUND direction: a MarketplaceEvent row already exists
(written 'received' by the webhook route before this task is enqueued, per
docs/plans/NATIVE_MARKETPLACE_CONNECTORS_PLAN.md §1) and this task advances it
to a terminal status.

process_marketplace_event(event_id)
    Load the MarketplaceEvent, dispatch to map_order/map_return_to_ticket/
    map_message_to_comment (app/services/marketplaces/ingestion.py) based on
    event_type, update status to the terminal outcome.

Not yet wired to a live connector (Phase 1 scaffold) — no connector currently
produces a MarketplaceEvent row for this task to pick up. Lands with Phase 2's
first connector (Amazon or Shopify per the pilot-batch roadmap).
"""

import asyncio
import logging

from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(
    name="app.workers.tasks_marketplace_sync.process_marketplace_event",
    queue="default",
)
def process_marketplace_event(event_id: str) -> None:
    """Process one inbound MarketplaceEvent row end to end.

    All exceptions are caught and logged, same as tasks_webhooks.py's tasks —
    the event row's own status field carries the failure, Celery is not relied
    on externally to surface it.
    """
    try:
        asyncio.run(_process_async(event_id))
    except Exception as exc:
        logger.error(
            "process_marketplace_event_task_error",
            extra={"event_id": event_id, "error": str(exc)},
        )


async def _process_async(event_id: str) -> None:
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker

    from app.config import get_settings
    from app.models.marketplace import MarketplaceConnection, MarketplaceEvent
    from app.services.marketplaces import ingestion

    settings = get_settings()
    engine = create_async_engine(settings.DATABASE_URL, pool_size=5, max_overflow=2)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    try:
        async with async_session() as db:
            async with db.begin():
                event = (
                    await db.execute(
                        select(MarketplaceEvent).where(MarketplaceEvent.id == event_id)
                    )
                ).scalar_one_or_none()
                if event is None:
                    logger.warning("process_marketplace_event_not_found", extra={"event_id": event_id})
                    return

                connection = (
                    await db.execute(
                        select(MarketplaceConnection).where(
                            MarketplaceConnection.id == event.connection_id
                        )
                    )
                ).scalar_one_or_none()
                if connection is None:
                    event.status = "failed"
                    event.error_message = "connection no longer exists"
                    return

                try:
                    # TODO(Phase 2): event.event_type routing to
                    # ingestion.map_order / map_return_to_ticket /
                    # map_message_to_comment, with each connector's
                    # parse_webhook() output already normalized by the time
                    # it reached the MarketplaceEvent.payload column. Left
                    # unwired until a real connector's event_type vocabulary
                    # exists to route against (no connector implemented yet —
                    # this task has nothing to actually pick up until Phase 2).
                    event.status = "processed"
                    event.processed_at = __import__("datetime").datetime.now(
                        __import__("datetime").timezone.utc
                    )
                except Exception as exc:  # noqa: BLE001 — recorded on the event row, not swallowed
                    event.status = "failed"
                    event.error_message = str(exc)
                    logger.error(
                        "process_marketplace_event_mapping_error",
                        extra={"event_id": event_id, "provider": connection.provider, "error": str(exc)},
                    )
    finally:
        await engine.dispose()


__all__ = ["process_marketplace_event"]
