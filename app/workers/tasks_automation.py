"""Celery tasks for the Automation Rules Engine (S6A).

Tasks are fire-and-forget (max_retries=0).  All errors are caught and logged
— they never propagate back to the calling service.
"""

import logging

from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(
    name="app.workers.tasks_automation.run_ticket_automation",
    queue="default",
    max_retries=0,
)
def run_ticket_automation(
    ticket_id: str,
    trigger: str,
    changed_fields: list[str] | None = None,
) -> None:
    """Evaluate automation rules for a single ticket event.

    Args:
        ticket_id:      UUID string of the affected ticket.
        trigger:        One of the AutomationRule trigger constants.
        changed_fields: List of field names that changed (for ticket_updated).
    """
    import asyncio

    try:
        asyncio.run(_async_run_ticket_automation(ticket_id, trigger, changed_fields))
    except Exception as exc:
        logger.error(
            "tasks_automation_ticket_failed",
            extra={"ticket_id": ticket_id, "trigger": trigger, "error": str(exc)},
        )
        # Never re-raise — automation must not affect the calling operation


@celery_app.task(
    name="app.workers.tasks_automation.run_asset_automation",
    queue="default",
    max_retries=0,
)
def run_asset_automation(asset_id: str, trigger: str) -> None:
    """Evaluate automation rules for a single asset event.

    Args:
        asset_id: UUID string of the affected asset.
        trigger:  One of the AutomationRule trigger constants.
    """
    import asyncio

    try:
        asyncio.run(_async_run_asset_automation(asset_id, trigger))
    except Exception as exc:
        logger.error(
            "tasks_automation_asset_failed",
            extra={"asset_id": asset_id, "trigger": trigger, "error": str(exc)},
        )


# ---------------------------------------------------------------------------
# Async implementations
# ---------------------------------------------------------------------------


async def _async_run_ticket_automation(
    ticket_id: str,
    trigger: str,
    changed_fields: list[str] | None,
) -> None:
    from uuid import UUID

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker

    from app.config import get_settings
    from app.models.ticket import Ticket
    from app.services.automation_service import automation_service

    settings = get_settings()
    engine = create_async_engine(settings.DATABASE_URL, pool_size=3, max_overflow=2)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    try:
        async with async_session() as db:
            result = await db.execute(
                select(Ticket).where(Ticket.id == UUID(ticket_id))
            )
            ticket = result.scalar_one_or_none()

            if ticket is None:
                logger.warning(
                    "tasks_automation_ticket_not_found",
                    extra={"ticket_id": ticket_id},
                )
                return

            await automation_service.evaluate_ticket_rules(
                db=db,
                redis=None,
                trigger=trigger,
                ticket=ticket,
                changed_fields=changed_fields,
            )
            await db.commit()
    except Exception as exc:
        logger.error(
            "tasks_automation_ticket_async_failed",
            extra={"ticket_id": ticket_id, "trigger": trigger, "error": str(exc)},
        )
    finally:
        await engine.dispose()


async def _async_run_asset_automation(asset_id: str, trigger: str) -> None:
    from uuid import UUID

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker

    from app.config import get_settings
    from app.models.asset import Asset
    from app.services.automation_service import automation_service

    settings = get_settings()
    engine = create_async_engine(settings.DATABASE_URL, pool_size=3, max_overflow=2)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    try:
        async with async_session() as db:
            result = await db.execute(
                select(Asset).where(Asset.id == UUID(asset_id))
            )
            asset = result.scalar_one_or_none()

            if asset is None:
                logger.warning(
                    "tasks_automation_asset_not_found",
                    extra={"asset_id": asset_id},
                )
                return

            await automation_service.evaluate_asset_rules(
                db=db,
                redis=None,
                trigger=trigger,
                asset=asset,
            )
            await db.commit()
    except Exception as exc:
        logger.error(
            "tasks_automation_asset_async_failed",
            extra={"asset_id": asset_id, "trigger": trigger, "error": str(exc)},
        )
    finally:
        await engine.dispose()
