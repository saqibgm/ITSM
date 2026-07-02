"""Status page, maintenance suppression & workflows (Phase 8 / S8.4)."""
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from sqlalchemy import select

from app.models.alerting import Page


def _iso(dt):
    return dt.isoformat()


# ── Status page ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_status_page_config_and_public_read(tenant_admin_client, agent_client, async_client):
    # publish a page
    cfg = await tenant_admin_client.put("/api/v1/status-page", json={"slug": "acme-status", "is_public": True})
    assert cfg.status_code == 200, cfg.text
    # a service to show
    await tenant_admin_client.post("/api/v1/services", json={"name": "Web", "description": "site"})
    # declare a public incident + public update
    inc = (await agent_client.post("/api/v1/incidents", json={"title": "Elevated errors"})).json()
    await agent_client.post(f"/api/v1/incidents/{inc['id']}/status-updates",
                            json={"body": "We are investigating", "audience": "public"})

    # unauthenticated public read
    pub = await async_client.get("/api/v1/status/acme-status")
    assert pub.status_code == 200, pub.text
    body = pub.json()
    assert any(s["name"] == "Web" for s in body["services"])
    assert any(i["title"] == "Elevated errors" for i in body["active_incidents"])

    # public subscribe (unauth)
    sub = await async_client.post("/api/v1/status/acme-status/subscribe",
                                  json={"channel": "email", "value": "user@example.com"})
    assert sub.status_code == 201


@pytest.mark.asyncio
async def test_private_status_page_not_public(tenant_admin_client, async_client):
    await tenant_admin_client.put("/api/v1/status-page", json={"slug": "hidden-status", "is_public": False})
    assert (await async_client.get("/api/v1/status/hidden-status")).status_code in (403, 404)


# ── Maintenance suppression ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_maintenance_window_suppresses_paging(tenant_admin_client, agent_client, db, test_agent_user_id):
    # policy + service
    p = (await tenant_admin_client.post("/api/v1/on-call/escalation-policies", json={"name": "EP"})).json()
    await tenant_admin_client.post(f"/api/v1/on-call/escalation-policies/{p['id']}/steps",
                                   json={"position": 1, "target_type": "user", "target_id": str(test_agent_user_id)})
    svc = (await tenant_admin_client.post("/api/v1/services", json={"name": "Db", "escalation_policy_id": p["id"]})).json()

    now = datetime.now(timezone.utc)
    await tenant_admin_client.post("/api/v1/maintenance-windows", json={
        "title": "DB upgrade", "service_ids": [svc["id"]],
        "start_at": _iso(now - timedelta(hours=1)), "end_at": _iso(now + timedelta(hours=1)),
        "suppress_alerts": True})

    a = (await agent_client.post("/api/v1/alerts", json={
        "dedup_key": "maint-1", "title": "db down", "service_id": svc["id"]})).json()
    # suppressed → no escalation policy engaged, no page
    assert a["escalation_policy_id"] is None
    pages = (await db.execute(select(Page).where(Page.alert_id == UUID(a["id"])))).scalars().all()
    assert len(pages) == 0


@pytest.mark.asyncio
async def test_no_maintenance_still_pages(tenant_admin_client, agent_client, db, test_agent_user_id):
    p = (await tenant_admin_client.post("/api/v1/on-call/escalation-policies", json={"name": "EP2"})).json()
    await tenant_admin_client.post(f"/api/v1/on-call/escalation-policies/{p['id']}/steps",
                                   json={"position": 1, "target_type": "user", "target_id": str(test_agent_user_id)})
    svc = (await tenant_admin_client.post("/api/v1/services", json={"name": "Api", "escalation_policy_id": p["id"]})).json()
    a = (await agent_client.post("/api/v1/alerts", json={
        "dedup_key": "nomaint-1", "title": "api down", "service_id": svc["id"]})).json()
    assert a["escalation_policy_id"] == p["id"]
    pages = (await db.execute(select(Page).where(Page.alert_id == UUID(a["id"])))).scalars().all()
    assert len(pages) == 1


# ── Workflows ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_workflow_runs_on_incident_declared(tenant_admin_client, agent_client):
    wf = (await tenant_admin_client.post("/api/v1/workflows", json={
        "name": "Announce", "trigger": "incident_declared", "conditions": {},
        "actions": [{"type": "annotate", "note": "auto"}]})).json()
    # declaring an incident fires the workflow
    await agent_client.post("/api/v1/incidents", json={"title": "boom"})
    runs = await tenant_admin_client.get(f"/api/v1/workflows/{wf['id']}/runs")
    assert runs.status_code == 200
    assert any(r["status"] == "success" for r in runs.json()["runs"])


@pytest.mark.asyncio
async def test_workflow_dry_run_applies_nothing(tenant_admin_client):
    wf = (await tenant_admin_client.post("/api/v1/workflows", json={
        "name": "DryWF", "trigger": "incident_declared", "conditions": {"title": "match-me"},
        "actions": [{"type": "notify", "message": "x"}]})).json()
    r = await tenant_admin_client.post(f"/api/v1/workflows/{wf['id']}/dry-run",
                                       json={"context": {"title": "match-me"}})
    assert r.status_code == 200 and r.json()["matched"] is True
    # no runs were logged by a dry-run
    runs = await tenant_admin_client.get(f"/api/v1/workflows/{wf['id']}/runs")
    assert runs.json()["runs"] == []
