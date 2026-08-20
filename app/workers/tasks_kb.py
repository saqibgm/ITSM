"""
Celery beat tasks for KB maintenance.

refresh_kb_embeddings   — 03:00 UTC daily
    Embeds any published article whose embedding is NULL or whose content was
    updated in the last 24 h.  Uses batch embedding for efficiency.
    AIBudgetExhaustedError → log WARNING, stop processing, return.

auto_draft_kb_from_tickets — 03:30 UTC daily
    For each active tenant, loads resolved tickets from the past 24 h that
    have no linked KB article, creates a pending KBArticle row per ticket
    (status=ai_curated_pending_review) linked via TicketKBLink, and dispatches
    run_kb_curation for each — the same STORM pipeline the manual trigger
    uses. Replaces the old single-LLM-call KBDrafter (retired, Phase 2).

run_kb_curation — on-demand (fired by POST /kb/curation/run or by
auto_draft_kb_from_tickets, not scheduled itself)
    Runs the STORM-style synthesis subgraph (KBCurator) against a single
    pending KBArticle row and fills in the synthesized title/body/citations.
    Pauses at a human-review interrupt() (durable, Postgres-checkpointed) —
    resumed later via resume_kb_curation. See KB_WIKI_CURATION_RAG_PLAN.md
    Phase 1 (manual trigger, no pause) / Phase 2 (real trigger, durable pause).

resume_kb_curation — fired by POST /kb/curation/{id}/approve or
/{id}/reject?resynthesize=true
    Resumes a paused curation run with a reviewer decision. Approve completes
    the graph and publishes the article; reject+resynthesize loops back into
    section-writing with reviewer notes and pauses again for another review.

cluster_kb_gaps_and_draft — 04:00 UTC daily
    Reads Project-IQ-V2's kb_search_gaps table directly (a separate repo/DB —
    that repo has no scheduler of its own; see KB_WIKI_CURATION_RAG_PLAN.md
    Phase 5), clusters unanswered queries by embedding similarity within each
    tenant, and for clusters at/above a volume threshold, drafts a curation
    article the same way every other trigger does. Writes back
    curation_article_id on the source gap rows so they aren't reprocessed.

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
    """Async implementation of refresh_kb_embeddings.

    Two passes over the same changed-article set: whole-article embedding
    (existing, unchanged), then heading-aware chunking + per-chunk embedding
    into kb_chunks (Phase 3) — a separate pass since chunk counts vary per
    article and can't share the fixed-size batching the whole-article pass
    uses. A failure in the chunking pass doesn't roll back the (already
    committed) whole-article embeddings from the first pass.
    """
    import openai as openai_sdk
    import sqlalchemy as sa
    from sqlalchemy import func, select, text

    from app.config import get_settings
    from app.database import AsyncSessionLocal
    from app.models.kb import KBArticle, KBArticleStatus, KBChunk
    from app.redis_client import get_worker_redis_client
    from app.services.ai.ai_service import AIService
    from app.services.ai.embedder import EmbedderService
    from app.services.ai.kb_chunker import chunk_article

    s = get_settings()
    redis = get_worker_redis_client()
    openai_client = openai_sdk.AsyncOpenAI(api_key=s.OPENAI_API_KEY)
    embedder = EmbedderService(openai_client=openai_client, redis=redis)

    articles_updated = 0
    _BATCH_SIZE = 50

    async with AsyncSessionLocal() as db:
        # Load articles that need (re-)embedding/(re-)chunking:
        # published AND (embedding IS NULL OR updated_at > now() - 24h)
        result = await db.execute(
            select(
                KBArticle.id, KBArticle.title, KBArticle.body,
                KBArticle.tenant_id, KBArticle.space_id, KBArticle.visibility,
            ).where(
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
    article_bodies = [row[2] for row in rows]

    # ---- Pass 1: whole-article embedding (unchanged from before Phase 3) ----
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

    # ---- Pass 2: heading-aware chunking + per-chunk embedding (Phase 3) ----
    chunks_updated = 0
    for article_id, title, body, tenant_id, space_id, visibility in rows:
        pieces = chunk_article(title, body)
        if not pieces:
            continue

        try:
            chunk_embeddings = await embedder.embed_batch([p["content"] for p in pieces])
        except AIBudgetExhaustedError:
            logger.warning(
                "refresh_kb_embeddings_chunking_budget_exhausted",
                extra={"chunks_updated_so_far": chunks_updated},
            )
            return
        except Exception as exc:
            logger.error(
                "refresh_kb_embeddings_chunking_failed",
                extra={"article_id": str(article_id), "exception_type": type(exc).__name__, "exception": str(exc)},
            )
            continue  # skip this article's chunks, keep processing the rest

        async with AsyncSessionLocal() as db:
            await db.execute(sa.delete(KBChunk).where(KBChunk.article_id == article_id))
            for idx, (piece, embedding) in enumerate(zip(pieces, chunk_embeddings)):
                db.add(KBChunk(
                    article_id=article_id,
                    tenant_id=tenant_id,
                    space_id=space_id,
                    visibility=visibility,
                    chunk_index=idx,
                    heading=piece["heading"][:500] if piece["heading"] else None,
                    content=piece["content"],
                    embedding=embedding,
                ))
            await db.commit()
            chunks_updated += len(pieces)

    logger.info(
        "refresh_kb_embeddings_complete",
        extra={"articles_updated": articles_updated, "chunks_updated": chunks_updated},
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
    """Async implementation of auto_draft_kb_from_tickets.

    Creates pending KBArticle rows + dispatches run_kb_curation per ticket —
    makes no LLM calls itself (all synthesis happens in the dispatched task),
    so there's no AIBudgetExhaustedError handling needed here anymore.
    """
    import sqlalchemy as sa
    from sqlalchemy import func, select
    from sqlalchemy.orm import joinedload

    from app.database import AsyncSessionLocal
    from app.models.identity import Tenant
    from app.models.kb import KBArticle, KBArticleStatus, KBArticleVisibility, KBSpace, TicketKBLink
    from app.models.ticket import Ticket, TicketStatus
    from app.services.kb_service import KBService

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
        dispatched_article_ids: list = []

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

                    category_name = ""
                    try:
                        if ticket.category is not None:
                            category_name = ticket.category.name
                    except Exception:
                        pass  # category may not be loaded; skip gracefully

                    source_title = ticket.title[:500]
                    source_text = (
                        f"Ticket ID: {ticket.id}\n"
                        f"Title: {ticket.title}\n"
                        f"Description: {ticket.description}\n"
                        f"Category: {category_name or 'N/A'}\n"
                        f"Resolution note: {resolution_note or 'No resolution note provided.'}"
                    )

                    # Determine author_id: prefer ticket assignee, fall back to requester
                    author_id = ticket.assignee_id or ticket.requester_id

                    slug = await kb_service.make_unique_slug(
                        title=source_title,
                        space_id=default_space.id,
                        tenant_id=tenant_id,
                        db=db,
                    )

                    article = KBArticle(
                        tenant_id=tenant_id,
                        space_id=default_space.id,
                        title=source_title,
                        slug=slug,
                        body="",
                        status=KBArticleStatus.ai_curated_pending_review,
                        visibility=KBArticleVisibility.internal,
                        author_id=author_id,
                        curation_source={
                            "trigger": "ticket_resolved",
                            "state": "running",
                            "source_title": source_title,
                            "source_text": source_text,
                            "source_ticket_id": str(ticket.id),
                        },
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

                    dispatched_article_ids.append(article.id)
                    drafts_created += 1

                await db.commit()

            # Dispatch only after commit succeeds, so a rollback never leaves
            # a task pointed at a row that doesn't exist.
            for article_id in dispatched_article_ids:
                run_kb_curation.delay(str(article_id))

            logger.info(
                "auto_draft_kb_tenant_complete",
                extra={
                    "tenant_id": str(tenant_id),
                    "tickets_processed": tickets_processed,
                    "drafts_created": drafts_created,
                },
            )

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


# ---------------------------------------------------------------------------
# Task 3: run_kb_curation (on-demand, fired by POST /kb/curation/run)
# ---------------------------------------------------------------------------


@celery_app.task(
    name="app.workers.tasks_kb.run_kb_curation",
    queue="low",
    bind=True,
    max_retries=1,
    soft_time_limit=180,
    time_limit=210,
)
def run_kb_curation(self, article_id: str) -> None:  # type: ignore[override]
    """Run the STORM synthesis subgraph for one pending curation draft.

    A hung upstream LLM call (no per-call timeout in the AI provider chain)
    would otherwise leave the draft stuck in "running" forever with no
    self-healing — the soft limit raises inside the task so the except
    branch in _run_kb_curation_async can still record a "failed" state
    before the hard limit kills the process.
    """
    asyncio.run(_run_kb_curation_async(article_id))


async def _run_kb_curation_async(article_id: str) -> None:
    """Async implementation of run_kb_curation."""
    from sqlalchemy import select

    from app.database import AsyncSessionLocal
    from app.models.kb import KBArticle, KBArticleStatus
    from app.redis_client import get_worker_redis_client
    from app.repositories.kb_repo import KBRepository
    from app.services.ai.ai_service import AIService
    from app.services.ai.kb_curation_checkpointer import get_checkpointer
    from app.services.ai.kb_curator import KBCurator

    redis = get_worker_redis_client()
    ai_service = AIService()

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(KBArticle).where(KBArticle.id == article_id))
        article = result.scalar_one_or_none()

        if article is None:
            logger.error("run_kb_curation_article_not_found", extra={"article_id": article_id})
            return

        if article.status != KBArticleStatus.ai_curated_pending_review:
            logger.warning(
                "run_kb_curation_unexpected_status",
                extra={"article_id": article_id, "status": article.status.value},
            )
            return

        source = dict(article.curation_source or {})
        source_title = source.get("source_title", article.title)
        source_text = source.get("source_text", "")
        category_id = article.category_id

        # Fetch related existing wiki pages (published, same tenant+space) for
        # the interview node to ground answers against — reuses the existing
        # hybrid-search repository method rather than a new retrieval path.
        related_context = ""
        try:
            repo = KBRepository(db)
            embedding = await ai_service.embed(source_title)
            related = await repo.search_hybrid(
                tenant_id=article.tenant_id,
                query_text=source_title,
                query_embedding=embedding,
                space_id=article.space_id,
                limit=3,
                viewer_role="agent",
            )
            related_context = "\n\n".join(
                f"### {a.title}\n{(a.body or '')[:500]}" for a, _score in related
            )
        except Exception as exc:
            logger.warning(
                "run_kb_curation_related_context_failed",
                extra={"article_id": article_id, "exception_type": type(exc).__name__, "exception": str(exc)},
            )

        curator = KBCurator(ai_service=ai_service, redis=redis)

        try:
            async with get_checkpointer() as checkpointer:
                synthesized = await curator.curate(
                    thread_id=article_id,
                    checkpointer=checkpointer,
                    tenant_id=str(article.tenant_id),
                    source_title=source_title,
                    source_body=source_text,
                    related_context=related_context,
                )
        except AIBudgetExhaustedError:
            source["state"] = "failed"
            source["error"] = "AI budget exhausted"
            article.curation_source = source
            await db.commit()
            logger.warning("run_kb_curation_budget_exhausted", extra={"article_id": article_id})
            return
        except Exception as exc:
            source["state"] = "failed"
            source["error"] = str(exc)
            article.curation_source = source
            await db.commit()
            logger.error(
                "run_kb_curation_failed",
                extra={"article_id": article_id, "exception_type": type(exc).__name__, "exception": str(exc)},
            )
            return

        lint_findings = list(synthesized["lint_findings"])
        if category_id is None:
            lint_findings.append("Uncategorized — no category_id set (orphan risk).")

        article.title = synthesized["title"][:500]
        article.body = synthesized["body"]
        article.excerpt = synthesized["body"][:300] if synthesized["body"] else None
        source["state"] = "ready_for_review" if synthesized["paused"] else "unexpected_completion"
        source["citations"] = synthesized["citations"]
        source["lint_findings"] = lint_findings
        article.curation_source = source
        await db.commit()

        logger.info(
            "run_kb_curation_complete",
            extra={"article_id": article_id, "citations": len(synthesized["citations"]), "paused": synthesized["paused"]},
        )


# ---------------------------------------------------------------------------
# Task 4: resume_kb_curation (fired by POST /kb/curation/{id}/approve or
# /{id}/reject?resynthesize=true)
# ---------------------------------------------------------------------------


@celery_app.task(
    name="app.workers.tasks_kb.resume_kb_curation",
    queue="low",
    bind=True,
    max_retries=1,
)
def resume_kb_curation(self, article_id: str, decision: str, notes: str | None = None) -> None:  # type: ignore[override]
    """Resume a paused curation run with a reviewer decision."""
    asyncio.run(_resume_kb_curation_async(article_id, decision, notes))


async def _resume_kb_curation_async(article_id: str, decision: str, notes: str | None) -> None:
    """Async implementation of resume_kb_curation."""
    from sqlalchemy import select

    from app.database import AsyncSessionLocal
    from app.models.kb import KBArticle, KBArticleStatus
    from app.redis_client import get_worker_redis_client
    from app.services.ai.ai_service import AIService
    from app.services.ai.kb_curation_checkpointer import get_checkpointer
    from app.services.ai.kb_curator import KBCurator
    from app.services.kb_service import KBService

    redis = get_worker_redis_client()
    ai_service = AIService()
    kb_service = KBService()

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(KBArticle).where(KBArticle.id == article_id))
        article = result.scalar_one_or_none()

        if article is None:
            logger.error("resume_kb_curation_article_not_found", extra={"article_id": article_id})
            return

        if article.status != KBArticleStatus.ai_curated_pending_review:
            logger.warning(
                "resume_kb_curation_unexpected_status",
                extra={"article_id": article_id, "status": article.status.value},
            )
            return

        source = dict(article.curation_source or {})
        curator = KBCurator(ai_service=ai_service, redis=redis)

        try:
            async with get_checkpointer() as checkpointer:
                result_state = await curator.resume(
                    thread_id=article_id,
                    checkpointer=checkpointer,
                    decision=decision,
                    notes=notes,
                )
        except AIBudgetExhaustedError:
            source["state"] = "failed"
            source["error"] = "AI budget exhausted"
            article.curation_source = source
            await db.commit()
            logger.warning("resume_kb_curation_budget_exhausted", extra={"article_id": article_id})
            return
        except Exception as exc:
            source["state"] = "failed"
            source["error"] = str(exc)
            article.curation_source = source
            await db.commit()
            logger.error(
                "resume_kb_curation_failed",
                extra={"article_id": article_id, "exception_type": type(exc).__name__, "exception": str(exc)},
            )
            return

        if result_state["paused"]:
            # Rejected + resynthesized — regenerated draft pauses again for review.
            article.title = result_state["title"][:500]
            article.body = result_state["body"]
            article.excerpt = result_state["body"][:300] if result_state["body"] else None
            source["state"] = "ready_for_review"
            source["citations"] = result_state["citations"]
            source["lint_findings"] = result_state["lint_findings"]
            source["reviewer_notes"] = notes
            article.curation_source = source
            await db.commit()
            logger.info("resume_kb_curation_resynthesized", extra={"article_id": article_id})
            return

        # decision == "approve": graph reached END, publish for real.
        source["state"] = "published"
        article.curation_source = source
        await kb_service.publish_article(
            article,
            actor_id=article.author_id,
            actor_roles=["system"],
            db=db,
        )
        await db.commit()
        logger.info("resume_kb_curation_published", extra={"article_id": article_id})


# ---------------------------------------------------------------------------
# Task 5: cluster_kb_gaps_and_draft (04:00 UTC daily)
# ---------------------------------------------------------------------------


@celery_app.task(
    name="app.workers.tasks_kb.cluster_kb_gaps_and_draft",
    queue="low",
    bind=True,
    max_retries=1,
)
def cluster_kb_gaps_and_draft(self) -> None:  # type: ignore[override]
    """Nightly job: cluster unanswered KB queries and draft curation articles."""
    asyncio.run(_cluster_kb_gaps_and_draft_async())


def _piq_dsn() -> str:
    from app.config import get_settings
    s = get_settings()
    return (
        f"host={s.PIQ_DB_HOST} port={s.PIQ_DB_PORT} dbname={s.PIQ_DB_NAME} "
        f"user={s.PIQ_DB_USER} password={s.PIQ_DB_PASSWORD}"
    )


async def _cluster_kb_gaps_and_draft_async() -> None:
    """Async implementation of cluster_kb_gaps_and_draft."""
    import openai as openai_sdk
    import psycopg
    from sqlalchemy import select

    from app.config import get_settings
    from app.database import AsyncSessionLocal
    from app.models.identity import Tenant
    from app.models.kb import KBArticle, KBArticleStatus, KBArticleVisibility, KBSpace
    from app.redis_client import get_worker_redis_client
    from app.services.ai.ai_service import AIService
    from app.services.ai.embedder import EmbedderService
    from app.services.ai.kb_gap_clusterer import cluster_gaps
    from app.services.kb_service import KBService

    s = get_settings()
    redis = get_worker_redis_client()
    openai_client = openai_sdk.AsyncOpenAI(api_key=s.OPENAI_API_KEY)
    embedder = EmbedderService(openai_client=openai_client, redis=redis)
    kb_service = KBService()

    # 1. Read unprocessed gaps from Project-IQ-V2's DB directly — that repo has
    # no scheduler of its own (see module docstring). A blocking psycopg read
    # is acceptable here: this runs once a day, not on a request path.
    dsn = _piq_dsn()
    try:
        with psycopg.connect(dsn, connect_timeout=8) as piq_conn:
            with piq_conn.cursor() as cur:
                cur.execute(
                    "SELECT id, query, org_id FROM kb_search_gaps "
                    "WHERE curation_article_id IS NULL AND org_id IS NOT NULL "
                    f"AND searched_at > NOW() - INTERVAL '{int(s.KB_GAP_CLUSTER_LOOKBACK_DAYS)} days'"
                )
                rows = cur.fetchall()
    except Exception as exc:
        logger.error(
            "cluster_kb_gaps_connect_failed",
            extra={"exception_type": type(exc).__name__, "exception": str(exc)},
        )
        return

    if not rows:
        logger.info("cluster_kb_gaps_nothing_to_do")
        return

    # 2. Embed each distinct query text (dedupe to save embedding calls)
    unique_queries = list({row[1] for row in rows})
    try:
        embeddings = await embedder.embed_batch(unique_queries)
    except AIBudgetExhaustedError:
        logger.warning("cluster_kb_gaps_budget_exhausted")
        return
    except Exception as exc:
        logger.error(
            "cluster_kb_gaps_embed_failed",
            extra={"exception_type": type(exc).__name__, "exception": str(exc)},
        )
        return

    embedding_by_query = dict(zip(unique_queries, embeddings))
    gap_rows = [
        {"id": row[0], "query": row[1], "org_id": row[2], "embedding": embedding_by_query[row[1]]}
        for row in rows
    ]

    # 3. Cluster within each tenant (never crosses org_id boundaries)
    clusters = cluster_gaps(
        gap_rows,
        similarity_threshold=s.KB_GAP_CLUSTER_SIMILARITY_THRESHOLD,
        min_count=s.KB_GAP_CLUSTER_MIN_COUNT,
    )

    if not clusters:
        logger.info("cluster_kb_gaps_no_clusters_met_threshold", extra={"gaps_checked": len(rows)})
        return

    drafted = 0

    async with AsyncSessionLocal() as db:
        for cluster in clusters:
            tenant_result = await db.execute(
                select(Tenant.id).where(Tenant.iam_org_id == cluster["org_id"])
            )
            tenant_id = tenant_result.scalar_one_or_none()
            if tenant_id is None:
                # Unknown/renamed org — leave these gap rows unprocessed rather
                # than silently dropping them; a human can investigate via the
                # admin KB-gaps report in the meantime.
                logger.warning("cluster_kb_gaps_unknown_org", extra={"org_id": cluster["org_id"]})
                continue

            space_result = await db.execute(
                select(KBSpace)
                .where(KBSpace.tenant_id == tenant_id, KBSpace.is_active.is_(True))
                .order_by(KBSpace.created_at.asc())
                .limit(1)
            )
            default_space = space_result.scalar_one_or_none()
            if default_space is None:
                logger.warning("cluster_kb_gaps_no_space", extra={"tenant_id": str(tenant_id)})
                continue

            source_title = cluster["queries"][0][:500]
            source_text = "Recurring questions users have asked with no answer found:\n" + "\n".join(
                f"- {q}" for q in cluster["queries"][:10]
            )

            slug = await kb_service.make_unique_slug(
                title=source_title, space_id=default_space.id, tenant_id=tenant_id, db=db,
            )
            article = KBArticle(
                tenant_id=tenant_id,
                space_id=default_space.id,
                title=source_title,
                slug=slug,
                body="",
                status=KBArticleStatus.ai_curated_pending_review,
                visibility=KBArticleVisibility.internal,
                author_id=None,
                curation_source={
                    "trigger": "gap_feedback",
                    "state": "running",
                    "source_title": source_title,
                    "source_text": source_text,
                    "gap_count": cluster["count"],
                },
            )
            db.add(article)
            await db.flush()
            await db.refresh(article)
            await db.commit()

            run_kb_curation.delay(str(article.id))
            drafted += 1

            # Mark these gap rows processed immediately after dispatch, so a
            # failure in a LATER cluster this run doesn't cause this one to be
            # redrafted tomorrow.
            try:
                with psycopg.connect(dsn, connect_timeout=8) as piq_conn:
                    with piq_conn.cursor() as write_cur:
                        write_cur.execute(
                            "UPDATE kb_search_gaps SET curation_article_id = %s WHERE id = ANY(%s)",
                            (str(article.id), cluster["gap_ids"]),
                        )
                    piq_conn.commit()
            except Exception as exc:
                logger.error(
                    "cluster_kb_gaps_writeback_failed",
                    extra={"article_id": str(article.id), "exception_type": type(exc).__name__, "exception": str(exc)},
                )

    logger.info("cluster_kb_gaps_complete", extra={"gaps_checked": len(rows), "clusters_found": len(clusters), "drafted": drafted})
