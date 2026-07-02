"""SLI/SLO/error-budget reliability (Phase 9) — math, ingest, status, burn alerts, RBAC."""
import pytest

from app.services import slo_service


# ── pure math ────────────────────────────────────────────────────────────────

def test_compute_status_meeting():
    # 99.9% target, 1,000,000 events, 500 bad → SLI 99.95%, budget 1000, 50% consumed
    st = slo_service.compute_status(99.9, 28, good=999_500, total=1_000_000)
    assert st["sli_pct"] == pytest.approx(99.95, abs=0.01)
    assert st["error_budget_events"] == pytest.approx(1000, abs=1)
    assert st["budget_consumed_pct"] == pytest.approx(50.0, abs=0.5)
    assert st["meeting"] is True


def test_compute_status_over_budget():
    st = slo_service.compute_status(99.9, 28, good=998_000, total=1_000_000)  # 2000 bad, budget 1000
    assert st["budget_consumed_pct"] > 100
    assert st["budget_remaining_pct"] < 0
    assert st["meeting"] is False


def test_burn_rate_and_eta():
    # observed error 1.44% vs allowed 0.1% → burn ~14.4 (fast-burn threshold)
    assert slo_service.burn_rate(99.9, good=9856, total=10000) == pytest.approx(14.4, abs=0.1)
    assert slo_service.burn_rate(99.9, good=10000, total=10000) == 0.0
    assert slo_service.burn_rate(99.9, 0, 0) is None
    eta = slo_service.eta_hours_to_exhaustion(28, remaining_pct=50.0, current_burn=14.4)
    assert eta and eta > 0


def test_empty_window_is_none():
    st = slo_service.compute_status(99.9, 28, good=0, total=0)
    assert st["sli_pct"] is None and st["meeting"] is None


# ── API flow ─────────────────────────────────────────────────────────────────

async def _service(client, name="Payments API"):
    return (await client.post("/api/v1/services", json={"name": name})).json()


@pytest.mark.asyncio
async def test_full_slo_lifecycle(tenant_admin_client):
    c = tenant_admin_client
    src = (await c.post("/api/v1/slo/sources", json={
        "name": "Internal uptime", "type": "internal_metric", "config": {"metric": "uptime"}})).json()
    svc = await _service(c)
    slo = (await c.post("/api/v1/slo/objectives", json={
        "service_id": svc["id"], "sli_source_id": src["id"], "name": "Payments availability",
        "target_pct": 99.9, "window": "rolling_28d"})).json()
    assert slo["target_pct"] == 99.9

    # push healthy then check status
    r = await c.post(f"/api/v1/slo/{slo['id']}/measurements", json={"good_count": 9999, "total_count": 10000})
    assert r.status_code == 201, r.text
    st = (await c.get(f"/api/v1/slo/objectives/{slo['id']}/status")).json()
    assert st["sli_pct"] == pytest.approx(99.99, abs=0.01)
    assert st["meeting"] is True
    assert st["budget_remaining_pct"] is not None

    # per-service + report
    svc_slos = (await c.get(f"/api/v1/services/{svc['id']}/slo")).json()
    assert len(svc_slos) == 1 and svc_slos[0]["slo_id"] == slo["id"]
    rep = (await c.get("/api/v1/slo/reports/error-budget")).json()
    assert any(o["slo_id"] == slo["id"] for o in rep["objectives"])


@pytest.mark.asyncio
async def test_fast_burn_fires_alert(tenant_admin_client):
    c = tenant_admin_client
    src = (await c.post("/api/v1/slo/sources", json={"name": "src", "type": "push_api"})).json()
    svc = await _service(c, "Web")
    slo = (await c.post("/api/v1/slo/objectives", json={
        "service_id": svc["id"], "sli_source_id": src["id"], "name": "Web availability",
        "target_pct": 99.9})).json()
    ba = (await c.post(f"/api/v1/slo/objectives/{slo['id']}/burn-alerts", json={"kind": "fast_burn"})).json()
    assert ba["kind"] == "fast_burn"

    # push a very-bad bucket → burn >> 14.4 over both windows → should fire
    await c.post(f"/api/v1/slo/{slo['id']}/measurements", json={"good_count": 0, "total_count": 500})
    rules = (await c.get(f"/api/v1/slo/objectives/{slo['id']}/burn-alerts")).json()
    assert rules[0]["state"] == "firing"
    # an alert was raised through Module B
    alerts = (await c.get("/api/v1/alerts")).json()
    assert any(a["source"] == "slo_burn" for a in alerts)


# ── RBAC + tenant scope ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_agent_cannot_create_objective(agent_client):
    r = await agent_client.post("/api/v1/slo/sources", json={"name": "x", "type": "internal_metric"})
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_platform_no_tenant_lists_all(platform_admin_client):
    assert (await platform_admin_client.get("/api/v1/slo/objectives")).status_code == 200
    assert (await platform_admin_client.get("/api/v1/slo/reports/error-budget")).status_code == 200
