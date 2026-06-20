"""
One-time import: GLPI KB export (JSON) -> kb_articles.

Reads the JSON file produced by Project-IQ-V2's scripts/export_glpi_kb.py
and creates one global KB space (tenant_id=NULL, product_id=NULL) plus one
published KBArticle per GLPI item.

Per the agreed KB scoping rules, content with no tenant/product association
in the source system is global — visible to every tenant and product:
    tenant_id NULL + product_id NULL -> global ("for all")

KBArticle.author_id is a NOT NULL FK to users.id, and there is no system/
platform user row to attribute migrated content to, so the operator must
supply an existing user's id (e.g. a 99Technologies admin) via --author-id.

Idempotent: re-running skips GLPI items already imported, matched by
metadata->>'glpi_id' within the target space.

Usage (from itsm-service root, inside the app's environment):
    python -m scripts.import_glpi_kb \
        --input ../Project-IQ-V2/data/glpi_kb_export.json \
        --author-id <existing-user-uuid> \
        [--space-slug migrated-glpi-kb] [--dry-run]
"""
import argparse
import asyncio
import json
import logging
from pathlib import Path
from uuid import UUID

import sqlalchemy as sa

from app.database import AsyncSessionLocal
from app.models.kb import KBArticle, KBArticleStatus, KBArticleVisibility, KBSpace, KBSpaceScope
from app.services.ai.ai_service import AIService
from app.services.kb_service import KBService

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("import_glpi_kb")

SPACE_NAME = "Migrated from GLPI"


async def _get_or_create_space(db, slug: str) -> KBSpace:
    existing = await db.scalar(
        sa.select(KBSpace).where(KBSpace.tenant_id.is_(None), KBSpace.slug == slug)
    )
    if existing:
        return existing

    space = KBSpace(
        tenant_id=None,
        product_id=None,
        name=SPACE_NAME,
        description="Articles imported from the legacy GLPI knowledge base.",
        slug=slug,
        scope=KBSpaceScope.tenant_wide,
        is_public=True,
        is_active=True,
    )
    db.add(space)
    await db.flush()
    logger.info("Created global KB space %r (id=%s)", slug, space.id)
    return space


async def _already_imported(db, space_id: UUID, glpi_id) -> bool:
    row = await db.scalar(
        sa.select(KBArticle.id).where(
            KBArticle.space_id == space_id,
            KBArticle.metadata_["glpi_id"].astext == str(glpi_id),
        )
    )
    return row is not None


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="Path to glpi_kb_export.json")
    parser.add_argument("--author-id", required=True, help="UUID of an existing user to attribute imported articles to")
    parser.add_argument("--space-slug", default="migrated-glpi-kb")
    parser.add_argument("--dry-run", action="store_true", help="Parse and report only; write nothing")
    args = parser.parse_args()

    author_id = UUID(args.author_id)
    input_path = Path(args.input).resolve()
    with open(input_path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    articles = payload.get("articles", [])
    logger.info("Loaded %d articles from %s", len(articles), input_path)

    if args.dry_run:
        logger.info("[dry-run] Would import %d articles into global space %r", len(articles), args.space_slug)
        return

    kb_service = KBService()
    ai_service = AIService()

    imported = skipped = failed = 0

    async with AsyncSessionLocal() as db:
        space = await _get_or_create_space(db, args.space_slug)
        await db.commit()

        for item in articles:
            glpi_id = item.get("glpi_id")
            title = (item.get("title") or "Untitled").strip()
            body = (item.get("body_markdown") or "").strip()

            if not body or len(body) < 10:
                logger.warning("Skipping GLPI item %s (%r): body too short", glpi_id, title)
                skipped += 1
                continue

            try:
                async with AsyncSessionLocal() as session:
                    if await _already_imported(session, space.id, glpi_id):
                        skipped += 1
                        continue

                    slug = await kb_service.make_unique_slug(title, space.id, tenant_id=None, db=session)

                    embedding = None
                    try:
                        embedding = await ai_service.embed(f"{title}\n\n{body[:8000]}")
                    except Exception as exc:
                        logger.warning("Embedding failed for GLPI item %s: %r (importing without embedding)", glpi_id, exc)

                    article = KBArticle(
                        tenant_id=None,
                        product_id=None,
                        space_id=space.id,
                        title=title,
                        slug=slug,
                        body=body,
                        excerpt=(item.get("question") or "")[:500] or None,
                        status=KBArticleStatus.published,
                        visibility=KBArticleVisibility.public,
                        author_id=author_id,
                        version=1,
                        embedding=embedding,
                        metadata_={"source": "glpi", "glpi_id": glpi_id, "glpi_date_mod": item.get("date_mod") or ""},
                    )
                    article.published_at = sa.func.now()
                    session.add(article)
                    await session.commit()
                    imported += 1
                    logger.info("Imported GLPI item %s -> %s (%s)", glpi_id, slug, title[:60])
            except Exception:
                failed += 1
                logger.exception("Failed to import GLPI item %s (%r)", glpi_id, title)

    logger.info("Done. imported=%d skipped=%d failed=%d total=%d", imported, skipped, failed, len(articles))


if __name__ == "__main__":
    asyncio.run(main())
