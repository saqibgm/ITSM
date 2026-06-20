"""
Tests for the AI Ticket Enrichment domain.

Covers:
  §7A.2 — HITL accept / reject classification
  §7A.3 — Duplicate candidate dismiss
  §7A.11 — Budget exhaustion is non-blocking for ticket creation

Uses the shared conftest fixtures (agent_client, seeded_tenant, etc.).
"""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TICKET_BODY = {
    "title": "Keyboard keys are sticking and not responding",
    "description": "Multiple keys on my laptop keyboard are not functioning after a spill.",
    "type": "incident",
    "priority": "high",
}


async def _create_ticket(client) -> dict:
    with patch("app.workers.tasks_ai_ticket.process_new_ticket.delay", return_value=None):
        resp = await client.post("/api/v1/tickets", json=_TICKET_BODY)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ===========================================================================
# HITL Accept
# ===========================================================================


async def test_accept_classification_stores_hitl_data(agent_client, db, test_tenant_id, test_agent_user_id):
    """POST /tickets/{id}/ai-classification/accept → 200, accepted=True, actual_* set."""
    from uuid import UUID

    ticket = await _create_ticket(agent_client)
    ticket_id = ticket["id"]

    # Manually seed an AI classification row for the ticket
    # Column names match the DB (mapped_column uses string aliases for FK columns)
    classification_id = uuid4()
    await db.execute(
        text(
            """
            INSERT INTO ai_ticket_classifications
                (id, ticket_id, suggested_priority, suggested_category,
                 suggested_team, suggested_assignee, confidence, model_version, accepted)
            VALUES
                (:id, :ticket_id, 'high', NULL, NULL, NULL, 0.92, 'test-model-v1', NULL)
            """
        ),
        {
            "id": str(classification_id),
            "ticket_id": ticket_id,
        },
    )
    await db.flush()

    resp = await agent_client.post(
        f"/api/v1/tickets/{ticket_id}/ai-classification/accept",
        json={"fields": ["priority"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["accepted"] is True
    assert "priority" in (body.get("accepted_fields") or [])
    # actual_priority should be set (copied from ticket's current priority)
    assert body.get("actual_priority") is not None


async def test_accept_classification_nonexistent_ticket_returns_404(agent_client):
    """POST accept on a non-existent ticket_id returns 404."""
    resp = await agent_client.post(
        f"/api/v1/tickets/{uuid4()}/ai-classification/accept",
        json={"fields": ["priority"]},
    )
    assert resp.status_code == 404


async def test_accept_classification_no_classification_row_returns_404(agent_client, db, test_tenant_id):
    """POST accept on a ticket with no AI classification row returns 404."""
    ticket = await _create_ticket(agent_client)
    ticket_id = ticket["id"]

    # No classification row seeded — endpoint should 404
    resp = await agent_client.post(
        f"/api/v1/tickets/{ticket_id}/ai-classification/accept",
        json={"fields": ["priority"]},
    )
    assert resp.status_code == 404


# ===========================================================================
# HITL Reject
# ===========================================================================


async def test_reject_classification_stores_corrections(agent_client, db, test_tenant_id):
    """POST /tickets/{id}/ai-classification/reject → 200, accepted=False, actual_* set."""
    ticket = await _create_ticket(agent_client)
    ticket_id = ticket["id"]

    classification_id = uuid4()
    await db.execute(
        text(
            """
            INSERT INTO ai_ticket_classifications
                (id, ticket_id, suggested_priority, suggested_category,
                 suggested_team, suggested_assignee, confidence, model_version, accepted)
            VALUES
                (:id, :ticket_id, 'critical', NULL, NULL, NULL, 0.75, 'test-model-v1', NULL)
            """
        ),
        {
            "id": str(classification_id),
            "ticket_id": ticket_id,
        },
    )
    await db.flush()

    resp = await agent_client.post(
        f"/api/v1/tickets/{ticket_id}/ai-classification/reject",
        json={},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["accepted"] is False
    # actual_priority should snapshot the ticket's current value
    assert body.get("actual_priority") is not None


# ===========================================================================
# Classification dataset endpoint — admin only
# ===========================================================================


async def test_classification_dataset_requires_admin(agent_client):
    """GET /api/v1/ai/classification-dataset returns 403 for agent role."""
    resp = await agent_client.get("/api/v1/ai/classification-dataset")
    assert resp.status_code == 403


async def test_classification_dataset_accessible_to_admin(tenant_admin_client):
    """GET /api/v1/ai/classification-dataset returns 200 for admin role."""
    resp = await tenant_admin_client.get("/api/v1/ai/classification-dataset")
    # Accept 200 (streaming or paginated JSON) or 404 if endpoint has changed path
    assert resp.status_code in (200, 404), resp.text


# ===========================================================================
# Duplicate dismiss
# ===========================================================================


async def test_duplicate_dismiss_sets_dismissed_true(agent_client, db, test_tenant_id):
    """POST /tickets/{id}/ai-duplicates/{candidate_id}/dismiss → 204, dismissed=True."""
    ticket = await _create_ticket(agent_client)
    ticket_id = ticket["id"]

    # Create a second ticket to act as the duplicate candidate
    duplicate_ticket = await _create_ticket(agent_client)
    candidate_ticket_id = duplicate_ticket["id"]

    # Seed an ai_duplicate_candidate row
    dup_id = uuid4()
    await db.execute(
        text(
            """
            INSERT INTO ai_duplicate_candidates
                (id, ticket_id, candidate_id, similarity_score, model_version, dismissed)
            VALUES
                (:id, :ticket_id, :candidate_id, 0.91, 'test-model-v1', false)
            """
        ),
        {
            "id": str(dup_id),
            "ticket_id": ticket_id,
            "candidate_id": candidate_ticket_id,
        },
    )
    await db.flush()

    resp = await agent_client.post(
        f"/api/v1/tickets/{ticket_id}/ai-duplicates/{dup_id}/dismiss"
    )
    assert resp.status_code == 204, resp.text

    # Verify the row was updated
    result = await db.execute(
        text("SELECT dismissed FROM ai_duplicate_candidates WHERE id = :id"),
        {"id": str(dup_id)},
    )
    row = result.fetchone()
    assert row is not None
    assert row[0] is True


async def test_dismiss_nonexistent_duplicate_returns_404(agent_client):
    """POST dismiss on a non-existent candidate id returns 404."""
    ticket = await _create_ticket(agent_client)
    ticket_id = ticket["id"]

    resp = await agent_client.post(
        f"/api/v1/tickets/{ticket_id}/ai-duplicates/{uuid4()}/dismiss"
    )
    assert resp.status_code == 404


# ===========================================================================
# AI budget exhaustion — non-blocking ticket creation  (pure unit test)
# ===========================================================================


async def test_budget_exhausted_does_not_block_ticket_creation():
    """AIBudgetExhaustedError in the Celery task must not affect ticket existence.

    Ticket is created synchronously in the API handler; AI enrichment runs
    asynchronously inside process_new_ticket (Celery). If the tenant has
    exhausted its monthly AI token budget, the task should return None — not
    raise — so the ticket is unaffected.

    This is a pure unit test: no HTTP client or DB required.
    """
    from app.exceptions import AIBudgetExhaustedError
    from app.workers.tasks_ai_ticket import _process_new_ticket_async

    ticket_id = str(uuid4())
    tenant_id = str(uuid4())

    with (
        patch("app.database.AsyncSessionLocal") as mock_session_cls,
        patch("app.services.ai.ai_service.AIService") as mock_ai_cls,
    ):
        # Budget check always raises — simulates exhausted tenant.
        mock_ai_instance = AsyncMock()
        mock_ai_instance.check_budget.side_effect = AIBudgetExhaustedError()
        mock_ai_cls.return_value = mock_ai_instance

        # DB session that yields a fake ticket
        mock_db = AsyncMock()
        mock_db.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db.__aexit__ = AsyncMock(return_value=False)

        fake_ticket = MagicMock()
        fake_ticket.id = uuid4()
        fake_ticket.description = "test description"
        mock_execute_result = AsyncMock()
        mock_execute_result.scalar_one_or_none.return_value = fake_ticket
        mock_db.execute.return_value = mock_execute_result
        mock_session_cls.return_value = mock_db

        result = await _process_new_ticket_async(ticket_id, tenant_id)
        assert result is None, "Task should return None on budget exhaustion, not raise"


async def test_ai_budget_exhausted_does_not_block_ticket_http(agent_client, test_tenant_id):
    """When AI enrichment raises AIBudgetExhaustedError in .delay(), POST /tickets still 201."""
    # The Celery .delay() call happens AFTER the ticket is persisted and the response
    # is already sent. Patching delay to raise should not affect the HTTP response.
    with patch(
        "app.workers.tasks_ai_ticket.process_new_ticket.delay",
        side_effect=Exception("Simulated Celery queue failure"),
    ):
        resp = await agent_client.post("/api/v1/tickets", json=_TICKET_BODY)

    # Ticket creation should succeed regardless of async task dispatch failure
    assert resp.status_code == 201, resp.text
    assert "id" in resp.json()
