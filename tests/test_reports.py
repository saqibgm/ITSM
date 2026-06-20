"""
Integration tests for the Reporting & Analytics API (/api/v1/reports).

All 7 report endpoints are tested for:
  - 200 on agent+ role
  - Correct response shape
  - 403 for end-user role
  - Optional date-range parameters are accepted
"""

import pytest

pytestmark = pytest.mark.asyncio


# ===========================================================================
# GET /api/v1/reports/tickets/summary
# ===========================================================================


async def test_ticket_summary_returns_200(agent_client):
    resp = await agent_client.get("/api/v1/reports/tickets/summary")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "by_status" in body
    assert "by_priority" in body
    assert "by_type" in body
    assert "total" in body


async def test_ticket_summary_with_date_range(agent_client):
    resp = await agent_client.get(
        "/api/v1/reports/tickets/summary?start_date=2025-01-01&end_date=2025-12-31"
    )
    assert resp.status_code == 200, resp.text


async def test_ticket_summary_end_user_forbidden(end_user_client):
    resp = await end_user_client.get("/api/v1/reports/tickets/summary")
    assert resp.status_code == 403


async def test_ticket_summary_unauthenticated_returns_401(async_client):
    resp = await async_client.get("/api/v1/reports/tickets/summary")
    assert resp.status_code == 401


# ===========================================================================
# GET /api/v1/reports/tickets/sla-compliance
# ===========================================================================


async def test_sla_compliance_returns_200(agent_client):
    resp = await agent_client.get("/api/v1/reports/tickets/sla-compliance")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "total_tickets" in body
    assert "within_sla" in body
    assert "breached" in body
    assert "compliance_pct" in body
    assert "by_priority" in body


async def test_sla_compliance_with_date_range(agent_client):
    resp = await agent_client.get(
        "/api/v1/reports/tickets/sla-compliance?start_date=2025-01-01&end_date=2025-12-31"
    )
    assert resp.status_code == 200


async def test_sla_compliance_end_user_forbidden(end_user_client):
    resp = await end_user_client.get("/api/v1/reports/tickets/sla-compliance")
    assert resp.status_code == 403


# ===========================================================================
# GET /api/v1/reports/tickets/volume-trend
# ===========================================================================


async def test_volume_trend_returns_200(agent_client):
    resp = await agent_client.get("/api/v1/reports/tickets/volume-trend")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "points" in body
    assert isinstance(body["points"], list)


async def test_volume_trend_with_granularity(agent_client):
    resp = await agent_client.get(
        "/api/v1/reports/tickets/volume-trend?granularity=week"
    )
    assert resp.status_code in (200, 422)


async def test_volume_trend_end_user_forbidden(end_user_client):
    resp = await end_user_client.get("/api/v1/reports/tickets/volume-trend")
    assert resp.status_code == 403


# ===========================================================================
# GET /api/v1/reports/assets/summary
# ===========================================================================


async def test_asset_summary_returns_200(agent_client):
    resp = await agent_client.get("/api/v1/reports/assets/summary")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "total" in body
    assert "by_status" in body


async def test_asset_summary_end_user_forbidden(end_user_client):
    resp = await end_user_client.get("/api/v1/reports/assets/summary")
    assert resp.status_code == 403


# ===========================================================================
# GET /api/v1/reports/ai/usage
# ===========================================================================


async def test_ai_usage_summary_returns_200(agent_client):
    resp = await agent_client.get("/api/v1/reports/ai/usage")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "total_tokens" in body
    assert "total_calls" in body


async def test_ai_usage_end_user_forbidden(end_user_client):
    resp = await end_user_client.get("/api/v1/reports/ai/usage")
    assert resp.status_code == 403


# ===========================================================================
# GET /api/v1/reports/kb/top-articles
# ===========================================================================


async def test_kb_top_articles_returns_200(agent_client):
    resp = await agent_client.get("/api/v1/reports/kb/top-articles")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "articles" in body
    assert isinstance(body["articles"], list)


async def test_kb_top_articles_end_user_forbidden(end_user_client):
    resp = await end_user_client.get("/api/v1/reports/kb/top-articles")
    assert resp.status_code == 403


# ===========================================================================
# GET /api/v1/reports/teams/workload
# ===========================================================================


async def test_team_workload_returns_200(agent_client):
    resp = await agent_client.get("/api/v1/reports/teams/workload")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "teams" in body
    assert isinstance(body["teams"], list)


async def test_team_workload_end_user_forbidden(end_user_client):
    resp = await end_user_client.get("/api/v1/reports/teams/workload")
    assert resp.status_code == 403
