"""
Celery task: process_new_ticket

Triggered after ticket creation.  Runs three AI enrichment steps:
  1. Generate description embedding → store on tickets.description_embedding
  2. Duplicate detection → insert ai_duplicate_candidates rows
  3. Classification → insert ai_ticket_classifications row

Each step is wrapped in its own try/except so a failure in one step does
not prevent the others from running.

AIBudgetExhaustedError is caught globally: if the tenant is out of budget
the task logs a WARNING and exits cleanly — the ticket already exists.

The task is on the "low" queue and retries up to 3 times with exponential
backoff on ExternalServiceError (transient provider errors).
"""

import logging
from uuid import UUID

from celery import Task

from app.exceptions import AIBudgetExhaustedError, ExternalServiceError
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(
    name="app.workers.tasks_ai_ticket.process_new_ticket",
    queue="low",
    bind=True,
    max_retries=3,
    autoretry_for=(ExternalServiceError,),
    retry_backoff=True,
    retry_backoff_max=240,
    retry_jitter=True,
)
def process_new_ticket(self: Task, ticket_id: str, tenant_id: str) -> None:
    """AI enrichment pipeline for a newly created ticket.

    Each step is fault-isolated: failure of embedding does not block
    classification, etc.  If the tenant budget is exhausted, all remaining
    steps are silently skipped (not an error — ticket still exists).

    Args:
        ticket_id:  String UUID of the new ticket.
        tenant_id:  String UUID of the owning tenant.
    """
    import asyncio

    asyncio.run(_process_new_ticket_async(ticket_id, tenant_id))


async def _process_new_ticket_async(ticket_id: str, tenant_id: str) -> None:
    """Async implementation — called from the sync Celery task wrapper."""
    import openai as openai_sdk

    from app.config import get_settings
    from app.database import AsyncSessionLocal
    from app.models.ticket import Ticket, TicketCategory
    from app.models.identity import Team
    from app.redis_client import redis_client
    from app.services.ai.ai_service import AIService
    from app.services.ai.embedder import EmbedderService
    from app.services.ai.duplicate_detector import DuplicateDetector
    from app.services.ai.classifier import TicketClassifier
    from sqlalchemy import select

    s = get_settings()
    ticket_uuid = UUID(ticket_id)
    tenant_uuid = UUID(tenant_id)

    redis = redis_client
    ai_service = AIService()
    openai_client = openai_sdk.AsyncOpenAI(api_key=s.OPENAI_API_KEY)
    embedder = EmbedderService(openai_client=openai_client, redis=redis)
    detector = DuplicateDetector()
    classifier = TicketClassifier(ai_service=ai_service)

    async with AsyncSessionLocal() as db:
        # ------------------------------------------------------------------
        # Step 0: Load ticket
        # ------------------------------------------------------------------
        result = await db.execute(
            select(Ticket).where(
                Ticket.id == ticket_uuid,
                Ticket.tenant_id == tenant_uuid,
            )
        )
        ticket = result.scalar_one_or_none()
        if ticket is None:
            logger.warning(
                "ai_ticket_not_found",
                extra={"ticket_id": ticket_id, "tenant_id": tenant_id},
            )
            return

        embedding: list[float] | None = None

        # ------------------------------------------------------------------
        # Step 1: Generate embedding and store on ticket
        # ------------------------------------------------------------------
        try:
            await ai_service.check_budget(tenant_id, redis)
            embedding = await embedder.embed_text(ticket.description)
            ticket.description_embedding = embedding  # type: ignore[assignment]
            db.add(ticket)
            await db.flush()
            logger.info(
                "ticket_embedding_stored",
                extra={"ticket_id": ticket_id},
            )
        except AIBudgetExhaustedError:
            logger.warning(
                "ai_budget_exhausted_skip_enrichment",
                extra={"ticket_id": ticket_id, "tenant_id": tenant_id},
            )
            await db.commit()
            return
        except Exception as exc:
            logger.error(
                "ticket_embedding_failed",
                extra={
                    "ticket_id": ticket_id,
                    "exception_type": type(exc).__name__,
                    "exception": str(exc),
                },
            )
            # Continue — duplicate detection and classification may still work
            # if embedding was previously set (or skip gracefully below)

        # ------------------------------------------------------------------
        # Step 2: Duplicate detection (only if we have an embedding)
        # ------------------------------------------------------------------
        if embedding is not None:
            try:
                await ai_service.check_budget(tenant_id, redis)
                await detector.find_duplicates(
                    db=db,
                    ticket_id=ticket_uuid,
                    tenant_id=tenant_uuid,
                    embedding=embedding,
                )
                await db.flush()
                logger.info(
                    "duplicate_detection_done",
                    extra={"ticket_id": ticket_id},
                )
            except AIBudgetExhaustedError:
                logger.warning(
                    "ai_budget_exhausted_skip_duplicate_detection",
                    extra={"ticket_id": ticket_id, "tenant_id": tenant_id},
                )
                await db.commit()
                return
            except Exception as exc:
                logger.error(
                    "duplicate_detection_failed",
                    extra={
                        "ticket_id": ticket_id,
                        "exception_type": type(exc).__name__,
                        "exception": str(exc),
                    },
                )

        # ------------------------------------------------------------------
        # Step 3: Classification
        # ------------------------------------------------------------------
        try:
            await ai_service.check_budget(tenant_id, redis)

            # Load available categories and teams for this tenant
            cat_result = await db.execute(
                select(TicketCategory.id, TicketCategory.name).where(
                    TicketCategory.tenant_id == tenant_uuid,
                    TicketCategory.is_active.is_(True),
                )
            )
            available_categories = [
                {"id": str(row[0]), "name": row[1]} for row in cat_result.all()
            ]

            team_result = await db.execute(
                select(Team.id, Team.name).where(Team.tenant_id == tenant_uuid)
            )
            available_teams = [
                {"id": str(row[0]), "name": row[1]} for row in team_result.all()
            ]

            await classifier.classify(
                db=db,
                redis=redis,
                ticket=ticket,
                tenant_id=tenant_uuid,
                available_categories=available_categories,
                available_teams=available_teams,
            )
            await db.flush()
            logger.info(
                "ticket_classification_done",
                extra={"ticket_id": ticket_id},
            )
        except AIBudgetExhaustedError:
            logger.warning(
                "ai_budget_exhausted_skip_classification",
                extra={"ticket_id": ticket_id, "tenant_id": tenant_id},
            )
        except Exception as exc:
            logger.error(
                "ticket_classification_failed",
                extra={
                    "ticket_id": ticket_id,
                    "exception_type": type(exc).__name__,
                    "exception": str(exc),
                },
            )

        # ------------------------------------------------------------------
        # Commit all successful steps
        # ------------------------------------------------------------------
        await db.commit()
