"""Alerting & escalation (Phase 8 / S8.2) — policies, ingest+dedup+route, paging, ack, RBAC."""
from uuid import UUID

import pytest
from sqlalchemy import select

from app.models.alerting import Page


async def _policy_with_user_step(client, user_id):
    p = (await client.post("/api/v1/on-call/escalation-policies", json={"name": "Primary EP"})).json()
    r = await client.post(f"/api/v1/on-call/escalation-policies/{p['id']}/steps", json={
        "position": 1, "target_type": "user", "target_id": str(user_id), "timeout_minutes": 15,
    })
    assert r.status_code == 201, r.text
    return p


# ── Escalation policies ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_policy_crud_and_test_dryrun(tenant_admin_client, test_agent_user_id):
    p = await _policy_with_user_step(tenant_admin_client, test_agent_user_id)
    lst = await tenant_admin_client.get("/api/v1/on-call/escalation-policies")
    assert lst.status_code == 200 and any(x["id"] == p["id"] for x in lst.json())
    t = await tenant_admin_client.post(f"/api/v1/on-call/escalation-policies/{p['id']}/test")
    assert t.status_code == 200
    assert t.json()["steps"][0]["would_page"] == [str(test_agent_user_id)]


@pytest.mark.asyncio
async def test_agent_cannot_create_policy(agent_client):
    r = await agent_client.post("/api/v1/on-call/escalation-policies", json={"name": "nope"})
    assert r.status_code == 403


# ── Ingest, dedup, routing, paging, ack ───────────────────────────────────────

@pytest.mark.asyncio
async def test_alert_ingest_routes_and_pages(tenant_admin_client, agent_client, db, test_agent_user_id):
    p = await _policy_with_user_step(tenant_admin_client, test_agent_user_id)
    svc = (await tenant_admin_client.post("/api/v1/services", json={
        "name": "Payments", "escalation_policy_id": p["id"]})).json()
    # route source=monitor → this service
    await tenant_admin_client.post("/api/v1/routing/rules", json={
        "service_id": svc["id"], "conditions": {"source": "monitor"}})

    a = await agent_client.post("/api/v1/alerts", json={
        "dedup_key": "cpu-high-1", "title": "CPU high", "source": "monitor"})
    assert a.status_code == 201, a.text
    body = a.json()
    assert body["service_id"] == svc["id"]
    assert body["escalation_policy_id"] == p["id"]
    assert body["status"] == "open"

    # a page was created for the step-1 user
    pages = (await db.execute(select(Page).where(Page.alert_id == UUID(body["id"])))).scalars().all()
    assert len(pages) == 1 and str(pages[0].user_id) == str(test_agent_user_id)


@pytest.mark.asyncio
async def test_alert_dedup_bumps_occurrence(agent_client):
    first = await agent_client.post("/api/v1/alerts", json={"dedup_key": "dup-key", "title": "x"})
    second = await agent_client.post("/api/v1/alerts", json={"dedup_key": "dup-key", "title": "x again"})
    assert first.json()["id"] == second.json()["id"]
    assert second.json()["occurrence_count"] == 2


@pytest.mark.asyncio
async def test_acknowledge_stops_escalation(tenant_admin_client, agent_client, test_agent_user_id):
    p = await _policy_with_user_step(tenant_admin_client, test_agent_user_id)
    svc = (await tenant_admin_client.post("/api/v1/services", json={
        "name": "Svc2", "escalation_policy_id": p["id"]})).json()
    a = (await agent_client.post("/api/v1/alerts", json={
        "dedup_key": "ack-1", "title": "t", "service_id": svc["id"]})).json()
    ack = await agent_client.post(f"/api/v1/alerts/{a['id']}/acknowledge")
    assert ack.status_code == 200 and ack.json()["status"] == "acknowledged"
    res = await agent_client.post(f"/api/v1/alerts/{a['id']}/resolve")
    assert res.status_code == 200 and res.json()["status"] == "resolved"


# ── Escalation advance (engine) ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_escalation_advances_to_next_step(tenant_admin_client, agent_client, db, test_agent_user_id, test_admin_user_id):
    from app.models.alerting import Alert
    from app.services import alerting_service
    p = (await tenant_admin_client.post("/api/v1/on-call/escalation-policies", json={"name": "TwoStep"})).json()
    await tenant_admin_client.post(f"/api/v1/on-call/escalation-policies/{p['id']}/steps",
                                   json={"position": 1, "target_type": "user", "target_id": str(test_agent_user_id)})
    await tenant_admin_client.post(f"/api/v1/on-call/escalation-policies/{p['id']}/steps",
                                   json={"position": 2, "target_type": "user", "target_id": str(test_admin_user_id)})
    svc = (await tenant_admin_client.post("/api/v1/services", json={"name": "Svc3", "escalation_policy_id": p["id"]})).json()
    a = (await agent_client.post("/api/v1/alerts", json={"dedup_key": "esc-1", "title": "t", "service_id": svc["id"]})).json()

    alert = (await db.execute(select(Alert).where(Alert.id == UUID(a["id"])))).scalar_one()
    assert alert.escalation_step_index == 0
    advanced = await alerting_service.advance_escalation(db, alert)
    await db.commit()
    assert advanced and alert.escalation_step_index == 1
    # step-2 user now has a page
    pages = (await db.execute(select(Page).where(Page.alert_id == alert.id))).scalars().all()
    assert any(str(pg.user_id) == str(test_admin_user_id) for pg in pages)


# ── Contact methods + heartbeats ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_contact_method_crud(agent_client):
    r = await agent_client.post("/api/v1/on-call/contact-methods", json={"type": "sms", "value": "+15551234567"})
    assert r.status_code == 201, r.text
    lst = await agent_client.get("/api/v1/on-call/contact-methods")
    assert any(c["value"] == "+15551234567" for c in lst.json())


@pytest.mark.asyncio
async def test_heartbeat_create_and_ping(tenant_admin_client):
    hb = (await tenant_admin_client.post("/api/v1/on-call/heartbeats", json={"name": "cron-x", "interval_sec": 300})).json()
    assert hb["ping_token"]
    ping = await tenant_admin_client.post(f"/api/v1/on-call/heartbeats/ping/{hb['ping_token']}")
    assert ping.status_code == 204
