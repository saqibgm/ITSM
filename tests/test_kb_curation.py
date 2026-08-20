"""
Integration + unit tests for KB wiki curation (/api/v1/kb/curation),
KB_WIKI_CURATION_RAG_PLAN Phase 1 (manual trigger, synchronous) + Phase 2
(ticket-resolved trigger, durable interrupt()-paused review).

Fixtures from conftest used:
  - tenant_admin_client — admin role; satisfies the team_lead+ gate on
                          approve/reject (admin is in _TEAM_LEAD_ROLES)
  - agent_client        — agent role; can trigger a curation run
  - end_user_client     — end_user role; should be rejected by every endpoint
"""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

pytestmark = pytest.mark.asyncio


def _space_body(**overrides) -> dict:
    base_slug = f"test-space-{uuid4().hex[:8]}"
    return {
        "name": "Test Space",
        "slug": base_slug,
        "description": "A test KB space",
        "is_public": True,
        **overrides,
    }


async def _create_space_and_get_id(client) -> str:
    resp = await client.post("/api/v1/kb/spaces", json=_space_body())
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _trigger_body(space_id, **overrides) -> dict:
    return {
        "space_id": str(space_id),
        "title": "How refunds are processed",
        "source_text": "Refunds are issued to the original payment method within 5-7 business days "
        "once the returned item is received and inspected by our warehouse team.",
        **overrides,
    }


@asynccontextmanager
async def _fake_checkpointer():
    """Stand-in for app.services.ai.kb_curation_checkpointer.get_checkpointer —
    tests mock KBCurator wholesale, so the checkpointer object itself is opaque."""
    yield MagicMock()


# ===========================================================================
# POST /kb/curation/run
# ===========================================================================


async def test_trigger_curation_creates_pending_draft_and_fires_task(agent_client, tenant_admin_client):
    """POST /kb/curation/run creates a pending draft and dispatches the Celery task."""
    space_id = await _create_space_and_get_id(tenant_admin_client)

    with patch("app.workers.tasks_kb.run_kb_curation.delay") as mock_delay:
        resp = await agent_client.post("/api/v1/kb/curation/run", json=_trigger_body(space_id))

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "ai_curated_pending_review"
    assert body["curation_source"]["trigger"] == "manual"
    assert body["curation_source"]["state"] == "running"
    assert body["body"] == ""  # not synthesized yet — full text, not the excerpt
    mock_delay.assert_called_once_with(body["id"])


async def test_end_user_cannot_trigger_curation(end_user_client):
    """An end_user (not agent+) gets 403 triggering a curation run."""
    resp = await end_user_client.post(
        "/api/v1/kb/curation/run",
        json=_trigger_body(str(uuid4())),
    )
    assert resp.status_code == 403


# ===========================================================================
# GET /kb/curation/pending
# ===========================================================================


async def test_list_pending_curation_shows_created_draft(agent_client, tenant_admin_client):
    space_id = await _create_space_and_get_id(tenant_admin_client)

    with patch("app.workers.tasks_kb.run_kb_curation.delay"):
        create_resp = await agent_client.post("/api/v1/kb/curation/run", json=_trigger_body(space_id))
    article_id = create_resp.json()["id"]

    resp = await agent_client.get("/api/v1/kb/curation/pending")
    assert resp.status_code == 200, resp.text
    ids = {a["id"] for a in resp.json()}
    assert article_id in ids


# ===========================================================================
# POST /kb/curation/{id}/approve — Phase 2: resumes the paused graph
# ===========================================================================


async def test_approve_curation_draft_fires_resume_task(agent_client, tenant_admin_client):
    """POST approve dispatches resume_kb_curation("approve") — 202, not yet published
    (actual publish happens inside the mocked-out task)."""
    space_id = await _create_space_and_get_id(tenant_admin_client)

    with patch("app.workers.tasks_kb.run_kb_curation.delay"):
        create_resp = await agent_client.post("/api/v1/kb/curation/run", json=_trigger_body(space_id))
    article_id = create_resp.json()["id"]

    with patch("app.workers.tasks_kb.resume_kb_curation.delay") as mock_delay:
        resp = await tenant_admin_client.post(f"/api/v1/kb/curation/{article_id}/approve")

    assert resp.status_code == 202, resp.text
    assert resp.json()["status"] == "ai_curated_pending_review"
    mock_delay.assert_called_once_with(article_id, "approve", None)


async def test_agent_cannot_approve_curation_draft(agent_client, tenant_admin_client):
    """Agent role does not satisfy team_lead+ — approve returns 403."""
    space_id = await _create_space_and_get_id(tenant_admin_client)

    with patch("app.workers.tasks_kb.run_kb_curation.delay"):
        create_resp = await agent_client.post("/api/v1/kb/curation/run", json=_trigger_body(space_id))
    article_id = create_resp.json()["id"]

    resp = await agent_client.post(f"/api/v1/kb/curation/{article_id}/approve")
    assert resp.status_code == 403


# ===========================================================================
# POST /kb/curation/{id}/reject
# ===========================================================================


async def test_reject_curation_draft_archives_with_notes(agent_client, tenant_admin_client):
    """resynthesize=false (default) — unchanged from Phase 1: synchronous archive."""
    space_id = await _create_space_and_get_id(tenant_admin_client)

    with patch("app.workers.tasks_kb.run_kb_curation.delay"):
        create_resp = await agent_client.post("/api/v1/kb/curation/run", json=_trigger_body(space_id))
    article_id = create_resp.json()["id"]

    resp = await tenant_admin_client.post(
        f"/api/v1/kb/curation/{article_id}/reject",
        json={"reviewer_notes": "Missing the international-shipping refund window."},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "archived"


async def test_reject_curation_draft_with_resynthesize_fires_resume_task(agent_client, tenant_admin_client):
    """resynthesize=true — Phase 2: dispatches resume_kb_curation("reject", notes), 202,
    draft stays pending (not archived) since it's going back through synthesis."""
    space_id = await _create_space_and_get_id(tenant_admin_client)

    with patch("app.workers.tasks_kb.run_kb_curation.delay"):
        create_resp = await agent_client.post("/api/v1/kb/curation/run", json=_trigger_body(space_id))
    article_id = create_resp.json()["id"]

    with patch("app.workers.tasks_kb.resume_kb_curation.delay") as mock_delay:
        resp = await tenant_admin_client.post(
            f"/api/v1/kb/curation/{article_id}/reject",
            json={"reviewer_notes": "Add the international-shipping window.", "resynthesize": True},
        )

    assert resp.status_code == 202, resp.text
    assert resp.json()["status"] == "ai_curated_pending_review"
    mock_delay.assert_called_once_with(article_id, "reject", "Add the international-shipping window.")


# ===========================================================================
# Celery tasks — pure unit tests (mirrors tests/test_ai_tickets.py's pattern)
# ===========================================================================


def _mock_db_session(fake_article):
    """scalar_one_or_none() is sync on a real SQLAlchemy Result — use MagicMock
    for the result object so calling it doesn't return an unawaited coroutine."""
    mock_db = AsyncMock()
    mock_db.__aenter__ = AsyncMock(return_value=mock_db)
    mock_db.__aexit__ = AsyncMock(return_value=False)
    mock_execute_result = MagicMock()
    mock_execute_result.scalar_one_or_none.return_value = fake_article
    mock_db.execute = AsyncMock(return_value=mock_execute_result)
    return mock_db


def _fake_pending_article(trigger="manual"):
    from app.models.kb import KBArticleStatus

    article = MagicMock()
    article.id = str(uuid4())
    article.tenant_id = uuid4()
    article.space_id = uuid4()
    article.category_id = uuid4()
    article.author_id = uuid4()
    article.title = "How refunds are processed"
    article.status = KBArticleStatus.ai_curated_pending_review
    article.curation_source = {
        "trigger": trigger,
        "state": "running",
        "source_title": "How refunds are processed",
        "source_text": "Refunds go back to the original payment method within 5-7 business days.",
    }
    return article


async def test_run_kb_curation_happy_path_marks_ready_for_review():
    from app.workers.tasks_kb import _run_kb_curation_async

    fake_article = _fake_pending_article()

    with (
        patch("app.database.AsyncSessionLocal") as mock_session_cls,
        patch("app.services.ai.ai_service.AIService") as mock_ai_cls,
        patch("app.repositories.kb_repo.KBRepository") as mock_repo_cls,
        patch("app.services.ai.kb_curation_checkpointer.get_checkpointer", _fake_checkpointer),
        patch("app.services.ai.kb_curator.KBCurator") as mock_curator_cls,
    ):
        mock_session_cls.return_value = _mock_db_session(fake_article)

        mock_ai_instance = AsyncMock()
        mock_ai_instance.embed.return_value = [0.1] * 10
        mock_ai_cls.return_value = mock_ai_instance

        mock_repo_instance = AsyncMock()
        mock_repo_instance.search_hybrid.return_value = []
        mock_repo_cls.return_value = mock_repo_instance

        mock_curator_instance = AsyncMock()
        mock_curator_instance.curate.return_value = {
            "paused": True,
            "title": "How Refunds Are Processed",
            "body": "## Overview\nRefunds post within 5-7 business days.",
            "citations": ["within 5-7 business days"],
            "lint_findings": [],
        }
        mock_curator_cls.return_value = mock_curator_instance

        await _run_kb_curation_async(fake_article.id)

    assert fake_article.title == "How Refunds Are Processed"
    assert fake_article.curation_source["state"] == "ready_for_review"
    assert fake_article.curation_source["citations"] == ["within 5-7 business days"]


async def test_run_kb_curation_budget_exhausted_marks_failed():
    from app.exceptions import AIBudgetExhaustedError
    from app.workers.tasks_kb import _run_kb_curation_async

    fake_article = _fake_pending_article()

    with (
        patch("app.database.AsyncSessionLocal") as mock_session_cls,
        patch("app.services.ai.ai_service.AIService") as mock_ai_cls,
        patch("app.repositories.kb_repo.KBRepository") as mock_repo_cls,
        patch("app.services.ai.kb_curation_checkpointer.get_checkpointer", _fake_checkpointer),
        patch("app.services.ai.kb_curator.KBCurator") as mock_curator_cls,
    ):
        mock_session_cls.return_value = _mock_db_session(fake_article)

        mock_ai_instance = AsyncMock()
        mock_ai_instance.embed.return_value = [0.1] * 10
        mock_ai_cls.return_value = mock_ai_instance

        mock_repo_instance = AsyncMock()
        mock_repo_instance.search_hybrid.return_value = []
        mock_repo_cls.return_value = mock_repo_instance

        mock_curator_instance = AsyncMock()
        mock_curator_instance.curate.side_effect = AIBudgetExhaustedError()
        mock_curator_cls.return_value = mock_curator_instance

        await _run_kb_curation_async(fake_article.id)

    assert fake_article.curation_source["state"] == "failed"
    assert fake_article.curation_source["error"] == "AI budget exhausted"


async def test_resume_kb_curation_approve_publishes():
    """resume(..., 'approve') reaching END (paused=False) publishes via KBService."""
    from app.workers.tasks_kb import _resume_kb_curation_async

    fake_article = _fake_pending_article()

    with (
        patch("app.database.AsyncSessionLocal") as mock_session_cls,
        patch("app.services.ai.ai_service.AIService") as mock_ai_cls,
        patch("app.services.ai.kb_curation_checkpointer.get_checkpointer", _fake_checkpointer),
        patch("app.services.ai.kb_curator.KBCurator") as mock_curator_cls,
        patch("app.services.kb_service.KBService") as mock_kb_service_cls,
    ):
        mock_session_cls.return_value = _mock_db_session(fake_article)
        mock_ai_cls.return_value = AsyncMock()

        mock_curator_instance = AsyncMock()
        mock_curator_instance.resume.return_value = {
            "paused": False,
            "title": "How Refunds Are Processed",
            "body": "## Overview\n...",
            "citations": [],
            "lint_findings": [],
        }
        mock_curator_cls.return_value = mock_curator_instance

        mock_kb_service_instance = AsyncMock()
        mock_kb_service_cls.return_value = mock_kb_service_instance

        await _resume_kb_curation_async(fake_article.id, "approve", None)

    assert fake_article.curation_source["state"] == "published"
    mock_kb_service_instance.publish_article.assert_called_once()
    _, kwargs = mock_kb_service_instance.publish_article.call_args
    assert kwargs["actor_roles"] == ["system"]


async def test_resume_kb_curation_reject_resynthesize_pauses_again():
    """resume(..., 'reject', notes) looping back to write_sections (paused=True again)
    updates the draft content but does NOT publish or archive."""
    from app.workers.tasks_kb import _resume_kb_curation_async

    fake_article = _fake_pending_article()

    with (
        patch("app.database.AsyncSessionLocal") as mock_session_cls,
        patch("app.services.ai.ai_service.AIService") as mock_ai_cls,
        patch("app.services.ai.kb_curation_checkpointer.get_checkpointer", _fake_checkpointer),
        patch("app.services.ai.kb_curator.KBCurator") as mock_curator_cls,
        patch("app.services.kb_service.KBService") as mock_kb_service_cls,
    ):
        mock_session_cls.return_value = _mock_db_session(fake_article)
        mock_ai_cls.return_value = AsyncMock()

        mock_curator_instance = AsyncMock()
        mock_curator_instance.resume.return_value = {
            "paused": True,
            "title": "How Refunds Are Processed (revised)",
            "body": "## Overview\nRevised with the international-shipping window.",
            "citations": ["within 5-7 business days"],
            "lint_findings": [],
        }
        mock_curator_cls.return_value = mock_curator_instance
        mock_kb_service_cls.return_value = AsyncMock()

        await _resume_kb_curation_async(fake_article.id, "reject", "Add the shipping window.")

    assert fake_article.title == "How Refunds Are Processed (revised)"
    assert fake_article.curation_source["state"] == "ready_for_review"
    assert fake_article.curation_source["reviewer_notes"] == "Add the shipping window."
    mock_kb_service_cls.return_value.publish_article.assert_not_called()


# ===========================================================================
# auto_draft_kb_from_tickets — Phase 2: ticket-resolved trigger
# ===========================================================================


async def test_auto_draft_kb_from_tickets_creates_pending_article_and_dispatches():
    """Replaces the old KBDrafter single-call path: creates a pending KBArticle
    (curation_source.trigger='ticket_resolved') and dispatches run_kb_curation —
    does not import/call KBDrafter (retired)."""
    from app.workers.tasks_kb import _auto_draft_kb_from_tickets_async

    tenant_id = uuid4()
    ticket_id = uuid4()
    space_id = uuid4()

    fake_tenant_result = MagicMock()
    fake_tenant_result.all.return_value = [(tenant_id,)]

    fake_ticket = MagicMock()
    fake_ticket.id = ticket_id
    fake_ticket.title = "VPN disconnects every 10 minutes"
    fake_ticket.description = "Customer reports repeated VPN drops on Windows."
    fake_ticket.category = None
    fake_ticket.resolution_note = "Updated VPN client to latest version."
    fake_ticket.assignee_id = None
    fake_ticket.requester_id = uuid4()

    fake_tickets_result = MagicMock()
    fake_tickets_result.unique.return_value.scalars.return_value.all.return_value = [fake_ticket]

    fake_space = MagicMock()
    fake_space.id = space_id

    fake_space_result = MagicMock()
    fake_space_result.scalar_one_or_none.return_value = fake_space

    fake_slug_check_result = MagicMock()
    fake_slug_check_result.scalar_one_or_none.return_value = None  # no slug collision

    tenant_session = AsyncMock()
    tenant_session.__aenter__ = AsyncMock(return_value=tenant_session)
    tenant_session.__aexit__ = AsyncMock(return_value=False)
    tenant_session.execute = AsyncMock(return_value=fake_tenant_result)

    work_session = AsyncMock()
    work_session.__aenter__ = AsyncMock(return_value=work_session)
    work_session.__aexit__ = AsyncMock(return_value=False)
    work_session.execute = AsyncMock(
        side_effect=[fake_tickets_result, fake_space_result, fake_slug_check_result]
    )

    with (
        patch("app.database.AsyncSessionLocal") as mock_session_cls,
        patch("app.workers.tasks_kb.run_kb_curation") as mock_run_kb_curation,
    ):
        mock_session_cls.side_effect = [tenant_session, work_session]
        mock_run_kb_curation.delay = MagicMock()

        await _auto_draft_kb_from_tickets_async()

    assert work_session.add.call_count == 2  # article + TicketKBLink
    added_article = work_session.add.call_args_list[0].args[0]
    assert added_article.curation_source["trigger"] == "ticket_resolved"
    assert added_article.curation_source["state"] == "running"
    assert added_article.curation_source["source_ticket_id"] == str(ticket_id)
    assert added_article.status.value == "ai_curated_pending_review"

    mock_run_kb_curation.delay.assert_called_once()
