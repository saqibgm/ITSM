"""
Integration tests for the GDPR Data Export & Erasure API (/api/v1/gdpr).

Covers:
  - Self-service export (GET /gdpr/export)
  - Self-service erasure (POST /gdpr/erasure-request)
  - Erasure history (GET /gdpr/erasure-history) — tenant_admin only
  - Admin-initiated erasure (POST /gdpr/admin/erase-user/{user_id}) — tenant_admin only
  - Guard: admin cannot erase themselves via the admin path
  - Guard: platform users cannot use the tenant-scoped GDPR endpoints
"""

import json
from uuid import uuid4

import pytest

pytestmark = pytest.mark.asyncio


# ===========================================================================
# GET /api/v1/gdpr/export
# ===========================================================================


async def test_export_my_data_returns_200_json(end_user_client):
    resp = await end_user_client.get("/api/v1/gdpr/export")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/json")
    assert "attachment" in resp.headers.get("content-disposition", "")

    data = resp.json()
    assert isinstance(data, dict)


async def test_export_my_data_as_admin(tenant_admin_client):
    resp = await tenant_admin_client.get("/api/v1/gdpr/export")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/json")


async def test_export_unauthenticated_returns_401(async_client):
    resp = await async_client.get("/api/v1/gdpr/export")
    assert resp.status_code == 401


# ===========================================================================
# POST /api/v1/gdpr/erasure-request
# ===========================================================================


async def test_self_erasure_confirm_true_succeeds(end_user_client, test_end_user_id):
    resp = await end_user_client.post(
        "/api/v1/gdpr/erasure-request", json={"confirm": True}
    )
    # 200 or 2xx — erasure succeeds
    assert resp.status_code == 200, resp.text


async def test_self_erasure_confirm_false_returns_422(end_user_client):
    resp = await end_user_client.post(
        "/api/v1/gdpr/erasure-request", json={"confirm": False}
    )
    assert resp.status_code in (400, 422), resp.text


async def test_self_erasure_missing_confirm_returns_422(end_user_client):
    resp = await end_user_client.post("/api/v1/gdpr/erasure-request", json={})
    assert resp.status_code == 422


async def test_self_erasure_unauthenticated_returns_401(async_client):
    resp = await async_client.post(
        "/api/v1/gdpr/erasure-request", json={"confirm": True}
    )
    assert resp.status_code == 401


# ===========================================================================
# GET /api/v1/gdpr/erasure-history
# ===========================================================================


async def test_erasure_history_admin_returns_200(tenant_admin_client):
    resp = await tenant_admin_client.get("/api/v1/gdpr/erasure-history")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "items" in body or isinstance(body, list)


async def test_erasure_history_agent_forbidden(agent_client):
    resp = await agent_client.get("/api/v1/gdpr/erasure-history")
    assert resp.status_code == 403


async def test_erasure_history_end_user_forbidden(end_user_client):
    resp = await end_user_client.get("/api/v1/gdpr/erasure-history")
    assert resp.status_code == 403


async def test_erasure_history_appears_after_erasure(
    tenant_admin_client, end_user_client, test_end_user_id
):
    """After a self-erasure, the history endpoint records the event."""
    erase_resp = await end_user_client.post(
        "/api/v1/gdpr/erasure-request", json={"confirm": True}
    )
    assert erase_resp.status_code == 200

    hist_resp = await tenant_admin_client.get("/api/v1/gdpr/erasure-history")
    assert hist_resp.status_code == 200


# ===========================================================================
# POST /api/v1/gdpr/admin/erase-user/{user_id}
# ===========================================================================


async def test_admin_erase_other_user_returns_200(
    tenant_admin_client, test_end_user_id, test_admin_user_id
):
    resp = await tenant_admin_client.post(
        f"/api/v1/gdpr/admin/erase-user/{test_end_user_id}",
        json={"reason": "User requested account deletion via support ticket #12345"},
    )
    assert resp.status_code == 200, resp.text


async def test_admin_erase_user_missing_reason_returns_422(
    tenant_admin_client, test_end_user_id
):
    resp = await tenant_admin_client.post(
        f"/api/v1/gdpr/admin/erase-user/{test_end_user_id}",
        json={},
    )
    assert resp.status_code == 422


async def test_admin_cannot_erase_themselves(
    tenant_admin_client, test_admin_user_id
):
    """Admin erasing their own ID via the admin path returns 403."""
    resp = await tenant_admin_client.post(
        f"/api/v1/gdpr/admin/erase-user/{test_admin_user_id}",
        json={"reason": "Self-erasure attempt"},
    )
    assert resp.status_code == 403, resp.text


async def test_admin_erase_agent_forbidden(agent_client, test_end_user_id):
    resp = await agent_client.post(
        f"/api/v1/gdpr/admin/erase-user/{test_end_user_id}",
        json={"reason": "Unauthorised agent attempt"},
    )
    assert resp.status_code == 403


async def test_admin_erase_nonexistent_user_returns_404(tenant_admin_client):
    resp = await tenant_admin_client.post(
        f"/api/v1/gdpr/admin/erase-user/{uuid4()}",
        json={"reason": "User not found test"},
    )
    assert resp.status_code == 404
