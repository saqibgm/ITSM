"""RLS regression + platform 'show all' (2026-07-02 fix).

Guards the long-standing bug where the tenant_isolation predicate's
current_setting('app.tenant_id')::uuid cast threw `invalid input syntax for
uuid: ""` under asyncpg when a platform session left the GUC empty (migration
0032 added a NULLIF guard), and the app-layer 'no tenant selected → show all'
behaviour on the operator list endpoints.
"""
import pytest
from sqlalchemy import text


@pytest.mark.asyncio
async def test_rls_empty_tenant_guc_does_not_crash(db):
    """Fix B: empty app.tenant_id + bypass on must NOT raise ''::uuid on any
    RLS table (this is exactly what a real platform request sets)."""
    await db.execute(text("SELECT set_config('app.tenant_id', '', false), "
                          "set_config('app.bypass_rls', 'on', false)"))
    for tbl in ("alerts", "incidents", "sla_agreements", "sla_instances",
                "escalation_policies", "oncall_services", "tickets"):
        await db.execute(text(f"SELECT count(*) FROM {tbl}"))  # must not raise
    # reset so later assertions/tests aren't affected
    await db.execute(text("SELECT set_config('app.tenant_id', '', false), "
                          "set_config('app.bypass_rls', '', false)"))


@pytest.mark.asyncio
async def test_platform_no_tenant_shows_all_not_error(platform_admin_client):
    """Fix A: a platform user with no tenant selected gets a list (show all),
    not 'Tenant context required' (403) or a 500."""
    assert (await platform_admin_client.get("/api/v1/alerts")).status_code == 200
    assert (await platform_admin_client.get("/api/v1/incidents")).status_code == 200


@pytest.mark.asyncio
async def test_tenant_user_still_scoped(agent_client):
    """Tenant users remain scoped (regression check on the predicate change)."""
    assert (await agent_client.get("/api/v1/alerts")).status_code == 200
    assert (await agent_client.get("/api/v1/incidents")).status_code == 200
