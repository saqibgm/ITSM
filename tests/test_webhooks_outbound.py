"""
Integration tests for the Outbound Webhook Configuration API (/api/v1/webhooks-config).

Covers:
  - CRUD on webhook endpoints
  - Secret always masked in GET responses
  - URL validation (must be http/https)
  - Secret length validation (min 16 chars)
  - Role-based access (admin-only for mutations)
"""

from uuid import uuid4

import pytest

pytestmark = pytest.mark.asyncio

_SECRET_MASK = "*****"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _endpoint_body(**overrides) -> dict:
    return {
        "name": "CI Notification Hook",
        "url": "https://hooks.example.com/itsm",
        "secret": "supersecretvalue12345",
        "events": ["ticket.created", "ticket.updated"],
        **overrides,
    }


async def _create_endpoint(client) -> dict:
    resp = await client.post(
        "/api/v1/webhooks-config/endpoints", json=_endpoint_body()
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ===========================================================================
# GET /api/v1/webhooks-config/endpoints
# ===========================================================================


async def test_list_endpoints_empty_returns_200(agent_client):
    resp = await agent_client.get("/api/v1/webhooks-config/endpoints")
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body
    assert isinstance(body["items"], list)


async def test_list_endpoints_unauthenticated_returns_401(async_client):
    resp = await async_client.get("/api/v1/webhooks-config/endpoints")
    assert resp.status_code == 401


# ===========================================================================
# POST /api/v1/webhooks-config/endpoints
# ===========================================================================


async def test_create_endpoint_returns_201(tenant_admin_client):
    resp = await tenant_admin_client.post(
        "/api/v1/webhooks-config/endpoints", json=_endpoint_body()
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert "id" in body
    assert body["name"] == "CI Notification Hook"
    assert body["url"] == "https://hooks.example.com/itsm"
    assert body["is_active"] is True
    assert body["failure_count"] == 0


async def test_create_endpoint_secret_is_masked_on_create(tenant_admin_client):
    resp = await tenant_admin_client.post(
        "/api/v1/webhooks-config/endpoints", json=_endpoint_body()
    )
    assert resp.status_code == 201
    assert resp.json()["secret"] == _SECRET_MASK


async def test_create_endpoint_secret_is_masked_on_get(tenant_admin_client, agent_client):
    created = await _create_endpoint(tenant_admin_client)
    endpoint_id = created["id"]

    resp = await agent_client.get(
        f"/api/v1/webhooks-config/endpoints/{endpoint_id}"
    )
    assert resp.status_code == 200
    assert resp.json()["secret"] == _SECRET_MASK


async def test_create_endpoint_secret_is_masked_in_list(tenant_admin_client, agent_client):
    await _create_endpoint(tenant_admin_client)
    resp = await agent_client.get("/api/v1/webhooks-config/endpoints")
    assert resp.status_code == 200
    for item in resp.json()["items"]:
        assert item["secret"] == _SECRET_MASK


async def test_create_endpoint_invalid_url_returns_422(tenant_admin_client):
    resp = await tenant_admin_client.post(
        "/api/v1/webhooks-config/endpoints",
        json=_endpoint_body(url="ftp://invalid.example.com"),
    )
    assert resp.status_code == 422


async def test_create_endpoint_short_secret_returns_422(tenant_admin_client):
    resp = await tenant_admin_client.post(
        "/api/v1/webhooks-config/endpoints",
        json=_endpoint_body(secret="tooshort"),
    )
    assert resp.status_code == 422


async def test_create_endpoint_missing_name_returns_422(tenant_admin_client):
    body = _endpoint_body()
    del body["name"]
    resp = await tenant_admin_client.post(
        "/api/v1/webhooks-config/endpoints", json=body
    )
    assert resp.status_code == 422


async def test_create_endpoint_agent_forbidden(agent_client):
    resp = await agent_client.post(
        "/api/v1/webhooks-config/endpoints", json=_endpoint_body()
    )
    assert resp.status_code == 403


# ===========================================================================
# GET /api/v1/webhooks-config/endpoints/{id}
# ===========================================================================


async def test_get_endpoint_returns_200(tenant_admin_client, agent_client):
    created = await _create_endpoint(tenant_admin_client)
    endpoint_id = created["id"]

    resp = await agent_client.get(
        f"/api/v1/webhooks-config/endpoints/{endpoint_id}"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == endpoint_id
    assert body["name"] == "CI Notification Hook"


async def test_get_nonexistent_endpoint_returns_404(agent_client):
    resp = await agent_client.get(
        f"/api/v1/webhooks-config/endpoints/{uuid4()}"
    )
    assert resp.status_code == 404


# ===========================================================================
# PATCH /api/v1/webhooks-config/endpoints/{id}
# ===========================================================================


async def test_update_endpoint_name(tenant_admin_client):
    created = await _create_endpoint(tenant_admin_client)
    endpoint_id = created["id"]

    resp = await tenant_admin_client.patch(
        f"/api/v1/webhooks-config/endpoints/{endpoint_id}",
        json={"name": "Updated Hook Name"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "Updated Hook Name"


async def test_update_endpoint_deactivate(tenant_admin_client):
    created = await _create_endpoint(tenant_admin_client)
    endpoint_id = created["id"]

    resp = await tenant_admin_client.patch(
        f"/api/v1/webhooks-config/endpoints/{endpoint_id}",
        json={"is_active": False},
    )
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False


async def test_update_endpoint_invalid_url_returns_422(tenant_admin_client):
    created = await _create_endpoint(tenant_admin_client)
    resp = await tenant_admin_client.patch(
        f"/api/v1/webhooks-config/endpoints/{created['id']}",
        json={"url": "not-a-url"},
    )
    assert resp.status_code == 422


async def test_update_endpoint_agent_forbidden(tenant_admin_client, agent_client):
    created = await _create_endpoint(tenant_admin_client)
    resp = await agent_client.patch(
        f"/api/v1/webhooks-config/endpoints/{created['id']}",
        json={"name": "Hack"},
    )
    assert resp.status_code == 403


async def test_update_nonexistent_endpoint_returns_404(tenant_admin_client):
    resp = await tenant_admin_client.patch(
        f"/api/v1/webhooks-config/endpoints/{uuid4()}",
        json={"name": "Ghost"},
    )
    assert resp.status_code == 404


# ===========================================================================
# DELETE /api/v1/webhooks-config/endpoints/{id}
# ===========================================================================


async def test_delete_endpoint_returns_204(tenant_admin_client, agent_client):
    created = await _create_endpoint(tenant_admin_client)
    endpoint_id = created["id"]

    del_resp = await tenant_admin_client.delete(
        f"/api/v1/webhooks-config/endpoints/{endpoint_id}"
    )
    assert del_resp.status_code == 204

    get_resp = await agent_client.get(
        f"/api/v1/webhooks-config/endpoints/{endpoint_id}"
    )
    assert get_resp.status_code == 404


async def test_delete_endpoint_agent_forbidden(tenant_admin_client, agent_client):
    created = await _create_endpoint(tenant_admin_client)
    resp = await agent_client.delete(
        f"/api/v1/webhooks-config/endpoints/{created['id']}"
    )
    assert resp.status_code == 403


async def test_delete_nonexistent_endpoint_returns_404(tenant_admin_client):
    resp = await tenant_admin_client.delete(
        f"/api/v1/webhooks-config/endpoints/{uuid4()}"
    )
    assert resp.status_code == 404


# ===========================================================================
# GET /api/v1/webhooks-config/endpoints/{id}/deliveries
# ===========================================================================


async def test_list_deliveries_empty(tenant_admin_client, agent_client):
    created = await _create_endpoint(tenant_admin_client)
    endpoint_id = created["id"]

    resp = await agent_client.get(
        f"/api/v1/webhooks-config/endpoints/{endpoint_id}/deliveries"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body
    assert body["items"] == []


# ===========================================================================
# POST /api/v1/webhooks-config/endpoints/{id}/enable
# ===========================================================================


async def test_enable_disabled_endpoint(tenant_admin_client):
    created = await _create_endpoint(tenant_admin_client)
    endpoint_id = created["id"]

    # First disable it
    await tenant_admin_client.patch(
        f"/api/v1/webhooks-config/endpoints/{endpoint_id}",
        json={"is_active": False},
    )

    # Re-enable
    resp = await tenant_admin_client.post(
        f"/api/v1/webhooks-config/endpoints/{endpoint_id}/enable"
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_active"] is True
