"""Knowledge Base API endpoints.

Route ordering note: static-segment routes (/search, /suggest) are registered
BEFORE parametric routes (/{id}) so FastAPI never captures literal segments
as UUID parameters.

Routers exported:
  router → /kb
"""

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentUser, get_current_user
from app.database import get_db
from app.exceptions import AIBudgetExhaustedError, AuthorizationError, ResourceNotFoundError, ValidationError
from app.models.kb import (
    KBArticle,
    KBArticleAccessLevel,
    KBArticleAttachment,
    KBArticleStatus,
    KBArticleTagAssignment,
    KBArticleVisibility,
    KBCategory,
    KBSpace,
    KBTag,
)
from app.repositories.kb_repo import KBRepository
from app.schemas.common import PaginatedResponse
from app.schemas.kb import (
    AssignKBTagRequest,
    CreateKBArticleAttachmentRequest,
    CreateKBArticleRequest,
    CreateKBCategoryRequest,
    CreateKBSpaceRequest,
    CreateKBTagRequest,
    KBArticleAttachmentResponse,
    KBArticleResponse,
    KBArticleSearchResult,
    KBArticleVersionResponse,
    KBAttachmentPresignResponse,
    KBCategoryResponse,
    KBFeedbackRequest,
    KBFeedbackResponse,
    KBSpaceResponse,
    KBTagResponse,
    LinkTicketToArticleRequest,
    TicketKBLinkResponse,
    UpdateKBArticleRequest,
    UpdateKBCategoryRequest,
    UpdateKBSpaceRequest,
    UpdateKBTagRequest,
)
from app.services.ai.ai_service import ai_service as _ai_service
from app.services.kb_service import KBService
from app.services.storage_service import get_storage_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/kb", tags=["knowledge-base"])

_service = KBService()

# Role sets
_AGENT_ROLES = {"agent", "team_lead", "manager", "admin"}
_TEAM_LEAD_ROLES = {"team_lead", "manager", "admin"}
_MANAGER_ROLES = {"manager", "admin"}
_ADMIN_ROLES = {"admin"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_tenant(current_user: CurrentUser) -> None:
    if current_user.tenant_id is None or current_user.local_user_id is None:
        raise AuthorizationError("Tenant context required")


def _resolve_list_scope(current_user: CurrentUser) -> UUID | None:
    """Tenant scope for list/read endpoints — see tickets._resolve_list_scope.

    Tenant users: their own tenant_id. Platform users: their selected tenant,
    or None for the cross-tenant "all tenants" view (repo omits the filter,
    returning every tenant's spaces/articles plus global ones).
    """
    if current_user.tenant_id is not None:
        return current_user.tenant_id
    if current_user.tier == "platform":
        return None
    raise AuthorizationError("Tenant context required")


def _has_role(current_user: CurrentUser, roles: set[str]) -> bool:
    return bool(set(current_user.roles) & roles)


def _require_any_role(
    current_user: CurrentUser, roles: set[str], action: str
) -> None:
    if not _has_role(current_user, roles):
        raise AuthorizationError(
            f"One of the following roles is required to {action}: "
            + ", ".join(sorted(roles))
        )


def _viewer_role(current_user: CurrentUser) -> str:
    """Return the highest-privilege KB-relevant role string for the user."""
    role_set = set(current_user.roles)
    for role in ("admin", "manager", "team_lead", "agent"):
        if role in role_set:
            return role
    return "end_user"


def _check_article_visibility(
    article: KBArticle, current_user: CurrentUser | None
) -> None:
    """Raise 403/404 if the caller may not read this article."""
    if current_user is None:
        # Anonymous access: only public articles in public spaces
        if article.visibility != KBArticleVisibility.public:
            raise ResourceNotFoundError("kb_article", str(article.id))
        return

    if article.visibility == KBArticleVisibility.agents_only:
        if not _has_role(current_user, _AGENT_ROLES):
            raise ResourceNotFoundError("kb_article", str(article.id))


def _resolve_article_scope(
    current_user: CurrentUser,
    access_level: KBArticleAccessLevel,
    product_id: UUID | None,
) -> tuple[UUID | None, UUID | None]:
    """Map an access level (+ product) to the article's (tenant_id, product_id).

    Authority rules:
      - global / product   → cross-tenant (tenant_id NULL); platform users only
      - tenant / tenant_product → scoped to the caller's tenant context

    product_id is required for the `product` and `tenant_product` levels and is
    cleared for `global` / `tenant`.
    """
    needs_product = access_level in (
        KBArticleAccessLevel.product,
        KBArticleAccessLevel.tenant_product,
    )
    if needs_product and product_id is None:
        raise ValidationError(
            f"product_id is required for the '{access_level.value}' access level"
        )

    if access_level in (KBArticleAccessLevel.global_, KBArticleAccessLevel.product):
        # Cross-tenant content — platform/super_admin only.
        if current_user.tier != "platform":
            raise AuthorizationError(
                "Only platform administrators can create or assign the "
                "'global' or 'product' (cross-tenant) access levels"
            )
        return None, (product_id if access_level == KBArticleAccessLevel.product else None)

    # tenant / tenant_product — require a tenant context
    if current_user.tenant_id is None:
        raise AuthorizationError(
            "A tenant context is required for tenant-scoped articles "
            "(platform users must select a tenant first)"
        )
    return current_user.tenant_id, (
        product_id if access_level == KBArticleAccessLevel.tenant_product else None
    )


def _require_write_authority(current_user: CurrentUser, article: KBArticle) -> None:
    """Block tenant users from mutating cross-tenant (global/product) articles."""
    if article.tenant_id is None and current_user.tier != "platform":
        raise AuthorizationError(
            "Only platform administrators can modify global or product-wide "
            "(cross-tenant) KB articles"
        )


def _require_role_or_platform(
    current_user: CurrentUser, roles: set[str], action: str
) -> None:
    """Role gate that always admits platform-tier users (full authority)."""
    if current_user.tier == "platform":
        return
    _require_tenant(current_user)
    _require_any_role(current_user, roles, action)


# ---------------------------------------------------------------------------
# ============================================================
# SPACES
# ============================================================
# ---------------------------------------------------------------------------


@router.get("/spaces", response_model=list[KBSpaceResponse])
async def list_spaces(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[KBSpaceResponse]:
    """List active spaces visible to the caller.

    Tenant users see their own tenant's spaces plus global spaces.
    Platform users with no tenant selected see every tenant's spaces plus
    global ones (cross-tenant view — KBSpaceResponse carries tenant_id so
    the UI can render a Tenant column / badge).
    """
    tenant_id = _resolve_list_scope(current_user)
    repo = KBRepository(db)
    spaces = await repo.list_spaces(tenant_id, active_only=True)
    return [KBSpaceResponse.model_validate(s) for s in spaces]


@router.post("/spaces", response_model=KBSpaceResponse, status_code=status.HTTP_201_CREATED)
async def create_space(
    body: CreateKBSpaceRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> KBSpaceResponse:
    """Create a new KB space (admin only).

    Spaces created via this route are always scoped to the caller's tenant.
    Global (tenant_id NULL) and cross-tenant spaces are seeded exclusively via
    KB import tooling, since they require a content-owning identity that
    platform-tier callers don't have (see create_article for the same
    constraint on author_id).
    """
    _require_tenant(current_user)
    _require_any_role(current_user, _ADMIN_ROLES, "create a KB space")

    # Check slug uniqueness within the tenant scope — the partial unique
    # indexes allow a tenant space and a global space to share a slug, so
    # only collide with an existing space owned by this same tenant.
    existing = await KBRepository(db).get_space_by_slug(current_user.tenant_id, body.slug)  # type: ignore[arg-type]
    if existing is not None and existing.tenant_id == current_user.tenant_id:
        raise ValidationError(f"A space with slug '{body.slug}' already exists")

    space = KBSpace(
        tenant_id=current_user.tenant_id,
        name=body.name,
        slug=body.slug,
        description=body.description,
        scope=body.scope,
        product_id=body.product_id,
        is_public=body.is_public,
    )
    db.add(space)
    await db.commit()
    await db.refresh(space)
    return KBSpaceResponse.model_validate(space)


@router.get("/spaces/{slug}", response_model=KBSpaceResponse)
async def get_space(
    slug: str,
    current_user: CurrentUser | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> KBSpaceResponse:
    """Get a space by slug.

    Public spaces are accessible without authentication.
    """
    if current_user is None:
        raise AuthorizationError("Authentication required to look up a space")
    tenant_id = _resolve_list_scope(current_user)

    space = await KBRepository(db).get_space_or_404(tenant_id, slug)

    if not space.is_public and current_user is None:
        raise AuthorizationError("This space requires authentication")

    return KBSpaceResponse.model_validate(space)


@router.patch("/spaces/{slug}", response_model=KBSpaceResponse)
async def update_space(
    slug: str,
    body: UpdateKBSpaceRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> KBSpaceResponse:
    """Update a space (admin only)."""
    _require_tenant(current_user)
    _require_any_role(current_user, _ADMIN_ROLES, "update a KB space")

    repo = KBRepository(db)
    space = await repo.get_space_or_404(current_user.tenant_id, slug)  # type: ignore[arg-type]

    if body.name is not None:
        space.name = body.name
    if body.description is not None:
        space.description = body.description
    if body.is_public is not None:
        space.is_public = body.is_public
    if body.is_active is not None:
        space.is_active = body.is_active
    if body.product_id is not None:
        space.product_id = body.product_id

    await db.commit()
    await db.refresh(space)
    return KBSpaceResponse.model_validate(space)


# ---------------------------------------------------------------------------
# ============================================================
# CATEGORIES  (within a space)
# ============================================================
# ---------------------------------------------------------------------------


async def _get_tenant_space_by_id(
    db: AsyncSession, space_id: UUID, tenant_id: UUID | None
) -> KBSpace:
    result = await db.execute(
        select(KBSpace).where(KBSpace.id == space_id, KBSpace.tenant_id == tenant_id)
    )
    space = result.scalar_one_or_none()
    if space is None:
        raise ResourceNotFoundError("kb_space", str(space_id))
    return space


async def _get_category_in_tenant(
    db: AsyncSession, category_id: UUID, tenant_id: UUID | None
) -> KBCategory:
    """Fetch a category, ensuring its parent space belongs to the tenant."""
    result = await db.execute(
        select(KBCategory)
        .join(KBSpace, KBSpace.id == KBCategory.space_id)
        .where(KBCategory.id == category_id, KBSpace.tenant_id == tenant_id)
    )
    category = result.scalar_one_or_none()
    if category is None:
        raise ResourceNotFoundError("kb_category", str(category_id))
    return category


@router.get("/spaces/{space_id}/categories", response_model=list[KBCategoryResponse])
async def list_categories(
    space_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    active_only: bool = Query(default=False),
) -> list[KBCategoryResponse]:
    """List categories within a space (hierarchical tree source)."""
    _require_tenant(current_user)
    await _get_tenant_space_by_id(db, space_id, current_user.tenant_id)

    q = select(KBCategory).where(KBCategory.space_id == space_id)
    if active_only:
        q = q.where(KBCategory.is_active.is_(True))
    q = q.order_by(KBCategory.display_order, KBCategory.name)
    result = await db.execute(q)
    return [KBCategoryResponse.model_validate(c) for c in result.scalars().all()]


@router.post(
    "/spaces/{space_id}/categories",
    response_model=KBCategoryResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_category(
    space_id: UUID,
    body: CreateKBCategoryRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> KBCategoryResponse:
    """Create a category within a space (manager+)."""
    _require_tenant(current_user)
    _require_any_role(current_user, _MANAGER_ROLES, "manage KB categories")
    await _get_tenant_space_by_id(db, space_id, current_user.tenant_id)

    if body.parent_id is not None:
        parent = await _get_category_in_tenant(db, body.parent_id, current_user.tenant_id)
        if parent.space_id != space_id:
            raise ValidationError("Parent category must belong to the same space")

    category = KBCategory(
        space_id=space_id,
        name=body.name,
        description=body.description,
        parent_id=body.parent_id,
        display_order=body.display_order,
    )
    db.add(category)
    await db.commit()
    await db.refresh(category)
    return KBCategoryResponse.model_validate(category)


@router.patch("/categories/{category_id}", response_model=KBCategoryResponse)
async def update_category(
    category_id: UUID,
    body: UpdateKBCategoryRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> KBCategoryResponse:
    """Update a category (manager+)."""
    _require_tenant(current_user)
    _require_any_role(current_user, _MANAGER_ROLES, "manage KB categories")

    category = await _get_category_in_tenant(db, category_id, current_user.tenant_id)

    updates = body.model_dump(exclude_unset=True)
    new_parent = updates.get("parent_id")
    if new_parent is not None:
        if new_parent == category_id:
            raise ValidationError("A category cannot be its own parent")
        parent = await _get_category_in_tenant(db, new_parent, current_user.tenant_id)
        if parent.space_id != category.space_id:
            raise ValidationError("Parent category must belong to the same space")

    for field_name, value in updates.items():
        setattr(category, field_name, value)

    await db.commit()
    await db.refresh(category)
    return KBCategoryResponse.model_validate(category)


@router.delete("/categories/{category_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_category(
    category_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a category (manager+). Articles in it are detached (category_id→NULL)."""
    _require_tenant(current_user)
    _require_any_role(current_user, _MANAGER_ROLES, "manage KB categories")

    category = await _get_category_in_tenant(db, category_id, current_user.tenant_id)
    await db.delete(category)
    await db.commit()


# ---------------------------------------------------------------------------
# ============================================================
# TAGS  (tenant-scoped)
# ============================================================
# ---------------------------------------------------------------------------


async def _get_tenant_tag(db: AsyncSession, tag_id: UUID, tenant_id: UUID | None) -> KBTag:
    result = await db.execute(
        select(KBTag).where(KBTag.id == tag_id, KBTag.tenant_id == tenant_id)
    )
    tag = result.scalar_one_or_none()
    if tag is None:
        raise ResourceNotFoundError("kb_tag", str(tag_id))
    return tag


@router.get("/tags", response_model=list[KBTagResponse])
async def list_tags(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[KBTagResponse]:
    """List all KB tags for the tenant."""
    _require_tenant(current_user)
    result = await db.execute(
        select(KBTag).where(KBTag.tenant_id == current_user.tenant_id).order_by(KBTag.name)
    )
    return [KBTagResponse.model_validate(t) for t in result.scalars().all()]


@router.post("/tags", response_model=KBTagResponse, status_code=status.HTTP_201_CREATED)
async def create_tag(
    body: CreateKBTagRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> KBTagResponse:
    """Create a KB tag (agent+)."""
    _require_tenant(current_user)
    _require_any_role(current_user, _AGENT_ROLES, "manage KB tags")

    existing = await db.execute(
        select(KBTag).where(
            KBTag.tenant_id == current_user.tenant_id, KBTag.name == body.name
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise ValidationError(f"A tag named '{body.name}' already exists")

    tag = KBTag(tenant_id=current_user.tenant_id, name=body.name)
    db.add(tag)
    await db.commit()
    await db.refresh(tag)
    return KBTagResponse.model_validate(tag)


@router.patch("/tags/{tag_id}", response_model=KBTagResponse)
async def update_tag(
    tag_id: UUID,
    body: UpdateKBTagRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> KBTagResponse:
    """Rename a KB tag (agent+)."""
    _require_tenant(current_user)
    _require_any_role(current_user, _AGENT_ROLES, "manage KB tags")

    tag = await _get_tenant_tag(db, tag_id, current_user.tenant_id)
    tag.name = body.name
    await db.commit()
    await db.refresh(tag)
    return KBTagResponse.model_validate(tag)


@router.delete("/tags/{tag_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_tag(
    tag_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a KB tag and all its assignments (agent+)."""
    _require_tenant(current_user)
    _require_any_role(current_user, _AGENT_ROLES, "manage KB tags")

    tag = await _get_tenant_tag(db, tag_id, current_user.tenant_id)
    await db.delete(tag)
    await db.commit()


# ---------------------------------------------------------------------------
# ============================================================
# ARTICLES — static routes first
# ============================================================
# ---------------------------------------------------------------------------


@router.get("/search", response_model=list[KBArticleSearchResult])
async def search_articles(
    q: str = Query(min_length=1, max_length=500),
    space: str | None = Query(default=None, description="Space slug to restrict search"),
    product: UUID | None = Query(default=None, description="Product ID to scope search to"),
    limit: int = Query(default=10, ge=1, le=50),
    current_user: CurrentUser | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[KBArticleSearchResult]:
    """Hybrid search across KB articles using pgvector cosine similarity + FTS.

    Falls back to FTS-only when the AI budget is exhausted.
    Authentication is required.
    """
    # Resolve tenant — require auth
    if current_user is not None and current_user.tenant_id is not None:
        tenant_id = current_user.tenant_id
        viewer_role = _viewer_role(current_user)
    else:
        raise AuthorizationError(
            "Authentication is required to search articles. "
            "Public search is available per-space via GET /kb/spaces/{slug}/articles."
        )

    space_id: UUID | None = None
    if space is not None:
        repo = KBRepository(db)
        kb_space = await repo.get_space_by_slug(tenant_id, space)  # type: ignore[arg-type]
        if kb_space is not None:
            space_id = kb_space.id

    # Attempt semantic embedding; degrade gracefully on budget exhaustion
    query_embedding: list[float] | None = None
    match_type = "fts"
    try:
        query_embedding = await _ai_service.embed(q)
        match_type = "hybrid"
    except AIBudgetExhaustedError:
        logger.warning(
            "kb_search_budget_exhausted_fts_fallback",
            extra={"tenant_id": str(tenant_id)},
        )
    except Exception:
        # Transient OpenAI failure — fall back to FTS silently
        logger.warning(
            "kb_search_embed_failed_fts_fallback",
            extra={"tenant_id": str(tenant_id)},
        )

    repo = KBRepository(db)
    results = await repo.search_hybrid(
        tenant_id=tenant_id,  # type: ignore[arg-type]
        query_text=q,
        query_embedding=query_embedding,
        space_id=space_id,
        limit=limit,
        viewer_role=viewer_role,
        product_id=product,
    )

    # Record token usage when embedding succeeded
    if query_embedding is not None:
        try:
            from app.redis_client import redis_client
            await _ai_service.record_usage(
                tenant_id=str(tenant_id),
                redis=redis_client,
                input_tokens=max(1, len(q) // 4),
                output_tokens=0,
                feature="kb_search",
                model="text-embedding-3-small",
            )
        except Exception:
            pass  # Usage recording is best-effort; never fail the request

    return [
        KBArticleSearchResult(
            article=KBArticleResponse.model_validate(article),
            score=score,
            match_type=match_type,
        )
        for article, score in results
    ]


@router.get("/suggest", response_model=list[KBArticleResponse])
async def suggest_articles(
    q: str = Query(min_length=1, max_length=500),
    product: UUID | None = Query(default=None, description="Product ID to scope suggestions to"),
    limit: int = Query(default=5, ge=1, le=20),
    current_user: CurrentUser | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[KBArticleResponse]:
    """Fast inline suggestion endpoint using vector similarity search.

    Falls back to FTS when the AI budget is exhausted or embedding fails.
    Used during ticket creation to surface relevant articles as the user types.
    Returns published articles visible to the caller.
    """
    if current_user is None or current_user.tenant_id is None:
        raise AuthorizationError("Authentication required for KB suggestions")

    viewer_role = _viewer_role(current_user)
    repo = KBRepository(db)

    # Attempt vector-based suggestion; fall back to FTS on any failure
    try:
        query_embedding = await _ai_service.embed(q)
        articles = await repo.suggest_by_vector(
            tenant_id=current_user.tenant_id,
            query_embedding=query_embedding,
            limit=limit,
            viewer_role=viewer_role,
            product_id=product,
        )
        if articles:
            return [KBArticleResponse.model_validate(a) for a in articles]
        # Empty vector results — fall through to FTS
    except AIBudgetExhaustedError:
        logger.warning(
            "kb_suggest_budget_exhausted_fts_fallback",
            extra={"tenant_id": str(current_user.tenant_id)},
        )
    except Exception:
        logger.warning(
            "kb_suggest_embed_failed_fts_fallback",
            extra={"tenant_id": str(current_user.tenant_id)},
        )

    # FTS fallback
    results = await repo.search_fts(
        tenant_id=current_user.tenant_id,
        query=q,
        limit=limit,
        viewer_role=viewer_role,
        product_id=product,
    )
    return [KBArticleResponse.model_validate(article) for article, _ in results]


@router.post(
    "/articles",
    response_model=KBArticleResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_article(
    body: CreateKBArticleRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> KBArticleResponse:
    """Create a KB article in draft state (agent+).

    The article's scope is set from `access_level` (see KBArticleAccessLevel):
      - tenant / tenant_product → scoped to the caller's tenant (default)
      - global / product        → cross-tenant; platform/super_admin only

    Platform users have no local users-row mirror, so cross-tenant articles
    they author carry a NULL author_id (kb_articles.author_id is nullable —
    migration 0016).
    """
    # Platform users author cross-tenant content; tenant users need an agent+
    # role. (_resolve_article_scope enforces the per-level authority below.)
    if current_user.tier != "platform":
        _require_tenant(current_user)
        _require_any_role(current_user, _AGENT_ROLES, "create a KB article")

    # Infer access level from product_id when the client omits it (legacy
    # clients sent only product_id to mean "tenant + product").
    access_level = body.access_level
    if access_level is None:
        access_level = (
            KBArticleAccessLevel.tenant_product
            if body.product_id is not None
            else KBArticleAccessLevel.tenant
        )

    tenant_id, product_id = _resolve_article_scope(
        current_user, access_level, body.product_id
    )

    # Space must be compatible with the article's tenant scope — global spaces
    # (tenant_id NULL) are usable by any scope; a tenant space only matches its
    # own tenant's articles.
    space_result = await db.execute(
        select(KBSpace).where(
            KBSpace.id == body.space_id,
            sa.or_(KBSpace.tenant_id == tenant_id, KBSpace.tenant_id.is_(None)),
        )
    )
    space = space_result.scalar_one_or_none()
    if space is None:
        raise ResourceNotFoundError("kb_space", str(body.space_id))

    slug = await _service.make_unique_slug(
        body.title,
        body.space_id,
        tenant_id,
        db,
    )

    article = KBArticle(
        tenant_id=tenant_id,
        product_id=product_id,
        space_id=body.space_id,
        category_id=body.category_id,
        title=body.title,
        slug=slug,
        body=body.body,
        excerpt=body.excerpt,
        visibility=body.visibility,
        author_id=current_user.local_user_id,
        status=KBArticleStatus.draft,
    )
    db.add(article)
    await db.commit()
    await db.refresh(article)
    return KBArticleResponse.model_validate(article)


@router.get(
    "/articles/{article_id}",
    response_model=KBArticleResponse,
)
async def get_article(
    article_id: UUID,
    current_user: CurrentUser | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> KBArticleResponse:
    """Get an article by ID and increment view count.

    Visibility rules:
      - public articles in public spaces: accessible without login
      - internal: requires authentication
      - agents_only: requires agent+ role
    """
    if current_user is None:
        raise AuthorizationError("Authentication required")
    tenant_id = _resolve_list_scope(current_user)

    repo = KBRepository(db)
    article = await repo.get_article_or_404(article_id, tenant_id)
    _check_article_visibility(article, current_user)

    await repo.increment_view_count(article_id)
    return KBArticleResponse.model_validate(article)


@router.patch("/articles/{article_id}", response_model=KBArticleResponse)
async def update_article(
    article_id: UUID,
    body: UpdateKBArticleRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> KBArticleResponse:
    """Update article body/title/visibility/category and (optionally) scope.

    Allowed for: the article's author, or any agent with admin/manager role.
    Changing scope to/within the cross-tenant levels is platform-admin only
    (see _resolve_article_scope / _require_write_authority).
    """
    if current_user.tier != "platform":
        _require_tenant(current_user)

    repo = KBRepository(db)
    article = await repo.get_article_or_404(article_id, current_user.tenant_id)  # type: ignore[arg-type]

    # Tenant users may not touch global/product (cross-tenant) articles.
    _require_write_authority(current_user, article)

    is_author = article.author_id == current_user.local_user_id
    is_privileged = _has_role(current_user, _MANAGER_ROLES)

    if not (is_author or is_privileged):
        _require_any_role(current_user, _AGENT_ROLES, "edit a KB article")

    if body.title is not None:
        new_slug = await _service.make_unique_slug(
            body.title,
            article.space_id,
            article.tenant_id,
            db,
            exclude_article_id=article.id,
        )
        article.title = body.title
        article.slug = new_slug

    if body.body is not None:
        article.body = body.body

    if body.category_id is not None:
        article.category_id = body.category_id

    if body.visibility is not None:
        article.visibility = body.visibility

    if body.excerpt is not None:
        article.excerpt = body.excerpt

    # Scope change. When access_level is given, recompute (tenant_id, product_id)
    # with authority enforced; product_id is read against the new level. When
    # only product_id is given (no access_level), preserve the existing tenant
    # scope and just (re)assign the product within it.
    if body.access_level is not None:
        new_product = body.product_id if body.product_id is not None else article.product_id
        new_tenant_id, new_product_id = _resolve_article_scope(
            current_user, body.access_level, new_product
        )
        article.tenant_id = new_tenant_id
        article.product_id = new_product_id
    elif body.product_id is not None:
        article.product_id = body.product_id

    article.last_edited_by = current_user.local_user_id  # type: ignore[assignment]

    await db.commit()
    await db.refresh(article)
    return KBArticleResponse.model_validate(article)


@router.delete("/articles/{article_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_article(
    article_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Soft-delete by archiving (manager/admin only)."""
    _require_role_or_platform(current_user, _MANAGER_ROLES, "archive/delete a KB article")

    repo = KBRepository(db)
    article = await repo.get_article_or_404(article_id, current_user.tenant_id)  # type: ignore[arg-type]
    _require_write_authority(current_user, article)
    await _service.archive_article(article, db)
    await db.commit()


@router.post("/articles/{article_id}/publish", response_model=KBArticleResponse)
async def publish_article(
    article_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> KBArticleResponse:
    """Publish an article (team_lead+)."""
    _require_role_or_platform(current_user, _TEAM_LEAD_ROLES, "publish a KB article")

    repo = KBRepository(db)
    article = await repo.get_article_or_404(article_id, current_user.tenant_id)  # type: ignore[arg-type]
    _require_write_authority(current_user, article)

    updated = await _service.publish_article(
        article,
        actor_id=current_user.local_user_id,  # type: ignore[arg-type]
        actor_roles=current_user.roles,
        db=db,
    )
    await db.commit()
    return KBArticleResponse.model_validate(updated)


@router.post("/articles/{article_id}/archive", response_model=KBArticleResponse)
async def archive_article(
    article_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> KBArticleResponse:
    """Archive an article (team_lead+)."""
    _require_role_or_platform(current_user, _TEAM_LEAD_ROLES, "archive a KB article")

    repo = KBRepository(db)
    article = await repo.get_article_or_404(article_id, current_user.tenant_id)  # type: ignore[arg-type]
    _require_write_authority(current_user, article)
    updated = await _service.archive_article(article, db)
    await db.commit()
    return KBArticleResponse.model_validate(updated)


@router.post("/articles/{article_id}/submit", response_model=KBArticleResponse)
async def submit_for_review(
    article_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> KBArticleResponse:
    """Submit a draft article for review (author, any agent+)."""
    _require_role_or_platform(current_user, _AGENT_ROLES, "submit an article for review")

    repo = KBRepository(db)
    article = await repo.get_article_or_404(article_id, current_user.tenant_id)  # type: ignore[arg-type]
    _require_write_authority(current_user, article)

    # Only the author (or a privileged role) can submit
    is_author = article.author_id == current_user.local_user_id
    if not (is_author or _has_role(current_user, _MANAGER_ROLES)):
        raise AuthorizationError("Only the article author may submit it for review")

    updated = await _service.submit_for_review(
        article, current_user.local_user_id, db  # type: ignore[arg-type]
    )
    await db.commit()
    return KBArticleResponse.model_validate(updated)


@router.get(
    "/articles/{article_id}/versions",
    response_model=list[KBArticleVersionResponse],
)
async def list_versions(
    article_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[KBArticleVersionResponse]:
    """List version history of an article (agent+)."""
    _require_tenant(current_user)
    _require_any_role(current_user, _AGENT_ROLES, "view article version history")

    repo = KBRepository(db)
    # Verify article exists in tenant
    await repo.get_article_or_404(article_id, current_user.tenant_id)  # type: ignore[arg-type]
    versions = await repo.list_versions(article_id)
    return [KBArticleVersionResponse.model_validate(v) for v in versions]


@router.post(
    "/articles/{article_id}/versions/{v_num}/restore",
    response_model=KBArticleResponse,
)
async def restore_version(
    article_id: UUID,
    v_num: int,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> KBArticleResponse:
    """Restore an article to a previous version (admin only)."""
    _require_role_or_platform(current_user, _ADMIN_ROLES, "restore an article version")

    repo = KBRepository(db)
    article = await repo.get_article_or_404(article_id, current_user.tenant_id)  # type: ignore[arg-type]
    _require_write_authority(current_user, article)
    version = await repo.get_version(article_id, v_num)
    if version is None:
        raise ResourceNotFoundError("kb_article_version", f"{article_id}@v{v_num}")

    updated = await _service.restore_version(
        article, version, current_user.local_user_id, db  # type: ignore[arg-type]
    )
    await db.commit()
    return KBArticleResponse.model_validate(updated)


@router.post(
    "/articles/{article_id}/feedback",
    response_model=KBFeedbackResponse,
    status_code=status.HTTP_201_CREATED,
)
async def submit_feedback(
    article_id: UUID,
    body: KBFeedbackRequest,
    current_user: CurrentUser | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> KBFeedbackResponse:
    """Submit helpful/not-helpful feedback.

    Anonymous feedback is accepted on public articles (no auth token required).
    """
    user_id: UUID | None = None

    if current_user is not None and current_user.local_user_id is not None:
        user_id = current_user.local_user_id
        tenant_id = current_user.tenant_id
    else:
        # Anonymous — must look up article a different way
        # Require at least the article_id to exist and be public
        result = await db.execute(
            select(KBArticle).where(
                KBArticle.id == article_id,
                KBArticle.status == KBArticleStatus.published,
                KBArticle.visibility == KBArticleVisibility.public,
            )
        )
        anon_article = result.scalar_one_or_none()
        if anon_article is None:
            raise AuthorizationError(
                "Authentication required to submit feedback on this article"
            )
        tenant_id = anon_article.tenant_id

    repo = KBRepository(db)
    article = await repo.get_article_or_404(article_id, tenant_id)  # type: ignore[arg-type]

    if article.status != KBArticleStatus.published:
        raise ValidationError("Feedback can only be submitted on published articles")

    feedback = await repo.add_feedback(
        article_id=article_id,
        is_helpful=body.is_helpful,
        comment=body.comment,
        user_id=user_id,
    )
    await db.commit()
    return KBFeedbackResponse.model_validate(feedback)


@router.get(
    "/articles/{article_id}/feedback",
    response_model=list[KBFeedbackResponse],
)
async def list_feedback(
    article_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    helpful: bool | None = Query(default=None),
) -> list[KBFeedbackResponse]:
    """List feedback submitted on an article (agent+ — for review)."""
    _require_tenant(current_user)
    _require_any_role(current_user, _AGENT_ROLES, "view article feedback")

    repo = KBRepository(db)
    await repo.get_article_or_404(article_id, current_user.tenant_id)  # type: ignore[arg-type]

    from app.models.kb import KBArticleFeedback

    q = select(KBArticleFeedback).where(KBArticleFeedback.article_id == article_id)
    if helpful is not None:
        q = q.where(KBArticleFeedback.is_helpful.is_(helpful))
    q = q.order_by(KBArticleFeedback.created_at.desc())

    result = await db.execute(q)
    return [KBFeedbackResponse.model_validate(f) for f in result.scalars().all()]


# ---------------------------------------------------------------------------
# Ticket-KB links
# ---------------------------------------------------------------------------


@router.post(
    "/articles/{article_id}/link-ticket",
    response_model=TicketKBLinkResponse,
    status_code=status.HTTP_201_CREATED,
)
async def link_ticket(
    article_id: UUID,
    body: LinkTicketToArticleRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TicketKBLinkResponse:
    """Link a ticket to a KB article (agent+)."""
    _require_tenant(current_user)
    _require_any_role(current_user, _AGENT_ROLES, "link a ticket to a KB article")

    repo = KBRepository(db)
    await repo.get_article_or_404(article_id, current_user.tenant_id)  # type: ignore[arg-type]

    link = await repo.link_ticket(
        article_id=article_id,
        ticket_id=body.ticket_id,
        linked_by=current_user.local_user_id,
        is_suggested=False,
    )
    await db.commit()
    return TicketKBLinkResponse.model_validate(link)


@router.delete(
    "/articles/{article_id}/link-ticket/{ticket_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def unlink_ticket(
    article_id: UUID,
    ticket_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Remove a ticket-KB article link (agent+)."""
    _require_tenant(current_user)
    _require_any_role(current_user, _AGENT_ROLES, "unlink a ticket from a KB article")

    repo = KBRepository(db)
    await repo.get_article_or_404(article_id, current_user.tenant_id)  # type: ignore[arg-type]
    removed = await repo.unlink_ticket(article_id, ticket_id)
    if not removed:
        raise ResourceNotFoundError("ticket_kb_link", f"{ticket_id}:{article_id}")
    await db.commit()


# ---------------------------------------------------------------------------
# Space — article list
# ---------------------------------------------------------------------------


@router.get(
    "/spaces/{slug}/articles",
    response_model=PaginatedResponse[KBArticleResponse],
)
async def list_space_articles(
    slug: str,
    status_filter: KBArticleStatus | None = Query(default=None, alias="status"),
    visibility_filter: KBArticleVisibility | None = Query(
        default=None, alias="visibility"
    ),
    category_id: UUID | None = Query(default=None),
    product: UUID | None = Query(default=None, description="Product ID to scope articles to"),
    cursor: str | None = Query(default=None),
    page: int | None = Query(default=None, ge=1, description="1-based page (offset pagination); when set, response carries a total count"),
    limit: int = Query(default=25, ge=1, le=100),
    current_user: CurrentUser | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PaginatedResponse[KBArticleResponse]:
    """List articles within a space, with visibility filtering.

    Pagination: pass `page` (1-based) for standard page-numbered pagination with
    a `total` count; otherwise the keyset `cursor` is used (returns next_cursor).

    Public spaces: no authentication required (end_user visibility rules apply).
    Private spaces: authentication required.
    """
    if current_user is None:
        raise AuthorizationError("Authentication required to list articles")
    tenant_id = _resolve_list_scope(current_user)
    viewer_role = _viewer_role(current_user)

    repo = KBRepository(db)
    space = await repo.get_space_or_404(tenant_id, slug)

    # Non-public space requires authentication
    if not space.is_public and current_user is None:
        raise AuthorizationError("This space requires authentication")

    offset = (page - 1) * limit if page is not None else None
    articles, next_cursor, total = await repo.list_articles(
        tenant_id=tenant_id,  # type: ignore[arg-type]
        space_id=space.id,
        category_id=category_id,
        status=status_filter,
        visibility=visibility_filter,
        cursor=cursor,
        limit=limit,
        viewer_role=viewer_role,
        product_id=product,
        offset=offset,
    )

    return PaginatedResponse(
        items=[KBArticleResponse.model_validate(a) for a in articles],
        next_cursor=next_cursor,
        total=total,
    )


# ---------------------------------------------------------------------------
# ============================================================
# ARTICLE TAGS  (assign / unassign / list)
# ============================================================
# ---------------------------------------------------------------------------


@router.get("/articles/{article_id}/tags", response_model=list[KBTagResponse])
async def list_article_tags(
    article_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[KBTagResponse]:
    """List tags assigned to an article."""
    _require_tenant(current_user)
    repo = KBRepository(db)
    await repo.get_article_or_404(article_id, current_user.tenant_id)  # type: ignore[arg-type]

    result = await db.execute(
        select(KBTag)
        .join(KBArticleTagAssignment, KBArticleTagAssignment.tag_id == KBTag.id)
        .where(KBArticleTagAssignment.article_id == article_id)
        .order_by(KBTag.name)
    )
    return [KBTagResponse.model_validate(t) for t in result.scalars().all()]


@router.post(
    "/articles/{article_id}/tags",
    response_model=list[KBTagResponse],
    status_code=status.HTTP_201_CREATED,
)
async def assign_article_tag(
    article_id: UUID,
    body: AssignKBTagRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[KBTagResponse]:
    """Assign a tag to an article (agent+). Idempotent."""
    _require_tenant(current_user)
    _require_any_role(current_user, _AGENT_ROLES, "assign KB tags")

    repo = KBRepository(db)
    await repo.get_article_or_404(article_id, current_user.tenant_id)  # type: ignore[arg-type]
    await _get_tenant_tag(db, body.tag_id, current_user.tenant_id)

    existing = await db.execute(
        select(KBArticleTagAssignment).where(
            KBArticleTagAssignment.article_id == article_id,
            KBArticleTagAssignment.tag_id == body.tag_id,
        )
    )
    if existing.scalar_one_or_none() is None:
        db.add(KBArticleTagAssignment(article_id=article_id, tag_id=body.tag_id))
        await db.commit()

    result = await db.execute(
        select(KBTag)
        .join(KBArticleTagAssignment, KBArticleTagAssignment.tag_id == KBTag.id)
        .where(KBArticleTagAssignment.article_id == article_id)
        .order_by(KBTag.name)
    )
    return [KBTagResponse.model_validate(t) for t in result.scalars().all()]


@router.delete(
    "/articles/{article_id}/tags/{tag_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def unassign_article_tag(
    article_id: UUID,
    tag_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Remove a tag from an article (agent+)."""
    _require_tenant(current_user)
    _require_any_role(current_user, _AGENT_ROLES, "assign KB tags")

    repo = KBRepository(db)
    await repo.get_article_or_404(article_id, current_user.tenant_id)  # type: ignore[arg-type]

    result = await db.execute(
        select(KBArticleTagAssignment).where(
            KBArticleTagAssignment.article_id == article_id,
            KBArticleTagAssignment.tag_id == tag_id,
        )
    )
    assignment = result.scalar_one_or_none()
    if assignment is None:
        raise ResourceNotFoundError("kb_article_tag_assignment", str(tag_id))
    await db.delete(assignment)
    await db.commit()


# ---------------------------------------------------------------------------
# ============================================================
# ARTICLE ATTACHMENTS  (presigned upload / list / delete)
# ============================================================
# ---------------------------------------------------------------------------


@router.post(
    "/articles/{article_id}/attachments",
    response_model=KBAttachmentPresignResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_article_attachment(
    article_id: UUID,
    body: CreateKBArticleAttachmentRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> KBAttachmentPresignResponse:
    """Generate a presigned upload URL for a KB article attachment (agent+)."""
    _require_tenant(current_user)
    _require_any_role(current_user, _AGENT_ROLES, "upload KB attachments")

    repo = KBRepository(db)
    await repo.get_article_or_404(article_id, current_user.tenant_id)  # type: ignore[arg-type]

    from uuid_extensions import uuid7

    storage = get_storage_service()
    upload_url, storage_key = await storage.presigned_upload_url(
        tenant_id=str(current_user.tenant_id),
        filename=body.filename,
        mime_type=body.mime_type,
        max_bytes=body.file_size,
    )

    attachment_id = uuid7()
    att = KBArticleAttachment(
        id=attachment_id,
        article_id=article_id,
        uploaded_by=current_user.local_user_id,
        filename=body.filename,
        storage_url=storage_key,
        file_size=body.file_size,
        mime_type=body.mime_type,
    )
    db.add(att)
    await db.commit()

    return KBAttachmentPresignResponse(
        upload_url=upload_url, attachment_id=attachment_id, expires_in=3600
    )


@router.get(
    "/articles/{article_id}/attachments",
    response_model=list[KBArticleAttachmentResponse],
)
async def list_article_attachments(
    article_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[KBArticleAttachmentResponse]:
    """List attachments for a KB article."""
    _require_tenant(current_user)
    repo = KBRepository(db)
    await repo.get_article_or_404(article_id, current_user.tenant_id)  # type: ignore[arg-type]

    result = await db.execute(
        select(KBArticleAttachment)
        .where(KBArticleAttachment.article_id == article_id)
        .order_by(KBArticleAttachment.created_at.desc())
    )
    return [KBArticleAttachmentResponse.model_validate(r) for r in result.scalars().all()]


@router.delete(
    "/articles/{article_id}/attachments/{attachment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_article_attachment(
    article_id: UUID,
    attachment_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a KB article attachment (agent+)."""
    _require_tenant(current_user)
    _require_any_role(current_user, _AGENT_ROLES, "delete KB attachments")

    repo = KBRepository(db)
    await repo.get_article_or_404(article_id, current_user.tenant_id)  # type: ignore[arg-type]

    result = await db.execute(
        select(KBArticleAttachment).where(
            KBArticleAttachment.id == attachment_id,
            KBArticleAttachment.article_id == article_id,
        )
    )
    att = result.scalar_one_or_none()
    if att is None:
        raise ResourceNotFoundError("kb_article_attachments", str(attachment_id))
    await db.delete(att)
    await db.commit()
