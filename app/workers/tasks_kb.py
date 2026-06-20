"""
Celery beat tasks for KB maintenance.

refresh_kb_embeddings   — 03:00 UTC daily
    Embeds any published article whose embedding is NULL or whose content was
    updated in the last 24 h.  Uses batch embedding for efficiency.
    AIBudgetExhaustedError → log WARNING, stop processing, return.

auto_draft_kb_from_tickets — 03:30 UTC daily
    For each active tenant, loads resolved tickets from the past 24 h that
    have no linked KB article, then calls KBDrafter to generate a draft and
    creates a KBArticle in draft status linked back to the ticket.
    AIBudgetExhaustedError → log WARNING, skip rest of tenant, continue next.

Security:
  - All SQL uses SQLAlchemy ORM or text() with :named params.
  - No user content is interpolated into SQL strings.
  - tenant_id always comes from the DB loop variable, never from request body.
"""

import asyncio
import logging

from app.exceptions import AIBudgetExhaustedError
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Task 1: refresh_kb_embeddings
# ---------------------------------------------------------------------------


@celery_app.task(
    name="app.workers.tasks_kb.refresh_kb_embeddings",
    queue="low",
    bind=True,
    max_retries=1,
)
def refresh_kb_embeddings(self) -> None:  # type: ignore[override]
    """Nightly job: generate/refresh embeddings for published KB articles."""
    asyncio.run(_refresh_kb_embeddings_async())


async def _refresh_kb_embeddings_async() -> None:
    """Async implementation of refresh_kb_embeddings."""
    import openai as openai_sdk
    import sqlalchemy as sa
    from sqlalchemy import func, select, text

    from app.config import get_settings
    from app.database import AsyncSessionLocal
    from app.models.kb import KBArticle, KBArticleStatus
    from app.redis_client import redis_client
    from app.services.ai.ai_service import AIService
    from app.services.ai.embedder import EmbedderService

    s = get_settings()
    redis = redis_client
    openai_client = openai_sdk.AsyncOpenAI(api_key=s.OPENAI_API_KEY)
    embedder = EmbedderService(openai_client=openai_client, redis=redis)

    articles_updated = 0
    _BATCH_SIZE = 50

    async with AsyncSessionLocal() as db:
        # Load articles that need (re-)embedding:
        # published AND (embedding IS NULL OR updated_at > now() - 24h)
        result = await db.execute(
            select(KBArticle.id, KBArticle.body).where(
                KBArticle.status == KBArticleStatus.published,
                sa.or_(
                    KBArticle.embedding.is_(None),
                    KBArticle.updated_at > func.now() - sa.text("INTERVAL '24 hours'"),
                ),
            )
        )
        rows = result.all()

    if not rows:
        logger.info(
            "refresh_kb_embeddings_nothing_to_do",
            extra={"articles_checked": 0},
        )
        return

    article_ids = [row[0] for row in rows]
    article_bodies = [row[1] for row in rows]

    # Process in batches
    for batch_start in range(0, len(article_ids), _BATCH_SIZE):
        batch_ids = article_ids[batch_start : batch_start + _BATCH_SIZE]
        batch_bodies = article_bodies[batch_start : batch_start + _BATCH_SIZE]

        try:
            embeddings = await embedder.embed_batch(batch_bodies)
        except AIBudgetExhaustedError:
            logger.warning(
                "refresh_kb_embeddings_budget_exhausted",
                extra={"articles_updated_so_far": articles_updated},
            )
            return
        except Exception as exc:
            logger.error(
                "refresh_kb_embeddings_batch_failed",
                extra={
                    "batch_start": batch_start,
                    "exception_type": type(exc).__name__,
                    "exception": str(exc),
                },
            )
            # Stop processing on transient failure; nightly job will retry tomorrow
            return

        # UPDATE each article's embedding using UPDATE stmt (not ORM object load)
        async with AsyncSessionLocal() as db:
            for article_id, embedding in zip(batch_ids, embeddings):
                await db.execute(
                    sa.update(KBArticle)
                    .where(KBArticle.id == article_id)
                    .values(embedding=str(embedding))
                )
            await db.commit()

        articles_updated += len(batch_ids)

    logger.info(
        "refresh_kb_embeddings_complete",
        extra={"articles_updated": articles_updated},
    )


# ---------------------------------------------------------------------------
# Task 2: auto_draft_kb_from_tickets
# ---------------------------------------------------------------------------


@celery_app.task(
    name="app.workers.tasks_kb.auto_draft_kb_from_tickets",
    queue="low",
    bind=True,
    max_retries=1,
)
def auto_draft_kb_from_tickets(self) -> None:  # type: ignore[override]
    """Nightly job: auto-draft KB articles from resolved tickets."""
    asyncio.run(_auto_draft_kb_from_tickets_async())


async def _auto_draft_kb_from_tickets_async() -> None:
    """Async implementation of auto_draft_kb_from_tickets."""
    import sqlalchemy as sa
    from sqlalchemy import func, select
    from sqlalchemy.orm import joinedload

    from app.config import get_settings
    from app.database import AsyncSessionLocal
    from app.models.identity import Tenant
    from app.models.kb import KBArticle, KBArticleStatus, KBArticleVisibility, KBSpace, TicketKBLink
    from app.models.ticket import Ticket, TicketCategory, TicketStatus
    from app.redis_client import redis_client
    from app.services.ai.ai_service import AIService
    from app.services.ai.kb_drafter import KBDrafter
    from app.services.kb_service import KBService

    redis = redis_client
    ai_service = AIService()
    drafter = KBDrafter()
    kb_service = KBService()

    # Load all active tenants
    async with AsyncSessionLocal() as db:
        tenant_result = await db.execute(
            select(Tenant.id).where(Tenant.is_active.is_(True))
        )
        tenant_ids = [row[0] for row in tenant_result.all()]

    for tenant_id in tenant_ids:
        tickets_processed = 0
        drafts_created = 0

        try:
            async with AsyncSessionLocal() as db:
                # Load resolved tickets from past 24 h with no KB link
                tickets_result = await db.execute(
                    select(Ticket)
                    .outerjoin(
                        TicketKBLink,
                        TicketKBLink.ticket_id == Ticket.id,
                    )
                    .where(
                        Ticket.tenant_id == tenant_id,
                        Ticket.status == TicketStatus.resolved,
                        Ticket.resolved_at > func.now() - sa.text("INTERVAL '24 hours'"),
                        TicketKBLink.article_id.is_(None),
                    )
                    .options(
                        joinedload(Ticket.category),
                    )
                )
                tickets = list(tickets_result.unique().scalars().all())

                if not tickets:
                    logger.info(
                        "auto_draft_kb_no_tickets",
                        extra={"tenant_id": str(tenant_id)},
                    )
                    continue

                # Resolve default space for this tenant (first active space)
                space_result = await db.execute(
                    select(KBSpace)
                    .where(
                        KBSpace.tenant_id == tenant_id,
                        KBSpace.is_active.is_(True),
                    )
                    .order_by(KBSpace.created_at.asc())
                    .limit(1)
                )
                default_space = space_result.scalar_one_or_none()

                if default_space is None:
                    logger.warning(
                        "auto_draft_kb_no_space",
                        extra={"tenant_id": str(tenant_id)},
                    )
                    continue

                for ticket in tickets:
                    tickets_processed += 1
                    resolution_note: str = ""
                    # resolution_note field may not exist on all ticket models;
                    # access defensively
                    if hasattr(ticket, "resolution_note") and ticket.resolution_note:
                        resolution_note = ticket.resolution_note

                    try:
                        draft = await drafter.draft_from_ticket(
                            ticket=ticket,
                            resolution_note=resolution_note,
                            ai_service=ai_service,
                            redis=redis,
                        )
                    except AIBudgetExhaustedError:
                        logger.warning(
                            "auto_draft_kb_budget_exhausted",
                            extra={
                                "tenant_id": str(tenant_id),
                                "tickets_processed": tickets_processed,
                                "drafts_created": drafts_created,
                            },
                        )
                        await db.commit()
                        break  # stop processing this tenant

                    if draft is None:
                        # Parse failure already logged inside KBDrafter
                        continue

                    # Determine author_id: prefer ticket assignee, fall back to requester
                    author_id = ticket.assignee_id or ticket.requester_id

                    # Generate unique slug for this space
                    slug = await kb_service.make_unique_slug(
                        title=draft["title"],
                        space_id=default_space.id,
                        tenant_id=tenant_id,
                        db=db,
                    )

                    article = KBArticle(
                        tenant_id=tenant_id,
                        space_id=default_space.id,
                        title=draft["title"],
                        slug=slug,
                        body=draft["content"],
                        excerpt=draft["content"][:300] if draft["content"] else None,
                        status=KBArticleStatus.draft,
                        visibility=KBArticleVisibility.internal,
                        author_id=author_id,
                    )
                    db.add(article)
                    await db.flush()
                    await db.refresh(article)

                    # Link the source ticket to the new draft article
                    link = TicketKBLink(
                        ticket_id=ticket.id,
                        article_id=article.id,
                        linked_by=None,  # system-generated
                        is_suggested=True,
                    )
                    db.add(link)
                    await db.flush()

                    drafts_created += 1

                else:
                    # Normal completion (no budget break)
                    await db.commit()

                    logger.info(
                        "auto_draft_kb_tenant_complete",
                        extra={
                            "tenant_id": str(tenant_id),
                            "tickets_processed": tickets_processed,
                            "drafts_created": drafts_created,
                        },
                    )
                    continue

                # We broke out of the ticket loop (budget exhausted)
                logger.info(
                    "auto_draft_kb_tenant_partial",
                    extra={
                        "tenant_id": str(tenant_id),
                        "tickets_processed": tickets_processed,
                        "drafts_created": drafts_created,
                    },
                )
                continue

        except AIBudgetExhaustedError:
            # Budget exhausted before even entering the ticket loop
            logger.warning(
                "auto_draft_kb_tenant_budget_exhausted_early",
                extra={"tenant_id": str(tenant_id)},
            )
            continue

        except Exception as exc:
            logger.error(
                "auto_draft_kb_tenant_error",
                extra={
                    "tenant_id": str(tenant_id),
                    "exception_type": type(exc).__name__,
                    "exception": str(exc),
                },
            )
            continue
