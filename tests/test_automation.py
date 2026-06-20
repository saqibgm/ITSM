"""
Integration tests for the Automation Rules Engine (/api/v1/automation).

Uses the shared fixtures from conftest:
  - tenant_admin_client — admin role; can create/update/delete rules
  - agent_client        — agent role; can list/view rules but not mutate
  - seeded_tenant       — ensures tenant + user rows exist in the DB
"""

from unittest.mock import patch
from uuid import uuid4

import pytest

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rule_body(**overrides) -> dict:
    return {
        "name": "Auto-escalate critical incidents",
        "trigger": "ticket_created",
        "conditions": [
            {"field": "priority", "op": "eq", "value": "critical"}
        ],
        "actions": [
            {"type": "set_priority", "priority": "critical"}
        ],
        "is_active": True,
        **overrides,
    }


async def _create_rule(client) -> dict:
    resp = await client.post("/api/v1/automation/rules", json=_rule_body())
    assert resp.status_code == 201, resp.text
    return resp.json()


# ===========================================================================
# GET /api/v1/automation/rules
# ===========================================================================


async def test_list_rules_empty_returns_200(agent_client):
    resp = await agent_client.get("/api/v1/automation/rules")
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body
    assert isinstance(body["items"], list)


async def test_list_rules_after_create(tenant_admin_client, agent_client):
    await _create_rule(tenant_admin_client)
    resp = await agent_client.get("/api/v1/automation/rules")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) >= 1


async def test_list_rules_filter_by_trigger(tenant_admin_client, agent_client):
    await _create_rule(tenant_admin_client)
    resp = await agent_client.get(
        "/api/v1/automation/rules?trigger=ticket_created"
    )
    assert resp.status_code == 200
    for item in resp.json()["items"]:
        assert item["trigger"] == "ticket_created"


async def test_list_rules_filter_by_is_active(tenant_admin_client, agent_client):
    await _create_rule(tenant_admin_client)
    resp = await agent_client.get("/api/v1/automation/rules?is_active=true")
    assert resp.status_code == 200
    for item in resp.json()["items"]:
        assert item["is_active"] is True


async def test_list_rules_unauthenticated_returns_401(async_client):
    resp = await async_client.get("/api/v1/automation/rules")
    assert resp.status_code == 401


# ===========================================================================
# POST /api/v1/automation/rules
# ===========================================================================


async def test_create_rule_returns_201(tenant_admin_client):
    resp = await tenant_admin_client.post(
        "/api/v1/automation/rules", json=_rule_body()
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert "id" in body
    assert body["name"] == "Auto-escalate critical incidents"
    assert body["trigger"] == "ticket_created"
    assert body["is_active"] is True
    assert body["trigger_count"] == 0


async def test_create_rule_with_empty_conditions(tenant_admin_client):
    resp = await tenant_admin_client.post(
        "/api/v1/automation/rules",
        json=_rule_body(conditions=[], actions=[{"type": "add_tag", "tag": "auto"}]),
    )
    assert resp.status_code == 201, resp.text


async def test_create_rule_missing_name_returns_422(tenant_admin_client):
    body = _rule_body()
    del body["name"]
    resp = await tenant_admin_client.post("/api/v1/automation/rules", json=body)
    assert resp.status_code == 422


async def test_create_rule_missing_trigger_returns_422(tenant_admin_client):
    body = _rule_body()
    del body["trigger"]
    resp = await tenant_admin_client.post("/api/v1/automation/rules", json=body)
    assert resp.status_code == 422


async def test_create_rule_agent_forbidden(agent_client):
    resp = await agent_client.post("/api/v1/automation/rules", json=_rule_body())
    assert resp.status_code == 403


# ===========================================================================
# GET /api/v1/automation/rules/{id}
# ===========================================================================


async def test_get_rule_returns_200(tenant_admin_client, agent_client):
    created = await _create_rule(tenant_admin_client)
    rule_id = created["id"]

    resp = await agent_client.get(f"/api/v1/automation/rules/{rule_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == rule_id
    assert body["name"] == created["name"]


async def test_get_nonexistent_rule_returns_404(agent_client):
    resp = await agent_client.get(f"/api/v1/automation/rules/{uuid4()}")
    assert resp.status_code == 404


# ===========================================================================
# PATCH /api/v1/automation/rules/{id}
# ===========================================================================


async def test_update_rule_name(tenant_admin_client):
    created = await _create_rule(tenant_admin_client)
    rule_id = created["id"]

    resp = await tenant_admin_client.patch(
        f"/api/v1/automation/rules/{rule_id}",
        json={"name": "Updated rule name"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "Updated rule name"


async def test_deactivate_rule(tenant_admin_client):
    created = await _create_rule(tenant_admin_client)
    rule_id = created["id"]

    resp = await tenant_admin_client.patch(
        f"/api/v1/automation/rules/{rule_id}", json={"is_active": False}
    )
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False


async def test_update_rule_agent_forbidden(tenant_admin_client, agent_client):
    created = await _create_rule(tenant_admin_client)
    rule_id = created["id"]

    resp = await agent_client.patch(
        f"/api/v1/automation/rules/{rule_id}", json={"name": "Hack"}
    )
    assert resp.status_code == 403


async def test_update_nonexistent_rule_returns_404(tenant_admin_client):
    resp = await tenant_admin_client.patch(
        f"/api/v1/automation/rules/{uuid4()}", json={"name": "Ghost"}
    )
    assert resp.status_code == 404


# ===========================================================================
# DELETE /api/v1/automation/rules/{id}
# ===========================================================================


async def test_delete_rule_returns_204(tenant_admin_client, agent_client):
    created = await _create_rule(tenant_admin_client)
    rule_id = created["id"]

    del_resp = await tenant_admin_client.delete(
        f"/api/v1/automation/rules/{rule_id}"
    )
    assert del_resp.status_code == 204

    # Verify it's gone
    get_resp = await agent_client.get(f"/api/v1/automation/rules/{rule_id}")
    assert get_resp.status_code == 404


async def test_delete_rule_agent_forbidden(tenant_admin_client, agent_client):
    created = await _create_rule(tenant_admin_client)
    resp = await agent_client.delete(
        f"/api/v1/automation/rules/{created['id']}"
    )
    assert resp.status_code == 403


async def test_delete_nonexistent_rule_returns_404(tenant_admin_client):
    resp = await tenant_admin_client.delete(
        f"/api/v1/automation/rules/{uuid4()}"
    )
    assert resp.status_code == 404


# ===========================================================================
# GET /api/v1/automation/rules/{id}/logs
# ===========================================================================


async def test_list_rule_logs_empty(tenant_admin_client, agent_client):
    created = await _create_rule(tenant_admin_client)
    rule_id = created["id"]

    resp = await agent_client.get(
        f"/api/v1/automation/rules/{rule_id}/logs"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body
    assert body["items"] == []


# ===========================================================================
# POST /api/v1/automation/rules/{id}/test  — dry-run
# ===========================================================================


async def test_dry_run_rule_with_matching_ticket(
    tenant_admin_client, agent_client, test_tenant_id
):
    """Dry-run should evaluate conditions against a real ticket."""
    # Create a rule that matches critical priority
    rule = await _create_rule(tenant_admin_client)
    rule_id = rule["id"]

    # Create a ticket to test against
    with patch(
        "app.workers.tasks_ai_ticket.process_new_ticket.delay", return_value=None
    ):
        ticket_resp = await tenant_admin_client.post(
            "/api/v1/tickets",
            json={
                "title": "Critical system failure",
                "description": "All services are down.",
                "type": "incident",
                "priority": "critical",
            },
        )
    assert ticket_resp.status_code == 201
    ticket_id = ticket_resp.json()["id"]

    resp = await tenant_admin_client.post(
        f"/api/v1/automation/rules/{rule_id}/test",
        json={"entity_type": "ticket", "entity_id": ticket_id},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "conditions_pass" in body
    assert "condition_results" in body
    assert "actions_would_execute" in body
    # priority=critical matches the condition
    assert body["conditions_pass"] is True


async def test_dry_run_rule_nonexistent_entity(tenant_admin_client):
    rule = await _create_rule(tenant_admin_client)
    resp = await tenant_admin_client.post(
        f"/api/v1/automation/rules/{rule['id']}/test",
        json={"entity_type": "ticket", "entity_id": str(uuid4())},
    )
    assert resp.status_code == 404


async def test_dry_run_nonexistent_rule_returns_404(tenant_admin_client, test_tenant_id):
    with patch(
        "app.workers.tasks_ai_ticket.process_new_ticket.delay", return_value=None
    ):
        ticket_resp = await tenant_admin_client.post(
            "/api/v1/tickets",
            json={
                "title": "Some ticket",
                "description": "Some description.",
                "type": "incident",
                "priority": "low",
            },
        )
    assert ticket_resp.status_code == 201
    ticket_id = ticket_resp.json()["id"]

    resp = await tenant_admin_client.post(
        f"/api/v1/automation/rules/{uuid4()}/test",
        json={"entity_type": "ticket", "entity_id": ticket_id},
    )
    assert resp.status_code == 404
