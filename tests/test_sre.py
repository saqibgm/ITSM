"""SRE analytics + post-incident review (Phase 8 / S8.5)."""
import pytest


async def _declare(client, **over):
    r = await client.post("/api/v1/incidents", json={"title": "outage", **over})
    assert r.status_code == 201, r.text
    return r.json()


async def _resolve(client, iid):
    for s in ("investigating", "monitoring", "resolved"):
        await client.post(f"/api/v1/incidents/{iid}/change-status", json={"status": s})


# ── Retrospective + PIR draft ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_retrospective_upsert_and_get(agent_client):
    inc = await _declare(agent_client)
    up = await agent_client.put(f"/api/v1/incidents/{inc['id']}/retrospective",
                                json={"summary": "root cause X", "status": "in_review"})
    assert up.status_code == 200 and up.json()["summary"] == "root cause X"
    got = await agent_client.get(f"/api/v1/incidents/{inc['id']}/retrospective")
    assert got.json()["status"] == "in_review"


@pytest.mark.asyncio
async def test_pir_draft_generated(agent_client):
    inc = await _declare(agent_client)
    await _resolve(agent_client, inc["id"])
    r = await agent_client.post(f"/api/v1/incidents/{inc['id']}/pir-draft")
    assert r.status_code == 200
    body = r.json()
    assert inc["incident_number"] in body["summary"]
    assert body["model_version"] == "template-v1"
    assert isinstance(body["action_items"], list) and body["action_items"]


# ── Action items + push-to-ticket ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_action_item_push_to_ticket(agent_client):
    inc = await _declare(agent_client)
    item = (await agent_client.post(f"/api/v1/incidents/{inc['id']}/retrospective/action-items",
                                    json={"description": "Add DB failover"})).json()
    push = await agent_client.post(f"/api/v1/incidents/retro-action-items/{item['id']}/push-to-ticket")
    assert push.status_code == 200
    tid = push.json()["ticket_id"]
    assert tid
    # the created ticket exists
    t = await agent_client.get(f"/api/v1/tickets/{tid}")
    assert t.status_code == 200 and "Follow-up" in t.json()["title"]
    # action item now links the ticket
    items = (await agent_client.get(f"/api/v1/incidents/{inc['id']}/retrospective/action-items")).json()["items"]
    assert any(i["ticket_id"] == tid for i in items)


# ── Analytics ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_incident_mttr_report(agent_client):
    inc = await _declare(agent_client)
    await _resolve(agent_client, inc["id"])
    r = await agent_client.get("/api/v1/incidents/reports/mttr")
    assert r.status_code == 200
    body = r.json()
    assert body["incidents"] >= 1
    assert body["mttr_seconds"] is not None  # at least one resolved incident
    assert "by_status" in body


@pytest.mark.asyncio
async def test_oncall_load_and_alert_noise(tenant_admin_client, agent_client, test_agent_user_id):
    # generate a page via an escalation
    p = (await tenant_admin_client.post("/api/v1/on-call/escalation-policies", json={"name": "EP"})).json()
    await tenant_admin_client.post(f"/api/v1/on-call/escalation-policies/{p['id']}/steps",
                                   json={"position": 1, "target_type": "user", "target_id": str(test_agent_user_id)})
    svc = (await tenant_admin_client.post("/api/v1/services", json={"name": "S", "escalation_policy_id": p["id"]})).json()
    await agent_client.post("/api/v1/alerts", json={"dedup_key": "n1", "title": "x", "service_id": svc["id"]})
    await agent_client.post("/api/v1/alerts", json={"dedup_key": "n1", "title": "x"})  # dedup bump

    load = await agent_client.get("/api/v1/on-call/reports/load")
    assert load.status_code == 200
    assert any(r["user_id"] == str(test_agent_user_id) for r in load.json()["load"])

    noise = await agent_client.get("/api/v1/alerts/reports/noise")
    assert noise.status_code == 200 and noise.json()["alerts"] >= 1


@pytest.mark.asyncio
async def test_suggest_responder(tenant_admin_client, agent_client):
    svc = (await tenant_admin_client.post("/api/v1/services", json={"name": "Svc"})).json()
    sch = (await tenant_admin_client.post("/api/v1/on-call/schedules", json={
        "name": "Sch", "rotation_type": "weekly", "start_at": "2026-01-01T00:00:00+00:00"})).json()
    from uuid import uuid4
    uid = str(uuid4())
    await tenant_admin_client.post(f"/api/v1/on-call/schedules/{sch['id']}/layers",
                                   json={"layer_rank": 1, "participants": [uid]})
    inc = await _declare(agent_client, affected_service_ids=[svc["id"]])
    r = await agent_client.get(f"/api/v1/incidents/{inc['id']}/suggest-responder")
    assert r.status_code == 200
    assert r.json()["suggested_user_id"] == uid
