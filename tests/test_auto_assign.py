"""Smart auto-assignment: AutomationService._pick_least_loaded_assignee."""
import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import text

from app.services.automation_service import AutomationService

pytestmark = pytest.mark.asyncio


async def _seed_team(db, tenant_id, *user_ids):
    team_id = uuid.uuid4()
    await db.execute(
        text("INSERT INTO teams (id, tenant_id, name) VALUES (:id, :t, :n)"),
        {"id": str(team_id), "t": str(tenant_id), "n": "Support A"},
    )
    for uid in user_ids:
        await db.execute(
            text("INSERT INTO team_members (team_id, user_id) VALUES (:tm, :u)"),
            {"tm": str(team_id), "u": str(uid)},
        )
    await db.commit()
    return team_id


async def test_none_when_team_has_no_members(db, seeded_tenant, test_tenant_id):
    empty = await _seed_team(db, test_tenant_id)  # team, zero members
    picked = await AutomationService()._pick_least_loaded_assignee(db, test_tenant_id, empty)
    assert picked is None


async def test_tiebreak_when_no_load(db, seeded_tenant, test_tenant_id,
                                     test_agent_user_id, test_admin_user_id):
    team = await _seed_team(db, test_tenant_id, test_agent_user_id, test_admin_user_id)
    picked = await AutomationService()._pick_least_loaded_assignee(db, test_tenant_id, team)
    # both have 0 open tickets → deterministic tie-break = lowest user_id
    assert picked == min(test_agent_user_id, test_admin_user_id)


async def test_prefers_least_loaded(db, seeded_tenant, agent_client, test_tenant_id,
                                    test_agent_user_id, test_admin_user_id):
    team = await _seed_team(db, test_tenant_id, test_agent_user_id, test_admin_user_id)
    # Give the AGENT one open ticket via the API (handles ticket_number etc.)
    with patch("app.workers.tasks_ai_ticket.process_new_ticket.delay", return_value=None):
        r = await agent_client.post("/api/v1/tickets", json={
            "title": "Printer down", "description": "Floor 4 printer offline.",
            "type": "incident", "priority": "medium",
        })
    assert r.status_code == 201, r.text
    tid = r.json()["id"]
    a = await agent_client.patch(f"/api/v1/tickets/{tid}", json={"assignee_id": str(test_agent_user_id)})
    assert a.status_code == 200, a.text

    picked = await AutomationService()._pick_least_loaded_assignee(db, test_tenant_id, team)
    assert picked == test_admin_user_id    # admin has 0 open, agent has 1
