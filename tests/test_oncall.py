"""On-call (Phase 8 / S8.1) — rotation resolver + services/schedules/overrides API + RBAC."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.oncall_service import participant_at, resolve_layers

_ANCHOR = datetime(2026, 1, 1, tzinfo=timezone.utc)


# ── Pure resolver ─────────────────────────────────────────────────────────────

def test_participant_rotation_index():
    u = [uuid4(), uuid4(), uuid4()]
    assert participant_at(u, _ANCHOR, 168, _ANCHOR) == u[0]              # week 0
    assert participant_at(u, _ANCHOR, 168, _ANCHOR + timedelta(days=8)) == u[1]   # week 1
    assert participant_at(u, _ANCHOR, 168, _ANCHOR + timedelta(days=22)) == u[0]  # week 3 → wrap


def test_participant_empty_roster_is_none():
    assert participant_at([], _ANCHOR, 168, _ANCHOR) is None


def test_resolve_layers_override_beats_rotation():
    u = [uuid4(), uuid4()]
    ovu = uuid4()
    sch = SimpleNamespace(start_at=_ANCHOR, created_at=_ANCHOR, rotation_type="weekly", rotation_length_hours=168)
    layers = [SimpleNamespace(layer_rank=1, participants=u), SimpleNamespace(layer_rank=2, participants=list(reversed(u)))]
    ov = [SimpleNamespace(user_id=ovu, start_at=_ANCHOR, end_at=_ANCHOR + timedelta(days=1))]
    r = resolve_layers(sch, layers, ov, _ANCHOR + timedelta(hours=2))
    prim = next(x for x in r if x["layer_rank"] == 1)
    assert prim["user_id"] == str(ovu) and prim["overridden"] is True
    # secondary layer keeps its rotation user
    sec = next(x for x in r if x["layer_rank"] == 2)
    assert sec["user_id"] == str(u[1]) and sec["overridden"] is False


# ── Services API ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_service_crud(tenant_admin_client):
    r = await tenant_admin_client.post("/api/v1/services", json={"name": "Payments API"})
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    assert (await tenant_admin_client.get("/api/v1/services")).status_code == 200
    p = await tenant_admin_client.patch(f"/api/v1/services/{sid}", json={"current_state": "degraded"})
    assert p.status_code == 200 and p.json()["current_state"] == "degraded"


@pytest.mark.asyncio
async def test_agent_cannot_write_service(agent_client):
    assert (await agent_client.get("/api/v1/services")).status_code == 200
    r = await agent_client.post("/api/v1/services", json={"name": "nope"})
    assert r.status_code == 403


# ── Schedules + who-is-on-call + override ─────────────────────────────────────

@pytest.mark.asyncio
async def test_schedule_layer_and_who_is_on_call(tenant_admin_client, agent_client):
    sch = (await tenant_admin_client.post("/api/v1/on-call/schedules", json={
        "name": "Primary", "rotation_type": "weekly", "start_at": "2026-01-01T00:00:00+00:00",
    })).json()
    uA, uB = str(uuid4()), str(uuid4())
    lr = await tenant_admin_client.post(f"/api/v1/on-call/schedules/{sch['id']}/layers",
                                        json={"layer_rank": 1, "participants": [uA, uB]})
    assert lr.status_code == 201, lr.text

    # week 0 → first participant
    pv = await agent_client.get(f"/api/v1/on-call/schedules/{sch['id']}/preview",
                                params={"at": "2026-01-01T02:00:00+00:00"})
    assert pv.status_code == 200
    assert pv.json()["primary_user_id"] == uA

    # week 1 → second participant
    pv2 = await agent_client.get(f"/api/v1/on-call/schedules/{sch['id']}/preview",
                                 params={"at": "2026-01-09T02:00:00+00:00"})
    assert pv2.json()["primary_user_id"] == uB


@pytest.mark.asyncio
async def test_override_changes_primary(tenant_admin_client, agent_client, test_agent_user_id):
    sch = (await tenant_admin_client.post("/api/v1/on-call/schedules", json={
        "name": "OverrideSched", "rotation_type": "weekly", "start_at": "2026-01-01T00:00:00+00:00",
    })).json()
    await tenant_admin_client.post(f"/api/v1/on-call/schedules/{sch['id']}/layers",
                                   json={"layer_rank": 1, "participants": [str(uuid4()), str(uuid4())]})
    ov = await tenant_admin_client.post("/api/v1/on-call/overrides", json={
        "schedule_id": sch["id"], "user_id": str(test_agent_user_id),
        "start_at": "2026-01-01T00:00:00+00:00", "end_at": "2026-01-02T00:00:00+00:00",
    })
    assert ov.status_code == 201, ov.text
    pv = await agent_client.get(f"/api/v1/on-call/schedules/{sch['id']}/preview",
                                params={"at": "2026-01-01T06:00:00+00:00"})
    assert pv.json()["primary_user_id"] == str(test_agent_user_id)


@pytest.mark.asyncio
async def test_who_is_on_call_requires_selector(agent_client):
    r = await agent_client.get("/api/v1/on-call/who-is-on-call")
    assert r.status_code in (400, 422)


@pytest.mark.asyncio
async def test_severity_crud(tenant_admin_client, agent_client):
    r = await tenant_admin_client.post("/api/v1/on-call/severities", json={"name": "SEV1", "rank": 1, "auto_page": True})
    assert r.status_code == 201, r.text
    assert any(s["name"] == "SEV1" for s in (await agent_client.get("/api/v1/on-call/severities")).json())
