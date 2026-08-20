"""
Mocked-DB tests for cluster_kb_gaps_and_draft (app/workers/tasks_kb.py),
KB_WIKI_CURATION_RAG_PLAN Phase 5 — confirms the task creates a pending
KBArticle + dispatches run_kb_curation + writes curation_article_id back to
Project-IQ-V2's DB, without needing a real second database in tests.
"""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4


def _mock_psycopg_module(fetch_rows):
    """Fake `psycopg` module whose .connect(...) is a context manager
    wrapping a cursor that's also a context manager (matches the task's
    `with psycopg.connect(...) as conn: with conn.cursor() as cur:` usage)."""
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = fetch_rows
    mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
    mock_cursor.__exit__ = MagicMock(return_value=False)

    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cursor
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)

    mock_module = MagicMock()
    mock_module.connect = MagicMock(return_value=mock_conn)
    return mock_module, mock_cursor


async def test_cluster_kb_gaps_creates_pending_article_and_dispatches():
    from app.workers.tasks_kb import _cluster_kb_gaps_and_draft_async

    tenant_id = uuid4()
    space_id = uuid4()

    # Five identical-embedding gap rows for one org — meets the default
    # KB_GAP_CLUSTER_MIN_COUNT=5 threshold in a single cluster.
    gap_rows = [(i, "how do refunds work", "org-1") for i in range(1, 6)]
    mock_psycopg, mock_cursor = _mock_psycopg_module(gap_rows)

    mock_embedder_instance = AsyncMock()
    mock_embedder_instance.embed_batch.return_value = [[1.0, 0.0, 0.0]]  # one unique query

    fake_tenant_result = MagicMock()
    fake_tenant_result.scalar_one_or_none.return_value = tenant_id

    fake_space = MagicMock()
    fake_space.id = space_id

    fake_space_result = MagicMock()
    fake_space_result.scalar_one_or_none.return_value = fake_space

    mock_db = AsyncMock()
    mock_db.__aenter__ = AsyncMock(return_value=mock_db)
    mock_db.__aexit__ = AsyncMock(return_value=False)
    mock_db.execute = AsyncMock(side_effect=[fake_tenant_result, fake_space_result])

    with (
        patch("psycopg.connect", mock_psycopg.connect),
        patch("app.database.AsyncSessionLocal") as mock_session_cls,
        patch("app.services.ai.embedder.EmbedderService") as mock_embedder_cls,
        patch("app.services.kb_service.KBService") as mock_kb_service_cls,
        patch("app.workers.tasks_kb.run_kb_curation") as mock_run_kb_curation,
    ):
        mock_session_cls.return_value = mock_db
        mock_embedder_cls.return_value = mock_embedder_instance
        mock_kb_service_instance = AsyncMock()
        mock_kb_service_instance.make_unique_slug.return_value = "how-do-refunds-work"
        mock_kb_service_cls.return_value = mock_kb_service_instance
        mock_run_kb_curation.delay = MagicMock()

        await _cluster_kb_gaps_and_draft_async()

    assert mock_db.add.call_count == 1
    added_article = mock_db.add.call_args.args[0]
    assert added_article.curation_source["trigger"] == "gap_feedback"
    assert added_article.curation_source["gap_count"] == 5
    assert added_article.status.value == "ai_curated_pending_review"
    assert added_article.tenant_id == tenant_id
    assert added_article.space_id == space_id

    mock_run_kb_curation.delay.assert_called_once()

    # Write-back: the UPDATE call should reference all 5 gap ids.
    write_calls = [c for c in mock_cursor.execute.call_args_list if "UPDATE kb_search_gaps" in c.args[0]]
    assert len(write_calls) == 1
    assert write_calls[0].args[1][1] == [1, 2, 3, 4, 5]


async def test_cluster_kb_gaps_nothing_to_do_when_no_gaps():
    from app.workers.tasks_kb import _cluster_kb_gaps_and_draft_async

    mock_psycopg, mock_cursor = _mock_psycopg_module([])

    with (
        patch("psycopg.connect", mock_psycopg.connect),
        patch("app.database.AsyncSessionLocal") as mock_session_cls,
        patch("app.workers.tasks_kb.run_kb_curation") as mock_run_kb_curation,
    ):
        await _cluster_kb_gaps_and_draft_async()

    mock_session_cls.assert_not_called()
    mock_run_kb_curation.delay.assert_not_called()


async def test_cluster_kb_gaps_unknown_org_is_skipped_not_dispatched():
    from app.workers.tasks_kb import _cluster_kb_gaps_and_draft_async

    gap_rows = [(i, "how do refunds work", "org-unknown") for i in range(1, 6)]
    mock_psycopg, mock_cursor = _mock_psycopg_module(gap_rows)

    mock_embedder_instance = AsyncMock()
    mock_embedder_instance.embed_batch.return_value = [[1.0, 0.0, 0.0]]

    fake_tenant_result = MagicMock()
    fake_tenant_result.scalar_one_or_none.return_value = None  # org doesn't resolve to a tenant

    mock_db = AsyncMock()
    mock_db.__aenter__ = AsyncMock(return_value=mock_db)
    mock_db.__aexit__ = AsyncMock(return_value=False)
    mock_db.execute = AsyncMock(return_value=fake_tenant_result)

    with (
        patch("psycopg.connect", mock_psycopg.connect),
        patch("app.database.AsyncSessionLocal") as mock_session_cls,
        patch("app.services.ai.embedder.EmbedderService") as mock_embedder_cls,
        patch("app.workers.tasks_kb.run_kb_curation") as mock_run_kb_curation,
    ):
        mock_session_cls.return_value = mock_db
        mock_embedder_cls.return_value = mock_embedder_instance
        mock_run_kb_curation.delay = MagicMock()

        await _cluster_kb_gaps_and_draft_async()

    mock_db.add.assert_not_called()
    mock_run_kb_curation.delay.assert_not_called()
