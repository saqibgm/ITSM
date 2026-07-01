"""SLM (Phase 7 / S7.1) — matcher unit tests + agreements/targets/rules/coverage API + RBAC."""
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services import slm_service


# ---------------------------------------------------------------------------
# Pure matcher unit tests (no DB)
# ---------------------------------------------------------------------------


def _rule(position, conditions, is_active=True):
    return SimpleNamespace(
        id=uuid4(), position=position, is_active=is_active,
        conditions=conditions, agreement_id=uuid4(),
    )


def test_conditions_match_empty_matches_everything():
    assert slm_service.conditions_match({}, {"priority": "high"}) is True


def test_conditions_match_all_keys_must_equal():
    assert slm_service.conditions_match({"priority": "high"}, {"priority": "high"}) is True
    assert slm_service.conditions_match({"priority": "high"}, {"priority": "low"}) is False


def test_conditions_match_null_condition_ignored():
    assert slm_service.conditions_match({"priority": None, "type": "incident"},
                                        {"type": "incident"}) is True


def test_conditions_match_tag_membership():
    assert slm_service.conditions_match({"tag": "vip"}, {"tag": ["vip", "urgent"]}) is True
    assert slm_service.conditions_match({"tag": "vip"}, {"tag": ["urgent"]}) is False


def test_match_rule_first_match_wins_by_position():
    r_hi = _rule(2, {"priority": "high"})
    r_any = _rule(1, {})           # catch-all, lower position → evaluated first
    matched = slm_service.match_rule([r_hi, r_any], {"priority": "high"})
    assert matched is r_any        # position 1 wins over position 2


def test_match_rule_skips_inactive():
    r_off = _rule(1, {"priority": "high"}, is_active=False)
    r_on = _rule(2, {"priority": "high"})
    assert slm_service.match_rule([r_off, r_on], {"priority": "high"}) is r_on


def test_match_rule_none_when_no_match():
    assert slm_service.match_rule([_rule(1, {"priority": "high"})], {"priority": "low"}) is None


def test_effective_events_default_and_override():
    t_default = SimpleNamespace(metric="first_response", start_event=None, stop_event=None)
    assert slm_service.effective_events(t_default) == ("ticket_created", "first_public_agent_reply")
    t_override = SimpleNamespace(metric="resolution", start_event="status_in_progress", stop_event="closed")
    assert slm_service.effective_events(t_override) == ("status_in_progress", "closed")


def test_match_explanation_trace():
    r1 = _rule(1, {"priority": "low"})
    r2 = _rule(2, {"priority": "high"})
    exp = slm_service.match_explanation([r1, r2], {"priority": "high"})
    assert exp["matched_by"] == "rule"
    assert exp["matched_rule_id"] == str(r2.id)
    assert exp["agreement_id"] == str(r2.agreement_id)


# ---------------------------------------------------------------------------
# API — coverage windows, agreements, targets, rules (tenant_admin write path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_coverage_window_crud(tenant_admin_client):
    r = await tenant_admin_client.post("/api/v1/sla/coverage-windows", json={
        "name": "EMEA 9-5", "timezone": "Europe/London",
        "work_days": [1, 2, 3, 4, 5], "windows": [{"start": "09:00", "end": "17:00"}],
    })
    assert r.status_code == 201, r.text
    cw_id = r.json()["id"]

    r = await tenant_admin_client.get("/api/v1/sla/coverage-windows")
    assert r.status_code == 200
    assert any(c["id"] == cw_id for c in r.json())

    r = await tenant_admin_client.patch(f"/api/v1/sla/coverage-windows/{cw_id}", json={"is_247": True})
    assert r.status_code == 200 and r.json()["is_247"] is True


@pytest.mark.asyncio
async def test_agreement_lifecycle_and_versioning(tenant_admin_client):
    r = await tenant_admin_client.post("/api/v1/sla/agreements", json={
        "name": "Critical Production", "kind": "sla",
    })
    assert r.status_code == 201, r.text
    ag = r.json()
    assert ag["kind"] == "sla" and ag["version"] == 1

    # add two targets
    for metric, mins in (("first_response", 15), ("resolution", 240)):
        rt = await tenant_admin_client.post(
            f"/api/v1/sla/agreements/{ag['id']}/targets",
            json={"metric": metric, "duration_minutes": mins},
        )
        assert rt.status_code == 201, rt.text

    r = await tenant_admin_client.get(f"/api/v1/sla/agreements/{ag['id']}")
    assert r.status_code == 200
    assert len(r.json()["targets"]) == 2

    # editing bumps the version
    r = await tenant_admin_client.patch(f"/api/v1/sla/agreements/{ag['id']}", json={"description": "prod"})
    assert r.status_code == 200 and r.json()["version"] == 2


@pytest.mark.asyncio
async def test_ola_requires_owner_team_and_uc_requires_vendor(tenant_admin_client):
    r = await tenant_admin_client.post("/api/v1/sla/agreements", json={"name": "Infra OLA", "kind": "ola"})
    assert r.status_code in (400, 422), r.text
    r = await tenant_admin_client.post("/api/v1/sla/agreements", json={"name": "Vendor UC", "kind": "uc"})
    assert r.status_code in (400, 422), r.text


@pytest.mark.asyncio
async def test_rules_reorder_and_match_preview(tenant_admin_client):
    ag = (await tenant_admin_client.post(
        "/api/v1/sla/agreements", json={"name": "Rule Target SLA", "kind": "sla"})).json()

    r1 = (await tenant_admin_client.post("/api/v1/sla/rules", json={
        "agreement_id": ag["id"], "conditions": {"priority": "high"}})).json()
    r2 = (await tenant_admin_client.post("/api/v1/sla/rules", json={
        "agreement_id": ag["id"], "conditions": {}})).json()  # catch-all

    # reorder: catch-all first would shadow the specific rule
    rr = await tenant_admin_client.patch("/api/v1/sla/rules/reorder",
                                         json={"ordered_ids": [r1["id"], r2["id"]]})
    assert rr.status_code == 200
    positions = {row["id"]: row["position"] for row in rr.json()}
    assert positions[r1["id"]] < positions[r2["id"]]

    # match-preview: a high-priority ticket matches the specific rule → this agreement
    mp = await tenant_admin_client.post("/api/v1/sla/match-preview", json={"conditions": {"priority": "high"}})
    assert mp.status_code == 200
    assert mp.json()["agreement_id"] == ag["id"]
    assert mp.json()["matched_rule_id"] == r1["id"]


# ---------------------------------------------------------------------------
# RBAC — agents can read, not write
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_can_read_but_not_write(agent_client):
    assert (await agent_client.get("/api/v1/sla/agreements")).status_code == 200
    r = await agent_client.post("/api/v1/sla/agreements", json={"name": "nope", "kind": "sla"})
    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_unauthenticated_rejected(async_client):
    r = await async_client.get("/api/v1/sla/agreements")
    assert r.status_code in (401, 403)
