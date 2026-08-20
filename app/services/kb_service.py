"""Service layer for Knowledge Base operations.

Business rules live here; the repository layer handles all DB I/O.
"""

import re
import unicodedata
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import AuthorizationError, InvalidStateTransitionError, ValidationError
from app.models.kb import KBArticle, KBArticleStatus, KBArticleVersion
from app.repositories.kb_repo import KBRepository

# Roles allowed to publish directly from draft (bypassing under_review)
_FAST_PUBLISH_ROLES = {"team_lead", "manager", "admin"}


def _slugify(text: str) -> str:
    """Convert a title string to a URL-safe slug.

    Steps:
      1. NFKD normalise, drop combining characters
      2. Lower-case
      3. Replace non-alphanumeric sequences with hyphens
      4. Strip leading/trailing hyphens
      5. Truncate to 200 chars
    """
    normalised = unicodedata.normalize("NFKD", text)
    ascii_str = normalised.encode("ascii", "ignore").decode("ascii")
    lower = ascii_str.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", lower).strip("-")
    return slug[:200] or "article"


class KBService:
    """Domain operations for KB articles and spaces."""

    # ------------------------------------------------------------------
    # Slug generation
    # ------------------------------------------------------------------

    async def make_unique_slug(
        self,
        title: str,
        space_id: UUID,
        tenant_id: UUID | None,
        db: AsyncSession,
        exclude_article_id: UUID | None = None,
    ) -> str:
        """Generate a URL-safe slug from title, ensuring uniqueness within space."""
        repo = KBRepository(db)
        base = _slugify(title)
        candidate = base
        suffix = 1

        while True:
            existing = await repo.get_by_slug(space_id, candidate, tenant_id)
            if existing is None or (
                exclude_article_id is not None and existing.id == exclude_article_id
            ):
                return candidate
            candidate = f"{base}-{suffix}"
            suffix += 1

    # ------------------------------------------------------------------
    # Publish
    # ------------------------------------------------------------------

    async def publish_article(
        self,
        article: KBArticle,
        actor_id: UUID,
        actor_roles: list[str],
        db: AsyncSession,
        change_summary: str | None = None,
    ) -> KBArticle:
        """Transition article to published status.

        Allowed transitions:
          - under_review → published   (any team_lead+)
          - draft → published          (team_lead+ only, fast-path)

        Sets published_at, increments version, creates a version snapshot.
        """
        repo = KBRepository(db)
        actor_role_set = set(actor_roles)

        if article.status == KBArticleStatus.published:
            return article  # idempotent

        if article.status == KBArticleStatus.archived:
            raise InvalidStateTransitionError(
                article.status.value,
                KBArticleStatus.published.value,
                [KBArticleStatus.under_review.value],
            )

        if article.status == KBArticleStatus.draft:
            if not actor_role_set.intersection(_FAST_PUBLISH_ROLES):
                raise AuthorizationError(
                    "Drafts can only be published directly by team_lead or higher; "
                    "submit for review first."
                )

        # Snapshot current state before changing
        await repo.create_version_snapshot(article, actor_id, change_summary)

        article.status = KBArticleStatus.published
        article.published_at = func.now()  # type: ignore[assignment]
        article.version = article.version + 1

        await db.flush()
        await db.refresh(article)

        # Dispatch outbound webhook event — never let errors propagate
        try:
            from app.workers.tasks_webhooks import dispatch_webhook_event
            dispatch_webhook_event.delay(
                str(article.tenant_id),
                "kb_article.published",
                {
                    "id": str(article.id),
                    "title": article.title,
                    "slug": article.slug,
                    "version": article.version,
                },
            )
        except Exception:
            pass

        return article

    # ------------------------------------------------------------------
    # Submit for review
    # ------------------------------------------------------------------

    async def submit_for_review(
        self,
        article: KBArticle,
        actor_id: UUID,
        db: AsyncSession,
    ) -> KBArticle:
        """Transition draft → under_review."""
        if article.status != KBArticleStatus.draft:
            raise InvalidStateTransitionError(
                article.status.value,
                KBArticleStatus.under_review.value,
                [KBArticleStatus.draft.value],
            )
        article.status = KBArticleStatus.under_review
        await db.flush()
        await db.refresh(article)
        return article

    # ------------------------------------------------------------------
    # Archive
    # ------------------------------------------------------------------

    async def archive_article(
        self,
        article: KBArticle,
        db: AsyncSession,
    ) -> KBArticle:
        """Set status=archived. Any non-archived article may be archived."""
        if article.status == KBArticleStatus.archived:
            return article  # idempotent

        article.status = KBArticleStatus.archived
        await db.flush()
        await db.refresh(article)
        return article

    # ------------------------------------------------------------------
    # Reject an AI-curated draft (KB_WIKI_CURATION_RAG_PLAN Phase 1)
    # ------------------------------------------------------------------

    async def reject_curation_draft(
        self,
        article: KBArticle,
        reviewer_notes: str,
        db: AsyncSession,
    ) -> KBArticle:
        """Archive an ai_curated_pending_review draft with reviewer notes.

        Does not loop back into re-synthesis (that needs the durable-pause
        machinery LangGraph's checkpointer/interrupt() provides — Phase 2).
        """
        if article.status != KBArticleStatus.ai_curated_pending_review:
            raise InvalidStateTransitionError(
                article.status.value,
                KBArticleStatus.archived.value,
                [KBArticleStatus.ai_curated_pending_review.value],
            )

        source = dict(article.curation_source or {})
        source["state"] = "rejected"
        source["reviewer_notes"] = reviewer_notes
        article.curation_source = source
        article.status = KBArticleStatus.archived

        await db.flush()
        await db.refresh(article)
        return article

    # ------------------------------------------------------------------
    # Restore version
    # ------------------------------------------------------------------

    async def restore_version(
        self,
        article: KBArticle,
        version: KBArticleVersion,
        actor_id: UUID,
        db: AsyncSession,
        change_summary: str | None = None,
    ) -> KBArticle:
        """Replace article title+body with a prior version snapshot.

        Snapshots the current state before overwriting, then increments version.
        The article status is NOT changed — archived articles stay archived.
        """
        repo = KBRepository(db)

        if version.article_id != article.id:
            raise ValidationError(
                f"Version {version.id} does not belong to article {article.id}"
            )

        # Snapshot current before overwriting
        await repo.create_version_snapshot(
            article,
            actor_id,
            change_summary or f"Restored to version {version.version_number}",
        )

        article.title = version.title
        article.body = version.body
        article.last_edited_by = actor_id  # type: ignore[assignment]
        article.version = article.version + 1

        await db.flush()
        await db.refresh(article)
        return article
