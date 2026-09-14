"""Celery tasks for native marketplace-integration event processing (V3-Marketplaces).

Mirrors tasks_webhooks.py's shape (inline async session per task, try/except
logging, no self-retry — retry semantics belong in the service layer once
written) but for the INBOUND direction: a MarketplaceEvent row already exists
(written 'received' by the webhook route before this task is enqueued, per
docs/plans/NATIVE_MARKETPLACE_CONNECTORS_PLAN.md §1) and this task advances it
to a terminal status.

process_marketplace_event(event_id)
    Load the MarketplaceEvent, resolve its connector via the registry, call
    connector.normalize_event(event_type, payload) to get a
    NormalizedOrder/NormalizedReturn/NormalizedMessage, dispatch to
    ingestion.map_order/map_return_to_ticket/map_message_to_comment based on
    which one came back, update status to the terminal outcome.

Wired 2026-09-14 — previously a TODO stub (see git history). Still only
reachable for connectors whose normalize_event() does real topic dispatch,
which today is Shopify alone; the others' normalize_event returns None
(no webhook route wired for them yet, see each connector's module
docstring), so this task has nothing to actually pick up for them until
that changes — same limitation, now explicit in the routing logic below
rather than in an unwired TODO.
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
    from datetime import datetime, timezone

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker

    from app.config import get_settings
    from app.models.marketplace import MarketplaceConnection, MarketplaceEvent
    from app.redis_client import get_worker_redis_client
    from app.services.marketplaces import ingestion
    from app.services.marketplaces.connectors.base import NormalizedMessage, NormalizedOrder, NormalizedReturn
    from app.services.marketplaces.registry import get_connector
    from app.services.marketplaces.system_user import get_or_create_marketplace_bot_user

    settings = get_settings()
    engine = create_async_engine(settings.DATABASE_URL, pool_size=5, max_overflow=2)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    redis_client = get_worker_redis_client()

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
                    connector = get_connector(event.provider)
                    normalized = connector.normalize_event(event.event_type, event.payload)

                    if normalized is None:
                        # Not an error — either an intentionally-ignored topic
                        # (e.g. Shopify's GDPR compliance topics, handled at
                        # the route layer) or a connector whose normalize_event
                        # doesn't do real dispatch yet (see module docstring).
                        event.status = "ignored"

                    elif isinstance(normalized, NormalizedOrder):
                        order = await ingestion.map_order(db, connection, event.tenant_id, normalized)
                        event.resulting_order_id = order.id
                        event.status = "order_updated"

                    elif isinstance(normalized, NormalizedReturn):
                        bot_user_id = await get_or_create_marketplace_bot_user(db, event.tenant_id)
                        link = await ingestion.map_return_to_ticket(
                            db, connection, event.tenant_id, bot_user_id, redis_client, normalized
                        )
                        if link is not None:
                            event.resulting_ticket_id = link.ticket_id
                            event.resulting_order_id = link.order_id
                            event.status = "ticket_created"
                        else:
                            # map_return_to_ticket() already logs its own
                            # reason (e.g. no matching order synced yet) —
                            # not necessarily a hard failure, but nothing
                            # resulted either.
                            event.status = "failed"
                            event.error_message = "no matching order found for this return/replacement event"

                    elif isinstance(normalized, NormalizedMessage):
                        bot_user_id = await get_or_create_marketplace_bot_user(db, event.tenant_id)
                        comment = await ingestion.map_message_to_comment(db, event.tenant_id, bot_user_id, normalized)
                        if comment is not None:
                            event.resulting_ticket_id = comment.ticket_id
                            event.status = "comment_added"
                        else:
                            event.status = "failed"
                            event.error_message = "no ticket linked to this message's order/case"

                    event.processed_at = datetime.now(timezone.utc)

                except Exception as exc:  # noqa: BLE001 — recorded on the event row, not swallowed
                    event.status = "failed"
                    event.error_message = str(exc)
                    logger.error(
                        "process_marketplace_event_mapping_error",
                        extra={"event_id": event_id, "provider": connection.provider, "error": str(exc)},
                    )
    finally:
        await engine.dispose()
        await redis_client.aclose()


__all__ = ["process_marketplace_event"]
