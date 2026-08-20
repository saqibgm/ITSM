"""Repository for Knowledge Base entities."""

import base64
import json
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import ResourceNotFoundError
from app.models.kb import (
    KBArticle,
    KBArticleFeedback,
    KBArticleStatus,
    KBArticleVersion,
    KBArticleVisibility,
    KBChunk,
    KBSpace,
    TicketKBLink,
)
from app.repositories.base import BaseRepository

# Roles that may access agents_only articles
_AGENT_ROLES = {"agent", "team_lead", "manager", "admin"}


class KBRepository(BaseRepository[KBArticle]):
    model = KBArticle

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    # ------------------------------------------------------------------
    # Scoping helper — matches global / tenant / product / tenant+product
    # ------------------------------------------------------------------

    @staticmethod
    def _scope_clause(tenant_id: UUID | None, product_id: UUID | None = None):
        """Build a predicate matching articles visible in this scope context.

        An article matches when its tenant_id is either NULL (global-for-tenant)
        or equal to the caller's tenant, AND its product_id is either NULL
        (global-for-product) or equal to the caller's product (when given).
        Callers without a product context only see non-product-scoped articles.

        tenant_id=None means "no tenant filter" — the platform cross-tenant
        "all tenants" view, which omits the tenant clause entirely so every
        tenant's articles (plus global ones) are returned.
        """
        clauses = []
        if tenant_id is not None:
            clauses.append(sa.or_(KBArticle.tenant_id.is_(None), KBArticle.tenant_id == tenant_id))
        if product_id is not None:
            clauses.append(
                sa.or_(KBArticle.product_id.is_(None), KBArticle.product_id == product_id)
            )
        else:
            clauses.append(KBArticle.product_id.is_(None))
        return sa.and_(*clauses) if clauses else sa.true()

    @staticmethod
    def _accessible_clause(tenant_id: UUID | None):
        """Tenant-only access predicate for direct ID/slug lookups.

        Product scope must NOT gate direct access by ID/slug — if the caller
        already has the identifier and the article belongs to their tenant
        (or is global), they may fetch it regardless of its product_id.
        Used by get_article_or_404 / get_by_slug; _scope_clause (tenant+product)
        is reserved for list/search where leaking unrelated product content
        into a listing would be undesirable.

        tenant_id=None means "no tenant filter" — the platform cross-tenant
        view, where any tenant's article (or a global one) is accessible.
        """
        if tenant_id is None:
            return sa.true()
        return sa.or_(KBArticle.tenant_id.is_(None), KBArticle.tenant_id == tenant_id)

    @staticmethod
    def _scope_sql(product_id: UUID | None) -> str:
        """Raw-SQL scope fragment for hybrid/vector queries (uses :tenant_id / :product_id)."""
        if product_id is not None:
            return (
                "(a.tenant_id IS NULL OR a.tenant_id = :tenant_id) "
                "AND (a.product_id IS NULL OR a.product_id = :product_id)"
            )
        return "(a.tenant_id IS NULL OR a.tenant_id = :tenant_id) AND a.product_id IS NULL"

    # ------------------------------------------------------------------
    # get override — scope aware (global / tenant / product)
    # ------------------------------------------------------------------

    async def get_article_or_404(self, article_id: UUID, tenant_id: UUID | None) -> KBArticle:
        """Return the article or raise ResourceNotFoundError.

        Direct ID lookup — accessible if the article is global or owned by the
        caller's tenant. Product scope does not gate direct access by ID; once
        the caller has the identifier, product_id only matters for discovery
        (listing/search), not for fetching a known article (see _accessible_clause).
        """
        result = await self.session.execute(
            select(KBArticle).where(
                KBArticle.id == article_id,
                self._accessible_clause(tenant_id),
            )
        )
        article = result.scalar_one_or_none()
        if article is None:
            raise ResourceNotFoundError("kb_article", str(article_id))
        return article

    # ------------------------------------------------------------------
    # List articles with visibility filtering and cursor pagination
    # ------------------------------------------------------------------

    async def list_articles(
        self,
        tenant_id: UUID | None,
        space_id: UUID | None = None,
        category_id: UUID | None = None,
        status: KBArticleStatus | None = None,
        visibility: KBArticleVisibility | None = None,
        cursor: str | None = None,
        limit: int = 25,
        viewer_role: str = "end_user",
        product_id: UUID | None = None,
        offset: int | None = None,
    ) -> tuple[list[KBArticle], str | None, int | None]:
        """Return a page of articles with visibility rules enforced at query level.

        Two pagination modes:
          - offset is not None → page-numbered: applies LIMIT/OFFSET and returns
            a total count (next_cursor is None).
          - offset is None     → keyset/cursor: returns next_cursor (total None).

        Visibility rules:
          - agents_only → only returned for agent/team_lead/manager/admin
          - public + internal → returned for all authenticated roles

        Scope rules: includes global and product-shared articles in addition
        to ones explicitly owned by the caller's tenant/product (see _scope_clause).

        Returns (rows, next_cursor, total).
        """
        # Collect WHERE conditions once so they can be reused by the count query.
        conds = [self._scope_clause(tenant_id, product_id)]

        if viewer_role not in _AGENT_ROLES:
            conds.append(
                KBArticle.visibility.in_(
                    [KBArticleVisibility.public, KBArticleVisibility.internal]
                )
            )
        if space_id is not None:
            conds.append(KBArticle.space_id == space_id)
        if category_id is not None:
            conds.append(KBArticle.category_id == category_id)
        if status is not None:
            conds.append(KBArticle.status == status)
        if visibility is not None:
            # Intersect an explicit visibility request with the permitted set
            # (prevent end_user from requesting agents_only).
            if viewer_role not in _AGENT_ROLES and visibility == KBArticleVisibility.agents_only:
                return [], None, (0 if offset is not None else None)
            conds.append(KBArticle.visibility == visibility)

        order = (KBArticle.created_at.desc(), KBArticle.id.desc())

        # ---- Offset / page-numbered mode ------------------------------------
        if offset is not None:
            total = await self.session.scalar(
                select(func.count()).select_from(KBArticle).where(*conds)
            )
            stmt = (
                select(KBArticle).where(*conds).order_by(*order)
                .offset(max(0, offset)).limit(limit)
            )
            rows = list((await self.session.execute(stmt)).scalars().all())
            return rows, None, int(total or 0)

        # ---- Cursor / keyset mode (default) ---------------------------------
        if cursor is not None:
            try:
                from datetime import datetime
                decoded = json.loads(base64.b64decode(cursor).decode())
                # Parse to native types — the columns are timestamptz / uuid, so
                # comparing against raw strings errors in PostgreSQL.
                cursor_created_at = datetime.fromisoformat(decoded["created_at"])
                cursor_id = UUID(decoded["id"])
                conds.append(
                    sa.or_(
                        KBArticle.created_at < cursor_created_at,
                        sa.and_(
                            KBArticle.created_at == cursor_created_at,
                            KBArticle.id < cursor_id,
                        ),
                    )
                )
            except Exception:
                pass  # Malformed cursor — ignore and return from beginning

        stmt = select(KBArticle).where(*conds).order_by(*order).limit(limit + 1)
        rows = list((await self.session.execute(stmt)).scalars().all())

        next_cursor: str | None = None
        if len(rows) > limit:
            rows = rows[:limit]
            last = rows[-1]
            next_cursor = base64.b64encode(
                json.dumps(
                    {"created_at": last.created_at.isoformat(), "id": str(last.id)}
                ).encode()
            ).decode()

        return rows, next_cursor, None

    # ------------------------------------------------------------------
    # Full-text search
    # ------------------------------------------------------------------

    async def search_fts(
        self,
        tenant_id: UUID,
        query: str,
        space_id: UUID | None = None,
        limit: int = 10,
        viewer_role: str = "end_user",
        product_id: UUID | None = None,
    ) -> list[tuple[KBArticle, float]]:
        """PostgreSQL FTS using plainto_tsquery.

        Returns list of (KBArticle, ts_rank) ordered by rank DESC.
        Only published articles visible to the caller are returned, including
        global and product-shared articles in scope (see _scope_clause).
        """
        tsquery = func.plainto_tsquery("english", query)
        rank_expr = func.ts_rank(KBArticle.search_vector, tsquery).label("rank")

        stmt = (
            select(KBArticle, rank_expr)
            .where(
                self._scope_clause(tenant_id, product_id),
                KBArticle.status == KBArticleStatus.published,
                KBArticle.search_vector.op("@@")(tsquery),
            )
            .order_by(rank_expr.desc())
            .limit(limit)
        )

        # Visibility enforcement
        if viewer_role not in _AGENT_ROLES:
            stmt = stmt.where(
                KBArticle.visibility.in_(
                    [KBArticleVisibility.public, KBArticleVisibility.internal]
                )
            )

        if space_id is not None:
            stmt = stmt.where(KBArticle.space_id == space_id)

        result = await self.session.execute(stmt)
        return [(row[0], float(row[1])) for row in result.all()]

    # ------------------------------------------------------------------
    # Hybrid search (FTS + pgvector cosine similarity)
    # ------------------------------------------------------------------

    async def search_hybrid(
        self,
        tenant_id: UUID,
        query_text: str,
        query_embedding: list[float] | None,
        space_id: UUID | None = None,
        limit: int = 20,
        offset: int = 0,
        viewer_role: str = "end_user",
        product_id: UUID | None = None,
    ) -> list[tuple[KBArticle, float]]:
        """Hybrid search combining FTS (0.4 weight) and cosine similarity (0.6 weight).

        Falls back to FTS-only ranking when query_embedding is None (budget
        exhausted or embedder unavailable).

        Returns list of (KBArticle, combined_score) ordered by score DESC.
        Only published articles visible to the caller are returned, including
        global and product-shared articles in scope (see _scope_clause/_scope_sql).
        All SQL parameters use :named bindings — no f-strings or % in SQL.
        """
        if query_embedding is not None:
            # Build the visibility IN-list as a literal for safe construction
            if viewer_role not in _AGENT_ROLES:
                visibility_filter = (
                    "AND a.visibility IN ('public', 'internal')"
                )
            else:
                visibility_filter = ""

            space_filter = "AND a.space_id = :space_id" if space_id is not None else ""
            scope_filter = self._scope_sql(product_id)

            sql = text(
                f"""
                SELECT
                    a.id,
                    (
                        0.4 * COALESCE(ts_rank(a.search_vector, plainto_tsquery('english', :query)), 0)
                        + 0.6 * (1.0 - (a.embedding <=> CAST(:embedding AS vector)))
                    ) AS combined_score
                FROM kb_articles a
                WHERE
                    {scope_filter}
                    AND a.status = 'published'
                    AND a.embedding IS NOT NULL
                    {visibility_filter}
                    {space_filter}
                ORDER BY combined_score DESC
                LIMIT :lim OFFSET :off
                """  # noqa: S608 — visibility_filter/scope_filter are controlled literals, not user input
            )
            params: dict = {
                "tenant_id": tenant_id,
                "query": query_text,
                "embedding": str(query_embedding),
                "lim": limit,
                "off": offset,
            }
            if product_id is not None:
                params["product_id"] = product_id
            if space_id is not None:
                params["space_id"] = space_id

            result = await self.session.execute(sql, params)
            rows = result.all()

            if not rows:
                # No articles with embeddings found — fall back to FTS
                return await self.search_fts(
                    tenant_id=tenant_id,
                    query=query_text,
                    space_id=space_id,
                    limit=limit,
                    viewer_role=viewer_role,
                    product_id=product_id,
                )

            # Load ORM objects for the returned IDs
            article_ids = [row[0] for row in rows]
            score_by_id = {row[0]: float(row[1]) for row in rows}

            stmt = (
                sa.select(KBArticle)
                .where(KBArticle.id.in_(article_ids))
            )
            articles_result = await self.session.execute(stmt)
            articles_by_id = {a.id: a for a in articles_result.scalars().all()}

            # Return in original score order
            return [
                (articles_by_id[aid], score_by_id[aid])
                for aid in article_ids
                if aid in articles_by_id
            ]

        # Fallback: FTS only (embedding unavailable)
        return await self.search_fts(
            tenant_id=tenant_id,
            query=query_text,
            space_id=space_id,
            limit=limit,
            viewer_role=viewer_role,
            product_id=product_id,
        )

    # ------------------------------------------------------------------
    # Vector-only suggest
    # ------------------------------------------------------------------

    async def suggest_by_vector(
        self,
        tenant_id: UUID,
        query_embedding: list[float],
        space_id: UUID | None = None,
        limit: int = 5,
        viewer_role: str = "end_user",
        product_id: UUID | None = None,
    ) -> list[KBArticle]:
        """Return articles nearest to query_embedding by cosine distance.

        Only published articles visible to the caller are returned, including
        global and product-shared articles in scope (see _scope_sql).
        All SQL parameters use :named bindings — no f-strings or % in SQL.
        """
        if viewer_role not in _AGENT_ROLES:
            visibility_filter = "AND a.visibility IN ('public', 'internal')"
        else:
            visibility_filter = ""

        space_filter = "AND a.space_id = :space_id" if space_id is not None else ""
        scope_filter = self._scope_sql(product_id)

        sql = text(
            f"""
            SELECT a.id
            FROM kb_articles a
            WHERE
                {scope_filter}
                AND a.status = 'published'
                AND a.embedding IS NOT NULL
                {visibility_filter}
                {space_filter}
            ORDER BY a.embedding <=> CAST(:embedding AS vector)
            LIMIT :lim
            """  # noqa: S608 — visibility_filter/scope_filter are controlled literals, not user input
        )
        params: dict = {
            "tenant_id": tenant_id,
            "embedding": str(query_embedding),
            "lim": limit,
        }
        if product_id is not None:
            params["product_id"] = product_id
        if space_id is not None:
            params["space_id"] = space_id

        result = await self.session.execute(sql, params)
        article_ids = [row[0] for row in result.all()]

        if not article_ids:
            return []

        stmt = sa.select(KBArticle).where(KBArticle.id.in_(article_ids))
        articles_result = await self.session.execute(stmt)
        articles_by_id = {a.id: a for a in articles_result.scalars().all()}

        # Preserve cosine-distance order
        return [articles_by_id[aid] for aid in article_ids if aid in articles_by_id]

    # ------------------------------------------------------------------
    # Chunk search (KB_WIKI_CURATION_RAG_PLAN Phase 3)
    # ------------------------------------------------------------------

    async def _load_chunks_in_order(self, chunk_ids: list) -> list[KBChunk]:
        if not chunk_ids:
            return []
        from sqlalchemy.orm import joinedload

        result = await self.session.execute(
            sa.select(KBChunk).options(joinedload(KBChunk.article)).where(KBChunk.id.in_(chunk_ids))
        )
        chunks_by_id = {c.id: c for c in result.unique().scalars().all()}
        return [chunks_by_id[cid] for cid in chunk_ids if cid in chunks_by_id]

    async def search_chunks(
        self,
        tenant_id: UUID,
        query_embedding: list[float],
        space_id: UUID | None = None,
        limit: int = 5,
        viewer_role: str = "end_user",
    ) -> list[tuple[KBChunk, float]]:
        """Authenticated chunk search — tenant + global (see _scope_sql).

        tenant_id here follows the SAME "None = no filter, cross-tenant
        platform view" semantics as every other scoped method in this repo —
        it is NOT the anonymous path. Anonymous/global-only callers must use
        search_chunks_global, which hardcodes tenant_id IS NULL and never
        takes a tenant_id argument at all, so the two paths can't be confused
        by a caller passing None.
        """
        visibility_filter = (
            "" if viewer_role in _AGENT_ROLES else "AND c.visibility IN ('public', 'internal')"
        )
        space_filter = "AND c.space_id = :space_id" if space_id is not None else ""
        scope_filter = (
            "(c.tenant_id IS NULL OR c.tenant_id = :tenant_id)"
            if tenant_id is not None
            else "TRUE"
        )

        sql = text(
            f"""
            SELECT c.id, 1.0 - (c.embedding <=> CAST(:embedding AS vector)) AS score
            FROM kb_chunks c
            WHERE
                {scope_filter}
                AND c.embedding IS NOT NULL
                {visibility_filter}
                {space_filter}
            ORDER BY c.embedding <=> CAST(:embedding AS vector)
            LIMIT :lim
            """  # noqa: S608 — visibility_filter/scope_filter are controlled literals, not user input
        )
        params: dict = {"embedding": str(query_embedding), "lim": limit}
        if tenant_id is not None:
            params["tenant_id"] = tenant_id
        if space_id is not None:
            params["space_id"] = space_id

        result = await self.session.execute(sql, params)
        rows = result.all()
        chunk_ids = [row[0] for row in rows]
        score_by_id = {row[0]: float(row[1]) for row in rows}

        chunks = await self._load_chunks_in_order(chunk_ids)
        return [(c, score_by_id[c.id]) for c in chunks]

    async def search_chunks_global(
        self,
        query_embedding: list[float],
        space_id: UUID | None = None,
        limit: int = 5,
    ) -> list[tuple[KBChunk, float]]:
        """Anonymous chunk search — global (tenant_id IS NULL) + public only.

        Never merges another tenant's content, regardless of caller input —
        there is no tenant_id parameter to get wrong. This is the entire
        security boundary for the unauthenticated /kb/chunks/search path.
        """
        space_filter = "AND c.space_id = :space_id" if space_id is not None else ""
        sql = text(
            f"""
            SELECT c.id, 1.0 - (c.embedding <=> CAST(:embedding AS vector)) AS score
            FROM kb_chunks c
            WHERE
                c.tenant_id IS NULL
                AND c.visibility = 'public'
                AND c.embedding IS NOT NULL
                {space_filter}
            ORDER BY c.embedding <=> CAST(:embedding AS vector)
            LIMIT :lim
            """  # noqa: S608 — space_filter is a controlled literal, not user input
        )
        params: dict = {"embedding": str(query_embedding), "lim": limit}
        if space_id is not None:
            params["space_id"] = space_id

        result = await self.session.execute(sql, params)
        rows = result.all()
        chunk_ids = [row[0] for row in rows]
        score_by_id = {row[0]: float(row[1]) for row in rows}

        chunks = await self._load_chunks_in_order(chunk_ids)
        return [(c, score_by_id[c.id]) for c in chunks]

    # ------------------------------------------------------------------
    # get_by_slug
    # ------------------------------------------------------------------

    async def get_by_slug(
        self,
        space_id: UUID,
        slug: str,
        tenant_id: UUID,
    ) -> KBArticle | None:
        """Direct slug lookup — tenant-accessible only (see _accessible_clause).

        Like get_article_or_404, product scope does not gate direct access:
        the caller already knows the space+slug, so product_id is irrelevant
        to whether they may fetch it.
        """
        result = await self.session.execute(
            select(KBArticle).where(
                KBArticle.space_id == space_id,
                KBArticle.slug == slug,
                self._accessible_clause(tenant_id),
            )
        )
        return result.scalar_one_or_none()

    # ------------------------------------------------------------------
    # get_space_by_slug — global slugs first (partial unique index allows
    # a global space and a tenant space to share a slug)
    # ------------------------------------------------------------------

    async def get_space_by_slug(
        self, tenant_id: UUID | None, slug: str
    ) -> KBSpace | None:
        result = await self.session.execute(
            select(KBSpace).where(
                sa.or_(KBSpace.tenant_id == tenant_id, KBSpace.tenant_id.is_(None)),
                KBSpace.slug == slug,
            )
            .order_by(KBSpace.tenant_id.is_(None))  # tenant-specific match wins over global
        )
        return result.scalars().first()

    async def get_space_or_404(self, tenant_id: UUID | None, slug: str) -> KBSpace:
        space = await self.get_space_by_slug(tenant_id, slug)
        if space is None:
            raise ResourceNotFoundError("kb_space", slug)
        return space

    # ------------------------------------------------------------------
    # list_spaces — tenant's own spaces plus global spaces, or every
    # tenant's spaces when tenant_id is None (platform cross-tenant view)
    # ------------------------------------------------------------------

    async def list_spaces(
        self, tenant_id: UUID | None, active_only: bool = True
    ) -> list[KBSpace]:
        stmt = select(KBSpace)
        if tenant_id is not None:
            stmt = stmt.where(
                sa.or_(KBSpace.tenant_id == tenant_id, KBSpace.tenant_id.is_(None))
            )
        if active_only:
            stmt = stmt.where(KBSpace.is_active.is_(True))
        stmt = stmt.order_by(KBSpace.name)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    # ------------------------------------------------------------------
    # increment_view_count — raw UPDATE for atomicity
    # ------------------------------------------------------------------

    async def increment_view_count(self, article_id: UUID) -> None:
        """Atomically increment view_count without loading the full ORM object.

        synchronize_session=False — this is a fire-and-forget counter bump;
        syncing would expire onupdate-tracked columns (e.g. updated_at) on any
        already-loaded KBArticle instance, forcing a lazy refresh outside the
        async greenlet context the next time it's accessed (e.g. response
        serialization in get_article right after this call).
        """
        await self.session.execute(
            sa.update(KBArticle)
            .where(KBArticle.id == article_id)
            .values(view_count=KBArticle.view_count + 1)
            .execution_options(synchronize_session=False)
        )

    # ------------------------------------------------------------------
    # create_version_snapshot
    # ------------------------------------------------------------------

    async def create_version_snapshot(
        self,
        article: KBArticle,
        changed_by: UUID,
        change_summary: str | None,
    ) -> KBArticleVersion:
        """Snapshot current article title+body into a KBArticleVersion row."""
        snapshot = KBArticleVersion(
            article_id=article.id,
            version_number=article.version,
            title=article.title,
            body=article.body,
            changed_by=changed_by,
            change_summary=change_summary,
        )
        self.session.add(snapshot)
        await self.session.flush()
        await self.session.refresh(snapshot)
        return snapshot

    # ------------------------------------------------------------------
    # list_versions
    # ------------------------------------------------------------------

    async def list_versions(self, article_id: UUID) -> list[KBArticleVersion]:
        result = await self.session.execute(
            select(KBArticleVersion)
            .where(KBArticleVersion.article_id == article_id)
            .order_by(KBArticleVersion.version_number.desc())
        )
        return list(result.scalars().all())

    async def get_version(
        self, article_id: UUID, version_number: int
    ) -> KBArticleVersion | None:
        result = await self.session.execute(
            select(KBArticleVersion).where(
                KBArticleVersion.article_id == article_id,
                KBArticleVersion.version_number == version_number,
            )
        )
        return result.scalar_one_or_none()

    # ------------------------------------------------------------------
    # Feedback helpers
    # ------------------------------------------------------------------

    async def add_feedback(
        self,
        article_id: UUID,
        is_helpful: bool,
        comment: str | None,
        user_id: UUID | None,
    ) -> KBArticleFeedback:
        """Insert a feedback row and update counters atomically."""
        feedback = KBArticleFeedback(
            article_id=article_id,
            user_id=user_id,
            is_helpful=is_helpful,
            comment=comment,
        )
        self.session.add(feedback)

        # Atomically increment the appropriate counter
        if is_helpful:
            await self.session.execute(
                sa.update(KBArticle)
                .where(KBArticle.id == article_id)
                .values(helpful_count=KBArticle.helpful_count + 1)
            )
        else:
            await self.session.execute(
                sa.update(KBArticle)
                .where(KBArticle.id == article_id)
                .values(not_helpful_count=KBArticle.not_helpful_count + 1)
            )

        await self.session.flush()
        await self.session.refresh(feedback)
        return feedback

    # ------------------------------------------------------------------
    # Ticket-KB link helpers
    # ------------------------------------------------------------------

    async def link_ticket(
        self,
        article_id: UUID,
        ticket_id: UUID,
        linked_by: UUID | None,
        is_suggested: bool = False,
    ) -> TicketKBLink:
        link = TicketKBLink(
            ticket_id=ticket_id,
            article_id=article_id,
            linked_by=linked_by,
            is_suggested=is_suggested,
        )
        self.session.add(link)
        await self.session.flush()
        return link

    async def unlink_ticket(self, article_id: UUID, ticket_id: UUID) -> bool:
        result = await self.session.execute(
            select(TicketKBLink).where(
                TicketKBLink.article_id == article_id,
                TicketKBLink.ticket_id == ticket_id,
            )
        )
        link = result.scalar_one_or_none()
        if link is None:
            return False
        await self.session.delete(link)
        await self.session.flush()
        return True
