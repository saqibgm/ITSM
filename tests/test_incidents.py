"""Incidents (Phase 8 / S8.3) — declare, lifecycle, roles, timeline, status updates, links."""
import pytest


async def _declare(client, **over):
    body = {"title": "Payments outage", **over}
    r = await client.post("/api/v1/incidents", json=body)
    assert r.status_code == 201, r.text
    return r.json()


@pytest.mark.asyncio
async def test_declare_gets_ir_number_and_timeline(agent_client):
    inc = await _declare(agent_client)
    assert inc["incident_number"].startswith("IR-")
    assert inc["status"] == "declared"
    tl = await agent_client.get(f"/api/v1/incidents/{inc['id']}/timeline")
    assert any(e["event_type"] == "declared" for e in tl.json()["events"])


@pytest.mark.asyncio
async def test_lifecycle_valid_and_invalid_transitions(agent_client):
    inc = await _declare(agent_client)
    # declared → resolved is NOT allowed
    bad = await agent_client.post(f"/api/v1/incidents/{inc['id']}/change-status", json={"status": "resolved"})
    assert bad.status_code == 409, bad.text
    # declared → investigating → monitoring (mitigated) → resolved (resolved)
    r1 = await agent_client.post(f"/api/v1/incidents/{inc['id']}/change-status", json={"status": "investigating"})
    assert r1.status_code == 200
    r2 = await agent_client.post(f"/api/v1/incidents/{inc['id']}/change-status", json={"status": "monitoring"})
    assert r2.status_code == 200 and r2.json()["mitigated_at"] is not None
    r3 = await agent_client.post(f"/api/v1/incidents/{inc['id']}/change-status", json={"status": "resolved"})
    assert r3.status_code == 200 and r3.json()["resolved_at"] is not None


@pytest.mark.asyncio
async def test_assign_role_and_reassign(agent_client, test_agent_user_id, test_admin_user_id):
    inc = await _declare(agent_client)
    r = await agent_client.post(f"/api/v1/incidents/{inc['id']}/assign-role",
                                json={"role": "ic", "user_id": str(test_agent_user_id)})
    assert r.status_code == 200
    roles = (await agent_client.get(f"/api/v1/incidents/{inc['id']}/roles")).json()["roles"]
    assert any(x["role"] == "ic" and x["user_id"] == str(test_agent_user_id) for x in roles)
    # reassign IC → single holder
    await agent_client.post(f"/api/v1/incidents/{inc['id']}/assign-role",
                            json={"role": "ic", "user_id": str(test_admin_user_id)})
    roles = (await agent_client.get(f"/api/v1/incidents/{inc['id']}/roles")).json()["roles"]
    ic = [x for x in roles if x["role"] == "ic"]
    assert len(ic) == 1 and ic[0]["user_id"] == str(test_admin_user_id)


@pytest.mark.asyncio
async def test_status_updates(agent_client):
    inc = await _declare(agent_client)
    r = await agent_client.post(f"/api/v1/incidents/{inc['id']}/status-updates",
                                json={"body": "Investigating elevated errors", "audience": "stakeholder"})
    assert r.status_code == 200
    lst = await agent_client.get(f"/api/v1/incidents/{inc['id']}/status-updates")
    assert any(u["body"].startswith("Investigating") for u in lst.json()["updates"])


@pytest.mark.asyncio
async def test_declare_from_alert_links_both_ways(agent_client):
    alert = (await agent_client.post("/api/v1/alerts", json={"dedup_key": "inc-alert-1", "title": "down"})).json()
    inc = await _declare(agent_client, source_alert_id=alert["id"])
    assert inc["source_alert_id"] == alert["id"]
    a2 = await agent_client.get(f"/api/v1/alerts/{alert['id']}")
    assert a2.json()["incident_id"] == inc["id"]


@pytest.mark.asyncio
async def test_blast_radius_lists_services(tenant_admin_client, agent_client):
    svc = (await tenant_admin_client.post("/api/v1/services", json={"name": "Checkout"})).json()
    inc = await _declare(agent_client, affected_service_ids=[svc["id"]])
    br = await agent_client.get(f"/api/v1/incidents/{inc['id']}/blast-radius")
    assert br.status_code == 200
    assert any(s["id"] == svc["id"] for s in br.json()["affected_services"])


@pytest.mark.asyncio
async def test_unauthenticated_rejected(async_client):
    assert (await async_client.get("/api/v1/incidents")).status_code in (401, 403)
