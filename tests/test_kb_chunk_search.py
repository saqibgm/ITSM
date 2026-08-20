"""
Tests for KB chunk search (KB_WIKI_CURATION_RAG_PLAN Phase 3).

Two layers, deliberately separated:
  - Repository-level tests against real seeded rows — this is where the
    actual security boundary (anonymous never sees tenant content) lives,
    so it's tested directly against the SQL, not through the HTTP/JWT stack.
  - HTTP-level tests using the existing agent_client/async_client fixtures
    with the repository mocked — confirms the endpoint wires role/tenant/
    anonymous-vs-authenticated correctly, independent of query correctness.
"""

from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.asyncio

_FIXED_VEC = "[" + ",".join(["1"] + ["0"] * 1535) + "]"


async def _seed_space(db, tenant_id) -> UUID:
    space_id = uuid4()
    await db.execute(
        text(
            """
            INSERT INTO kb_spaces (id, tenant_id, name, slug, is_public, is_active)
            VALUES (:id, :tenant_id, 'Test Space', :slug, true, true)
            """
        ),
        {"id": str(space_id), "tenant_id": str(tenant_id) if tenant_id else None, "slug": f"space-{uuid4().hex[:8]}"},
    )
    await db.flush()
    return space_id


async def _seed_article(db, space_id, tenant_id, author_id, visibility: str = "public") -> UUID:
    article_id = uuid4()
    await db.execute(
        text(
            """
            INSERT INTO kb_articles (id, tenant_id, space_id, title, slug, body, status, visibility, author_id, version)
            VALUES (:id, :tenant_id, :space_id, 'Refund policy', :slug, 'body', 'published'::kbarticlestatus, CAST(:visibility AS kbarticlevisibility), :author_id, 1)
            """
        ),
        {
            "id": str(article_id), "tenant_id": str(tenant_id) if tenant_id else None,
            "space_id": str(space_id), "slug": f"article-{uuid4().hex[:8]}",
            "visibility": visibility, "author_id": str(author_id),
        },
    )
    await db.flush()
    return article_id


async def _seed_chunk(db, article_id, tenant_id, space_id, visibility: str, heading: str, content: str) -> UUID:
    chunk_id = uuid4()
    await db.execute(
        text(
            """
            INSERT INTO kb_chunks (id, article_id, tenant_id, space_id, visibility, chunk_index, heading, content, embedding)
            VALUES (:id, :article_id, :tenant_id, :space_id, CAST(:visibility AS kbarticlevisibility), 0, :heading, :content, CAST(:embedding AS vector))
            """
        ),
        {
            "id": str(chunk_id), "article_id": str(article_id),
            "tenant_id": str(tenant_id) if tenant_id else None, "space_id": str(space_id),
            "visibility": visibility, "heading": heading, "content": content, "embedding": _FIXED_VEC,
        },
    )
    await db.flush()
    return chunk_id


# ===========================================================================
# Repository-level — the actual security boundary
# ===========================================================================


async def test_search_chunks_global_never_returns_tenant_content(db, seeded_tenant, test_tenant_id, test_agent_user_id):
    """The anonymous path (search_chunks_global) must only ever return
    tenant_id IS NULL + visibility=public chunks — this is the whole security
    boundary for the unauthenticated endpoint."""
    from app.repositories.kb_repo import KBRepository

    tenant_space = await _seed_space(db, test_tenant_id)
    tenant_article = await _seed_article(db, tenant_space, test_tenant_id, test_agent_user_id, visibility="public")
    await _seed_chunk(db, tenant_article, test_tenant_id, tenant_space, "public", "Tenant Section", "Tenant-specific refund content.")

    global_space = await _seed_space(db, None)
    global_article = await _seed_article(db, global_space, None, test_agent_user_id, visibility="public")
    await _seed_chunk(db, global_article, None, global_space, "public", "Global Section", "Global refund content for everyone.")

    repo = KBRepository(db)
    results = await repo.search_chunks_global(query_embedding=[1.0] + [0.0] * 1535, limit=10)

    contents = {c.content for c, _score in results}
    assert "Global refund content for everyone." in contents
    assert "Tenant-specific refund content." not in contents


async def test_search_chunks_global_excludes_internal_visibility(db, seeded_tenant, test_agent_user_id):
    """Global-but-internal content must not leak to anonymous callers either —
    anonymous is public-only, not just tenant-only."""
    from app.repositories.kb_repo import KBRepository

    space = await _seed_space(db, None)
    article = await _seed_article(db, space, None, test_agent_user_id, visibility="internal")
    await _seed_chunk(db, article, None, space, "internal", "Internal Section", "Internal-only global content.")

    repo = KBRepository(db)
    results = await repo.search_chunks_global(query_embedding=[1.0] + [0.0] * 1535, limit=10)

    contents = {c.content for c, _score in results}
    assert "Internal-only global content." not in contents


async def test_search_chunks_authenticated_sees_own_tenant_and_global_not_other_tenant(db, seeded_tenant, test_tenant_id, test_agent_user_id):
    """The authenticated path (search_chunks) must merge own-tenant + global,
    and never another tenant's content."""
    from app.repositories.kb_repo import KBRepository

    own_space = await _seed_space(db, test_tenant_id)
    own_article = await _seed_article(db, own_space, test_tenant_id, test_agent_user_id, visibility="public")
    await _seed_chunk(db, own_article, test_tenant_id, own_space, "public", "Own Section", "Own tenant refund content.")

    other_tenant_id = uuid4()
    await db.execute(
        text(
            "INSERT INTO tenants (id, iam_org_id, name, slug, is_active, settings) "
            "VALUES (:id, :iam_org_id, 'Other Corp', :slug, true, '{}')"
        ),
        {"id": str(other_tenant_id), "iam_org_id": f"org_other_{other_tenant_id.hex[:8]}", "slug": f"other-corp-{other_tenant_id.hex[:8]}"},
    )
    await db.flush()
    other_space = await _seed_space(db, other_tenant_id)
    other_article = await _seed_article(db, other_space, other_tenant_id, test_agent_user_id, visibility="public")
    await _seed_chunk(db, other_article, other_tenant_id, other_space, "public", "Other Section", "Other tenant refund content.")

    global_space = await _seed_space(db, None)
    global_article = await _seed_article(db, global_space, None, test_agent_user_id, visibility="public")
    await _seed_chunk(db, global_article, None, global_space, "public", "Global Section", "Global refund content.")

    repo = KBRepository(db)
    results = await repo.search_chunks(
        tenant_id=test_tenant_id, query_embedding=[1.0] + [0.0] * 1535, limit=10, viewer_role="end_user",
    )

    contents = {c.content for c, _score in results}
    assert "Own tenant refund content." in contents
    assert "Global refund content." in contents
    assert "Other tenant refund content." not in contents


# ===========================================================================
# HTTP-level — endpoint wiring (repository mocked)
# ===========================================================================


async def test_endpoint_authenticated_calls_tenant_scoped_search(agent_client):
    with (
        patch("app.services.ai.ai_service.AIService.embed", new_callable=AsyncMock, return_value=[0.0] * 1536),
        patch("app.repositories.kb_repo.KBRepository.search_chunks", new_callable=AsyncMock, return_value=[]) as mock_search,
    ):
        resp = await agent_client.get("/api/v1/kb/chunks/search", params={"q": "refund policy"})

    assert resp.status_code == 200, resp.text
    assert resp.json() == []
    mock_search.assert_called_once()


async def test_endpoint_anonymous_no_auth_header_calls_global_search(async_client):
    """No Authorization header at all — must succeed (not 401) and use the
    global-only search path, never the tenant-scoped one."""
    async_client.headers.pop("Authorization", None)

    with (
        patch("app.services.ai.ai_service.AIService.embed", new_callable=AsyncMock, return_value=[0.0] * 1536),
        patch("app.repositories.kb_repo.KBRepository.search_chunks_global", new_callable=AsyncMock, return_value=[]) as mock_global,
        patch("app.repositories.kb_repo.KBRepository.search_chunks", new_callable=AsyncMock) as mock_tenant,
    ):
        resp = await async_client.get("/api/v1/kb/chunks/search", params={"q": "refund policy"})

    assert resp.status_code == 200, resp.text
    mock_global.assert_called_once()
    mock_tenant.assert_not_called()
