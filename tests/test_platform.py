"""
Integration tests for the Platform Management API (/api/v1/platform).

Platform endpoints are restricted to platform-tier users (tier="platform").
Tenant users receive 403 on all platform endpoints.

A local platform_admin_client fixture is defined here since it differs from
the tenant-scoped role clients in conftest.
"""

from uuid import UUID, uuid4

import pytest
import pytest_asyncio

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Local platform-tier client fixtures
# ---------------------------------------------------------------------------


def _make_platform_user_override(platform_role: str):
    from app.auth.dependencies import CurrentUser

    async def _override():
        return CurrentUser(
            iam_user_id=f"usr_platform_{uuid4().hex[:8]}",
            org_id="org_99technologies",
            email="platform@99technologies.com",
            roles=["admin"],
            tier="platform",
            platform_role=platform_role,
            tenant_id=None,
            local_user_id=None,
            is_active=True,
        )

    return _override


@pytest_asyncio.fixture
async def platform_admin_client(async_client):
    """AsyncClient with platform-admin identity (no tenant context)."""
    from app.auth.dependencies import get_current_user

    app = async_client.app  # type: ignore[attr-defined]
    app.dependency_overrides[get_current_user] = _make_platform_user_override(
        "platform_admin"
    )
    async_client.headers.update({"Authorization": "Bearer fake-platform-admin-token"})
    return async_client


@pytest_asyncio.fixture
async def platform_support_client(async_client):
    """AsyncClient with platform-support identity (no tenant context)."""
    from app.auth.dependencies import get_current_user

    app = async_client.app  # type: ignore[attr-defined]
    app.dependency_overrides[get_current_user] = _make_platform_user_override(
        "platform_support"
    )
    async_client.headers.update({"Authorization": "Bearer fake-platform-support-token"})
    return async_client


# ===========================================================================
# GET /api/v1/platform/tenants
# ===========================================================================


async def test_list_tenants_platform_admin_returns_200(platform_admin_client):
    resp = await platform_admin_client.get("/api/v1/platform/tenants")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "items" in body
    assert isinstance(body["items"], list)


async def test_list_tenants_platform_support_returns_200(platform_support_client):
    resp = await platform_support_client.get("/api/v1/platform/tenants")
    assert resp.status_code == 200, resp.text


async def test_list_tenants_tenant_user_forbidden(tenant_admin_client):
    resp = await tenant_admin_client.get("/api/v1/platform/tenants")
    assert resp.status_code == 403


async def test_list_tenants_agent_forbidden(agent_client):
    resp = await agent_client.get("/api/v1/platform/tenants")
    assert resp.status_code == 403


async def test_list_tenants_unauthenticated_returns_401(async_client):
    resp = await async_client.get("/api/v1/platform/tenants")
    assert resp.status_code == 401


async def test_list_tenants_contains_seeded_tenant(
    platform_admin_client, seeded_tenant, test_tenant_id
):
    resp = await platform_admin_client.get("/api/v1/platform/tenants")
    assert resp.status_code == 200
    items = resp.json()["items"]
    ids = [item["id"] for item in items]
    assert str(test_tenant_id) in ids


# ===========================================================================
# GET /api/v1/platform/tenants/{tenant_id}
# ===========================================================================


async def test_get_tenant_detail_returns_200(
    platform_admin_client, seeded_tenant, test_tenant_id
):
    resp = await platform_admin_client.get(
        f"/api/v1/platform/tenants/{test_tenant_id}"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == str(test_tenant_id)
    assert "name" in body
    assert "iam_org_id" in body


async def test_get_nonexistent_tenant_returns_404(platform_admin_client):
    resp = await platform_admin_client.get(
        f"/api/v1/platform/tenants/{uuid4()}"
    )
    assert resp.status_code == 404


async def test_get_tenant_tenant_user_forbidden(
    tenant_admin_client, seeded_tenant, test_tenant_id
):
    resp = await tenant_admin_client.get(
        f"/api/v1/platform/tenants/{test_tenant_id}"
    )
    assert resp.status_code == 403


# ===========================================================================
# PATCH /api/v1/platform/tenants/{tenant_id}
# ===========================================================================


async def test_patch_tenant_name(
    platform_admin_client, seeded_tenant, test_tenant_id
):
    resp = await platform_admin_client.patch(
        f"/api/v1/platform/tenants/{test_tenant_id}",
        json={"plan": "enterprise"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["settings"].get("plan") == "enterprise"


async def test_deactivate_tenant(
    platform_admin_client, seeded_tenant, test_tenant_id
):
    resp = await platform_admin_client.patch(
        f"/api/v1/platform/tenants/{test_tenant_id}",
        json={"status": "suspended"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_active"] is False


async def test_patch_tenant_support_cannot_write(
    platform_support_client, seeded_tenant, test_tenant_id
):
    """Platform support is read-only — PATCH should return 403."""
    resp = await platform_support_client.patch(
        f"/api/v1/platform/tenants/{test_tenant_id}",
        json={"name": "Attempted update"},
    )
    assert resp.status_code == 403


async def test_patch_nonexistent_tenant_returns_404(platform_admin_client):
    resp = await platform_admin_client.patch(
        f"/api/v1/platform/tenants/{uuid4()}",
        json={"status": "suspended"},
    )
    assert resp.status_code == 404


# ===========================================================================
# GET /api/v1/platform/tenants/{tenant_id}/audit-log
# ===========================================================================


async def test_get_tenant_audit_log_returns_200(
    platform_admin_client, seeded_tenant, test_tenant_id
):
    resp = await platform_admin_client.get(
        f"/api/v1/platform/tenants/{test_tenant_id}/audit-log"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "items" in body


# ===========================================================================
# GET /api/v1/platform/system-health
# ===========================================================================


async def test_system_health_platform_admin(platform_admin_client):
    resp = await platform_admin_client.get("/api/v1/platform/system-health")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "db" in body


async def test_system_health_tenant_user_forbidden(tenant_admin_client):
    resp = await tenant_admin_client.get("/api/v1/platform/system-health")
    assert resp.status_code == 403


# ===========================================================================
# GET /api/v1/platform/ai-usage-overview
# ===========================================================================


async def test_ai_usage_overview_platform_admin(platform_admin_client):
    resp = await platform_admin_client.get("/api/v1/platform/ai-usage-overview")
    assert resp.status_code == 200, resp.text
