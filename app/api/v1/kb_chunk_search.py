"""KB chunk search endpoint (KB_WIKI_CURATION_RAG_PLAN Phase 3).

GET /kb/chunks/search returns actual chunk text (not just article metadata
like the existing /kb/search) — this is what a bot needs for a focused
answer snippet, not a "which article matches" lookup.

Auth is genuinely optional here, unlike /kb/search's CurrentUser | None type
hint (which is misleading — get_current_user always raises without a token).
Anonymous callers are scoped server-side to global (tenant_id IS NULL) +
visibility=public content only — see KBRepository.search_chunks_global,
which has no tenant_id parameter at all, so there's no way to misuse it into
leaking another tenant's content. No new auth mechanism: authenticated
callers use their existing IAM JWT exactly as every other endpoint does.

Routers exported:
  router → /kb/chunks
"""

import logging

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentUser, get_current_user, oauth2_scheme
from app.database import get_db
from app.exceptions import AuthenticationError
from app.repositories.kb_repo import KBRepository
from app.schemas.kb import KBChunkSearchResult
from app.services.ai.ai_service import ai_service as _ai_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/kb/chunks", tags=["knowledge-base-chunk-search"])

_AGENT_ROLES = {"agent", "team_lead", "manager", "admin"}


async def _get_current_user_optional(
    request: Request,
    token: str | None = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> CurrentUser | None:
    """Like get_current_user, but returns None instead of raising when no
    (or an invalid) token is present — this endpoint's anonymous path is a
    deliberate, safely-scoped feature, not a fallback for broken auth.

    Deliberately consults request.app.dependency_overrides for get_current_user
    (rather than calling it directly) so this composes with the standard test
    fixtures (agent_client, tenant_admin_client, ...), which override
    get_current_user with a zero-argument callable — hence the TypeError
    fallback below, since the real function takes (request, token, db).
    """
    if not token:
        return None
    resolver = request.app.dependency_overrides.get(get_current_user, get_current_user)
    try:
        try:
            return await resolver(request=request, token=token, db=db)
        except TypeError:
            return await resolver()
    except AuthenticationError:
        return None


@router.get("/search", response_model=list[KBChunkSearchResult])
async def search_chunks(
    q: str = Query(min_length=1, max_length=500),
    space: str | None = Query(default=None, description="Space slug to restrict search"),
    limit: int = Query(default=5, ge=1, le=20),
    current_user: CurrentUser | None = Depends(_get_current_user_optional),
    db: AsyncSession = Depends(get_db),
) -> list[KBChunkSearchResult]:
    """Focused-snippet chunk search. Logged-in: tenant + global content.
    Anonymous: global public content only — no auth required."""
    repo = KBRepository(db)

    space_id = None
    if space is not None:
        scope_tenant = current_user.tenant_id if current_user is not None else None
        space_row = await repo.get_space_by_slug(scope_tenant, space)
        space_id = space_row.id if space_row is not None else None

    embedding = await _ai_service.embed(q)

    if current_user is None:
        results = await repo.search_chunks_global(embedding, space_id=space_id, limit=limit)
    else:
        viewer_role = "end_user"
        for role in ("admin", "manager", "team_lead", "agent"):
            if role in current_user.roles:
                viewer_role = role
                break
        results = await repo.search_chunks(
            tenant_id=current_user.tenant_id,
            query_embedding=embedding,
            space_id=space_id,
            limit=limit,
            viewer_role=viewer_role,
        )

    return [
        KBChunkSearchResult(
            article_id=chunk.article_id,
            article_title=chunk.article.title,
            heading=chunk.heading,
            content=chunk.content,
            score=score,
        )
        for chunk, score in results
    ]
