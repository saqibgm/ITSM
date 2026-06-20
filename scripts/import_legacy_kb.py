"""One-time importer: legacy chatbot RAG KB articles -> itsm-service KB.

Reads a JSON array (exported from the chatbot's kb_embeddings cache / GLPI) and
inserts each as a GLOBAL KB article (tenant_id NULL, product_id NULL) in a
global "General" space, so the content lives in the single ITSM KB store and is
visible to every tenant (and the chatbot, once it sources from itsm-service).

Idempotent: re-runs skip any article whose legacy_id already exists (matched via
the article's metadata.legacy_id). Platform-authored, so author_id is NULL
(requires migration 0016).

Usage (inside the api container, as the table owner so RLS is bypassed):
    docker compose exec -T \
      -e DATABASE_URL=postgresql+asyncpg://itsm_user:itsm_pass@db:5432/itsm_db \
      api python /tmp/import_legacy_kb.py /tmp/kb_export.json
"""

import asyncio
import datetime
import json
import os
import re
import sys
import unicodedata

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.models.kb import (
    KBArticle,
    KBArticleStatus,
    KBArticleVisibility,
    KBSpace,
    KBSpaceScope,
)
from uuid_extensions import uuid7

GLOBAL_SPACE_SLUG = "global-kb"
GLOBAL_SPACE_NAME = "Global Knowledge Base"


def slugify(text: str) -> str:
    norm = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", norm.lower()).strip("-")
    return slug[:200] or "article"


async def main(json_path: str) -> None:
    with open(json_path, encoding="utf-8") as f:
        articles = json.load(f)

    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with AsyncSession(engine, expire_on_commit=False) as session:
        # Ensure a GLOBAL space (tenant_id NULL) exists. The partial unique
        # indexes let a global space share the 'general' slug with tenant spaces.
        space = (
            await session.execute(
                select(KBSpace).where(
                    KBSpace.tenant_id.is_(None), KBSpace.slug == GLOBAL_SPACE_SLUG
                )
            )
        ).scalar_one_or_none()
        if space is None:
            space = KBSpace(
                id=uuid7(),
                tenant_id=None,
                product_id=None,
                name=GLOBAL_SPACE_NAME,
                slug=GLOBAL_SPACE_SLUG,
                description="Global IT knowledge shared across all tenants.",
                scope=KBSpaceScope.tenant_wide,
                is_public=True,
                is_active=True,
            )
            session.add(space)
            await session.flush()
            print(f"created global space {space.id}")
        else:
            print(f"reusing global space {space.id}")

        existing = (
            await session.execute(
                select(KBArticle).where(KBArticle.space_id == space.id)
            )
        ).scalars().all()
        seen_legacy = {(a.metadata_ or {}).get("legacy_id") for a in existing}
        used_slugs = {a.slug for a in existing}

        now = datetime.datetime.now(datetime.timezone.utc)
        created = skipped = 0
        for art in articles:
            legacy_id = art.get("legacy_id")
            if legacy_id and legacy_id in seen_legacy:
                skipped += 1
                continue

            base = slugify(art.get("title", ""))
            slug, n = base, 1
            while slug in used_slugs:
                slug = f"{base}-{n}"
                n += 1
            used_slugs.add(slug)

            body = art.get("content") or art.get("title") or ""
            excerpt = (art.get("question") or body)[:300] or None

            session.add(
                KBArticle(
                    id=uuid7(),
                    tenant_id=None,
                    product_id=None,
                    space_id=space.id,
                    category_id=None,
                    title=(art.get("title") or "Untitled")[:500],
                    slug=slug,
                    body=body,
                    excerpt=excerpt,
                    status=KBArticleStatus.published,
                    visibility=KBArticleVisibility.public,
                    author_id=None,
                    version=1,
                    published_at=now,
                    metadata_={
                        "legacy_id": legacy_id,
                        "source": art.get("source"),
                        "glpi_id": art.get("glpi_id"),
                        "category": art.get("category"),
                        "keywords": art.get("keywords") or [],
                        "imported_at": now.isoformat(),
                    },
                )
            )
            created += 1

        await session.commit()
        print(f"done: created={created} skipped={skipped} total_in_space={created + len(existing)}")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/kb_export.json"))
