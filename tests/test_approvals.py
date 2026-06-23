"""Approval workflow: pending_approval opens an approval, decision endpoint
drives approve/reject with authz, and the direct status PATCH is blocked."""
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.asyncio


def _change_body(**o):
    return {
        "title": "Change: upgrade prod DB",
        "description": "Requires CAB approval before the maintenance window.",
        "type": "change",
        "priority": "high",
        **o,
    }


async def _new_change_ticket(client):
    with patch("app.workers.tasks_ai_ticket.process_new_ticket.delay", return_value=None):
        r = await client.post("/api/v1/tickets", json=_change_body())
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _to_pending(client, tid):
    return await client.patch(f"/api/v1/tickets/{tid}", json={"status": "pending_approval"})


# ── PATCH guard ───────────────────────────────────────────────────────────────
async def test_direct_status_approved_is_blocked(agent_client):
    tid = await _new_change_ticket(agent_client)
    await _to_pending(agent_client, tid)
    r = await agent_client.patch(f"/api/v1/tickets/{tid}", json={"status": "approved"})
    assert r.status_code in (400, 422), r.text          # must use the approval endpoint


# ── Decision endpoint ───────────────────────────────────────────────────────
async def test_manager_can_approve(agent_client, tenant_admin_client):
    tid = await _new_change_ticket(agent_client)
    p = await _to_pending(agent_client, tid)
    assert p.status_code in (200, 409), p.text
    r = await tenant_admin_client.post(f"/api/v1/tickets/{tid}/approval",
                                       json={"decision": "approve", "comment": "CAB ok"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved"


async def test_manager_can_reject(agent_client, tenant_admin_client):
    tid = await _new_change_ticket(agent_client)
    await _to_pending(agent_client, tid)
    r = await tenant_admin_client.post(f"/api/v1/tickets/{tid}/approval", json={"decision": "reject"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "rejected"


async def test_agent_cannot_decide(agent_client):
    tid = await _new_change_ticket(agent_client)
    await _to_pending(agent_client, tid)
    r = await agent_client.post(f"/api/v1/tickets/{tid}/approval", json={"decision": "approve"})
    assert r.status_code == 403, r.text                  # agent is neither manager nor approver


async def test_approval_requires_pending(agent_client, tenant_admin_client):
    tid = await _new_change_ticket(agent_client)          # still 'open', no pending approval
    r = await tenant_admin_client.post(f"/api/v1/tickets/{tid}/approval", json={"decision": "approve"})
    assert r.status_code in (400, 422), r.text


async def test_bad_decision_value_422(agent_client, tenant_admin_client):
    tid = await _new_change_ticket(agent_client)
    await _to_pending(agent_client, tid)
    r = await tenant_admin_client.post(f"/api/v1/tickets/{tid}/approval", json={"decision": "maybe"})
    assert r.status_code == 422, r.text
