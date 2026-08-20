"""KB wiki-curation API endpoints (KB_WIKI_CURATION_RAG_PLAN Phase 1 + 2).

Manual "curate this doc" admin action (Phase 1): trigger a STORM-style
synthesis run against pasted source text. The ticket-resolved trigger (Phase
2) fires the same underlying run_kb_curation task from
app/workers/tasks_kb.py's auto_draft_kb_from_tickets — nothing here changes
for that path.

Approve/reject (Phase 2): both now resume a checkpointed, interrupt()-paused
LangGraph run rather than writing directly to the DB — approve always
resumes; reject only resumes (and re-synthesizes) when resynthesize=true,
otherwise it's Phase 1's original synchronous archive.

No review UI (itsm-service is API-only — see the Phase 1/2 plans).

Routers exported:
  router → /kb/curation
"""

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, Response, status
import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.kb import (
    _AGENT_ROLES,
    _TEAM_LEAD_ROLES,
    _require_any_role,
    _require_role_or_platform,
    _require_tenant,
    _require_write_authority,
    _resolve_article_scope,
    _service,
)
from app.api.v1.tickets import _resolve_list_scope
from app.auth.dependencies import CurrentUser, get_current_user
from app.database import get_db
from app.exceptions import ResourceNotFoundError, ValidationError
from app.models.kb import KBArticle, KBArticleAccessLevel, KBArticleStatus, KBArticleVisibility, KBSpace, TicketKBLink
from app.models.ticket import TicketStatus
from app.repositories.kb_repo import KBRepository
from app.repositories.ticket_repo import TicketRepository
from app.schemas.kb import (
    KBCurationDraftResponse,
    RejectKBCurationRequest,
    TriggerKBCurationRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/kb/curation", tags=["knowledge-base-curation"])


@router.post("/run", response_model=KBCurationDraftResponse, status_code=status.HTTP_202_ACCEPTED)
async def trigger_curation(
    body: TriggerKBCurationRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> KBCurationDraftResponse:
    """Trigger a curation run against pasted source text (agent+).

    Creates the article row immediately (status=ai_curated_pending_review,
    empty body) and fires the synthesis as a background Celery task — the
    STORM-style pipeline is ~10-15 sequential LLM calls and would risk an
    HTTP timeout if run inline. Poll GET /kb/curation/pending for the result.
    """
    if current_user.tier != "platform":
        _require_tenant(current_user)
        _require_any_role(current_user, _AGENT_ROLES, "trigger a KB curation run")

    access_level = (
        KBArticleAccessLevel.tenant_product if body.product_id is not None else KBArticleAccessLevel.tenant
    )
    tenant_id, product_id = _resolve_article_scope(current_user, access_level, body.product_id)

    space_result = await db.execute(
        select(KBSpace).where(
            KBSpace.id == body.space_id,
            sa.or_(KBSpace.tenant_id == tenant_id, KBSpace.tenant_id.is_(None)),
        )
    )
    space = space_result.scalar_one_or_none()
    if space is None:
        raise ResourceNotFoundError("kb_space", str(body.space_id))

    slug = await _service.make_unique_slug(body.title, body.space_id, tenant_id, db)

    article = KBArticle(
        tenant_id=tenant_id,
        product_id=product_id,
        space_id=body.space_id,
        category_id=body.category_id,
        title=body.title,
        slug=slug,
        body="",
        visibility=KBArticleVisibility.internal,
        author_id=current_user.local_user_id,
        status=KBArticleStatus.ai_curated_pending_review,
        curation_source={
            "trigger": "manual",
            "state": "running",
            "source_title": body.title,
            "source_text": body.source_text,
            "triggered_by": str(current_user.local_user_id) if current_user.local_user_id else None,
        },
    )
    db.add(article)
    await db.commit()
    await db.refresh(article)

    from app.workers.tasks_kb import run_kb_curation
    run_kb_curation.delay(str(article.id))

    return KBCurationDraftResponse.model_validate(article)


@router.post("/from-ticket/{ticket_id}", response_model=KBCurationDraftResponse, status_code=status.HTTP_202_ACCEPTED)
async def trigger_curation_from_ticket(
    ticket_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> KBCurationDraftResponse:
    """Draft a KB article from a resolved ticket right now (agent+), instead of
    waiting for the nightly auto_draft_kb_from_tickets batch (03:30 UTC).

    Idempotent: if this ticket already has a KBArticle linked (from an
    earlier manual trigger or the nightly batch), returns that draft as-is —
    never creates a duplicate or re-runs curation. Response status is 202 for
    a fresh draft, 200 when returning an existing one (FastAPI reports the
    decorator's 202 either way; callers should key off body.state instead).
    """
    if current_user.tier != "platform":
        _require_tenant(current_user)
    _require_any_role(current_user, _AGENT_ROLES, "draft a KB article from this ticket")

    tenant_id = _resolve_list_scope(current_user)
    ticket_repo = TicketRepository(db)
    ticket = await ticket_repo.get_or_404(ticket_id, tenant_id)

    if ticket.status != TicketStatus.resolved:
        raise ValidationError("Ticket must be resolved before drafting a KB article")

    existing_link = (await db.execute(
        select(TicketKBLink).where(TicketKBLink.ticket_id == ticket.id)
    )).scalar_one_or_none()
    if existing_link is not None:
        existing_article = (await db.execute(
            select(KBArticle).where(KBArticle.id == existing_link.article_id)
        )).scalar_one_or_none()
        if existing_article is not None:
            return KBCurationDraftResponse.model_validate(existing_article)

    space_result = await db.execute(
        select(KBSpace)
        .where(KBSpace.tenant_id == ticket.tenant_id, KBSpace.is_active.is_(True))
        .order_by(KBSpace.created_at.asc())
        .limit(1)
    )
    space = space_result.scalar_one_or_none()
    if space is None:
        raise ResourceNotFoundError("kb_space", "no active KB space configured for this tenant")

    from sqlalchemy.orm import joinedload

    from app.models.ticket import Ticket
    from app.workers.tasks_kb import build_ticket_kb_draft_source, run_kb_curation

    ticket_result = await db.execute(
        select(Ticket).options(joinedload(Ticket.category)).where(Ticket.id == ticket.id)
    )
    ticket_with_category = ticket_result.unique().scalar_one()
    source_title, source_text = build_ticket_kb_draft_source(ticket_with_category)

    slug = await _service.make_unique_slug(source_title, space.id, ticket.tenant_id, db)

    article = KBArticle(
        tenant_id=ticket.tenant_id,
        space_id=space.id,
        title=source_title,
        slug=slug,
        body="",
        status=KBArticleStatus.ai_curated_pending_review,
        visibility=KBArticleVisibility.internal,
        author_id=ticket.assignee_id or ticket.requester_id,
        curation_source={
            "trigger": "ticket_resolved",
            "state": "running",
            "source_title": source_title,
            "source_text": source_text,
            "source_ticket_id": str(ticket.id),
            "triggered_by": str(current_user.local_user_id) if current_user.local_user_id else None,
        },
    )
    db.add(article)
    await db.flush()

    link = TicketKBLink(
        ticket_id=ticket.id,
        article_id=article.id,
        linked_by=current_user.local_user_id,
        is_suggested=True,
    )
    db.add(link)
    await db.commit()
    await db.refresh(article)

    run_kb_curation.delay(str(article.id))

    return KBCurationDraftResponse.model_validate(article)


@router.get("/pending", response_model=list[KBCurationDraftResponse])
async def list_pending_curation(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[KBCurationDraftResponse]:
    """List AI-curated drafts awaiting review (agent+, tenant-scoped)."""
    _require_role_or_platform(current_user, _AGENT_ROLES, "view pending curation drafts")

    stmt = select(KBArticle).where(KBArticle.status == KBArticleStatus.ai_curated_pending_review)
    if current_user.tenant_id is not None:
        stmt = stmt.where(KBArticle.tenant_id == current_user.tenant_id)
    stmt = stmt.order_by(KBArticle.created_at.desc())

    result = await db.execute(stmt)
    articles = result.scalars().all()
    return [KBCurationDraftResponse.model_validate(a) for a in articles]


@router.post(
    "/{article_id}/approve",
    response_model=KBCurationDraftResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def approve_curation_draft(
    article_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> KBCurationDraftResponse:
    """Approve an AI-curated draft (team_lead+) — resumes the paused graph run.

    Fires resume_kb_curation, which resumes the interrupt()-paused LangGraph
    run and publishes the article once it reaches END. Async (202) rather
    than Phase 1's synchronous publish, since resuming re-enters the graph.
    Poll GET /kb/curation/pending for the outcome.
    """
    _require_role_or_platform(current_user, _TEAM_LEAD_ROLES, "approve a KB curation draft")

    repo = KBRepository(db)
    article = await repo.get_article_or_404(article_id, current_user.tenant_id)  # type: ignore[arg-type]
    _require_write_authority(current_user, article)

    from app.workers.tasks_kb import resume_kb_curation
    resume_kb_curation.delay(str(article.id), "approve", None)

    return KBCurationDraftResponse.model_validate(article)


@router.post("/{article_id}/reject", response_model=KBCurationDraftResponse)
async def reject_curation_draft(
    article_id: UUID,
    body: RejectKBCurationRequest,
    response: Response,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> KBCurationDraftResponse:
    """Reject an AI-curated draft (team_lead+).

    resynthesize=false (default): Phase 1 behavior — archive immediately
    with reviewer notes, synchronous 200, no graph resume.
    resynthesize=true: Phase 2 — resumes the paused graph with the notes
    injected, regenerates sections, and pauses again for another review
    round. Async (202), draft goes back to curation_source.state="running".
    """
    _require_role_or_platform(current_user, _TEAM_LEAD_ROLES, "reject a KB curation draft")

    repo = KBRepository(db)
    article = await repo.get_article_or_404(article_id, current_user.tenant_id)  # type: ignore[arg-type]
    _require_write_authority(current_user, article)

    if not body.resynthesize:
        updated = await _service.reject_curation_draft(article, body.reviewer_notes, db)
        await db.commit()
        return KBCurationDraftResponse.model_validate(updated)

    from app.workers.tasks_kb import resume_kb_curation
    resume_kb_curation.delay(str(article.id), "reject", body.reviewer_notes)

    response.status_code = status.HTTP_202_ACCEPTED
    return KBCurationDraftResponse.model_validate(article)
