"""SLA runtime (Phase 7 / S7.2) — coverage-window math, instance lifecycle API,
pause/resume, and breach attribution."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models.sla import SLAInstance
from app.services import slm_service, sla_runtime


# ---------------------------------------------------------------------------
# Coverage-window minute math (pure)
# ---------------------------------------------------------------------------


def _dt(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


def test_cw_none_is_plain_wallclock():
    assert slm_service.add_working_minutes_cw(_dt(2026, 7, 1, 10), 120, None) == _dt(2026, 7, 1, 12)


def test_cw_247_is_plain_wallclock():
    cw = SimpleNamespace(timezone="UTC", is_247=True, work_days=[1, 2, 3, 4, 5], windows=[], holidays=[])
    assert slm_service.add_working_minutes_cw(_dt(2026, 7, 1, 10), 600, cw) == _dt(2026, 7, 1, 20)


def test_cw_split_shift_skips_lunch():
    cw = SimpleNamespace(timezone="UTC", is_247=False, work_days=[1, 2, 3, 4, 5],
                         windows=[{"start": "09:00", "end": "12:00"}, {"start": "13:00", "end": "17:00"}],
                         holidays=[])
    # Wed 2026-07-01 10:00 + 240 working min = 2h morning (→12:00) + 2h afternoon (→15:00)
    assert slm_service.add_working_minutes_cw(_dt(2026, 7, 1, 10), 240, cw) == _dt(2026, 7, 1, 15)


def test_cw_skips_weekend_and_holiday():
    from datetime import date
    cw = SimpleNamespace(timezone="UTC", is_247=False, work_days=[1, 2, 3, 4, 5],
                         windows=[{"start": "09:00", "end": "17:00"}],
                         holidays=[date(2026, 7, 6)])  # Mon 6 Jul is a holiday
    # Fri 2026-07-03 16:00 + 120 min: 1h Fri (→17:00), Sat/Sun skipped, Mon holiday skipped,
    # Tue 07 09:00 + 60 = 10:00
    assert slm_service.add_working_minutes_cw(_dt(2026, 7, 3, 16), 120, cw) == _dt(2026, 7, 7, 10)


# ---------------------------------------------------------------------------
# Helpers for API-driven setup
# ---------------------------------------------------------------------------


async def _make_agreement_with_target(client, name, minutes=240, kind="sla"):
    ag = (await client.post("/api/v1/sla/agreements", json={"name": name, "kind": kind})).json()
    await client.post(f"/api/v1/sla/agreements/{ag['id']}/targets",
                      json={"metric": "resolution", "duration_minutes": minutes})
    return ag


async def _make_ticket(agent_client):
    r = await agent_client.post("/api/v1/tickets", json={
        "title": "SLA runtime ticket", "description": "x" * 20,
        "type": "incident", "priority": "medium",
    })
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ---------------------------------------------------------------------------
# Instance lifecycle API
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_override_opens_instances_and_get_sla(tenant_admin_client, agent_client):
    ag = await _make_agreement_with_target(tenant_admin_client, "Runtime SLA")
    ticket_id = await _make_ticket(agent_client)

    r = await tenant_admin_client.post(f"/api/v1/tickets/{ticket_id}/sla/override",
                                       json={"agreement_id": ag["id"]})
    assert r.status_code == 200, r.text
    insts = r.json()["instances"]
    assert len(insts) == 1 and insts[0]["status"] == "running"
    assert insts[0]["metric"] == "resolution"
    assert insts[0]["remaining_seconds"] is not None

    # GET reflects the same instance
    g = await agent_client.get(f"/api/v1/tickets/{ticket_id}/sla")
    assert g.status_code == 200
    assert len(g.json()["instances"]) == 1

    # events include a 'started'
    ev = await agent_client.get(f"/api/v1/tickets/{ticket_id}/sla/events")
    assert ev.status_code == 200
    assert any(e["event"] == "started" for e in ev.json()["events"])


@pytest.mark.asyncio
async def test_override_again_cancels_previous(tenant_admin_client, agent_client):
    ag1 = await _make_agreement_with_target(tenant_admin_client, "First SLA")
    ag2 = await _make_agreement_with_target(tenant_admin_client, "Second SLA")
    ticket_id = await _make_ticket(agent_client)

    await tenant_admin_client.post(f"/api/v1/tickets/{ticket_id}/sla/override", json={"agreement_id": ag1["id"]})
    r = await tenant_admin_client.post(f"/api/v1/tickets/{ticket_id}/sla/override", json={"agreement_id": ag2["id"]})
    running = [i for i in r.json()["instances"] if i["status"] == "running"]
    cancelled = [i for i in r.json()["instances"] if i["status"] == "cancelled"]
    assert len(running) == 1 and len(cancelled) == 1

    ev = await agent_client.get(f"/api/v1/tickets/{ticket_id}/sla/events")
    assert any(e["event"] == "cancelled" for e in ev.json()["events"])


@pytest.mark.asyncio
async def test_ticket_create_auto_opens_instances_when_rule_matches(tenant_admin_client, agent_client):
    ag = await _make_agreement_with_target(tenant_admin_client, "Auto SLA")
    # catch-all rule (empty conditions) → matches any ticket
    r = await tenant_admin_client.post("/api/v1/sla/rules", json={"agreement_id": ag["id"], "conditions": {}})
    assert r.status_code == 201, r.text
    ticket_id = await _make_ticket(agent_client)
    g = await agent_client.get(f"/api/v1/tickets/{ticket_id}/sla")
    assert g.status_code == 200
    insts = g.json()["instances"]
    assert len(insts) == 1 and insts[0]["status"] == "running"


@pytest.mark.asyncio
async def test_ticket_create_no_rule_no_instances(agent_client):
    ticket_id = await _make_ticket(agent_client)
    g = await agent_client.get(f"/api/v1/tickets/{ticket_id}/sla")
    assert g.status_code == 200 and g.json()["instances"] == []


@pytest.mark.asyncio
async def test_agent_cannot_override(tenant_admin_client, agent_client):
    ag = await _make_agreement_with_target(tenant_admin_client, "RBAC SLA")
    ticket_id = await _make_ticket(agent_client)
    r = await agent_client.post(f"/api/v1/tickets/{ticket_id}/sla/override", json={"agreement_id": ag["id"]})
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Pause / resume engine
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pause_and_resume_pushes_due(tenant_admin_client, agent_client, db):
    ag = await _make_agreement_with_target(tenant_admin_client, "PauseResume SLA")
    ticket_id = await _make_ticket(agent_client)
    await tenant_admin_client.post(f"/api/v1/tickets/{ticket_id}/sla/override", json={"agreement_id": ag["id"]})

    from uuid import UUID
    tid = UUID(ticket_id)
    paused = await sla_runtime.pause_ticket_instances(db, tid, "waiting_on_customer")
    await db.commit()
    assert paused == 1
    inst = (await db.execute(select(SLAInstance).where(SLAInstance.ticket_id == tid))).scalars().first()
    assert inst.status == "paused" and inst.pause_reason == "waiting_on_customer"

    resumed = await sla_runtime.resume_ticket_instances(db, tid)
    await db.commit()
    assert resumed == 1
    inst = (await db.execute(select(SLAInstance).where(SLAInstance.ticket_id == tid))).scalars().first()
    assert inst.status == "running" and inst.paused_at is None


# ---------------------------------------------------------------------------
# Breach attribution via OLA underpinning
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Reporting (S7.3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_report_at_risk_lists_soon_due(tenant_admin_client, agent_client):
    ag = await _make_agreement_with_target(tenant_admin_client, "AtRisk SLA", minutes=5)
    await tenant_admin_client.post("/api/v1/sla/rules", json={"agreement_id": ag["id"], "conditions": {}})
    ticket_id = await _make_ticket(agent_client)  # auto-opens an instance due in ~5 min
    r = await agent_client.get("/api/v1/sla/reports/at-risk?window_minutes=60")
    assert r.status_code == 200
    assert any(i["ticket_id"] == ticket_id and i["ticket_number"] for i in r.json()["items"])


@pytest.mark.asyncio
async def test_report_breaches_and_overview(tenant_admin_client, agent_client, db, test_tenant_id):
    ag = await _make_agreement_with_target(tenant_admin_client, "Breach SLA")
    target_id = (await tenant_admin_client.get(
        f"/api/v1/sla/agreements/{ag['id']}")).json()["targets"][0]["id"]
    ticket_id = await _make_ticket(agent_client)
    from uuid import UUID
    tid, ttid = UUID(str(test_tenant_id)), UUID(str(ticket_id))
    db.add(SLAInstance(tenant_id=tid, ticket_id=ttid, target_id=UUID(target_id),
                       agreement_version=1, due_at=_dt(2026, 1, 1, 0),
                       status="breached", breached_at=_dt(2026, 1, 1, 0)))
    await db.commit()

    b = await agent_client.get("/api/v1/sla/reports/breaches")
    assert b.status_code == 200
    assert any(i["ticket_id"] == ticket_id for i in b.json()["items"])

    o = await agent_client.get("/api/v1/sla/reports/overview")
    assert o.status_code == 200
    assert o.json()["breached"] >= 1


@pytest.mark.asyncio
async def test_breach_attribution_to_ola_team(tenant_admin_client, agent_client, db, test_tenant_id):
    # A team to own the OLA
    team = (await tenant_admin_client.post("/api/v1/tenant/teams", json={"name": "Infra Team"})).json()
    team_id = team["id"]

    # SLA (parent) + resolution target
    sla = (await tenant_admin_client.post("/api/v1/sla/agreements", json={"name": "Cust SLA", "kind": "sla"})).json()
    p_target = (await tenant_admin_client.post(
        f"/api/v1/sla/agreements/{sla['id']}/targets",
        json={"metric": "resolution", "duration_minutes": 240})).json()

    # OLA (support) owned by the team + resolution target
    ola = (await tenant_admin_client.post("/api/v1/sla/agreements",
                                          json={"name": "Infra OLA", "kind": "ola", "owner_team_id": team_id})).json()
    s_target = (await tenant_admin_client.post(
        f"/api/v1/sla/agreements/{ola['id']}/targets",
        json={"metric": "resolution", "duration_minutes": 180})).json()

    # Link the underpinning
    await tenant_admin_client.post(f"/api/v1/sla/targets/{p_target['id']}/underpinning",
                                   json={"support_target_id": s_target["id"]})

    ticket_id = await _make_ticket(agent_client)
    from uuid import UUID
    tid, ttid = UUID(str(test_tenant_id)), UUID(str(ticket_id))

    # Seed instances: support already breached, parent breached (attribution input)
    support_inst = SLAInstance(tenant_id=tid, ticket_id=ttid, target_id=UUID(s_target["id"]),
                               agreement_version=1, due_at=_dt(2026, 1, 1, 0), status="breached")
    parent_inst = SLAInstance(tenant_id=tid, ticket_id=ttid, target_id=UUID(p_target["id"]),
                              agreement_version=1, due_at=_dt(2026, 1, 1, 0), status="breached")
    db.add_all([support_inst, parent_inst])
    await db.flush()

    party = await sla_runtime.attribute_breach(db, parent_inst)
    assert party == {"kind": "team", "id": team_id}
    assert parent_inst.attributed_party == {"kind": "team", "id": team_id}
