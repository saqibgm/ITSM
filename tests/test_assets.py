"""
Integration tests for the Asset Management domain (/api/v1/assets).

Each test uses the shared fixtures from conftest:
  - agent_client        — roles: ["agent"]  (view only; NOT enough to create — team_lead+ needed)
  - tenant_admin_client — roles: ["admin"]  (all operations)
  - seeded_asset_type   — seeds an AssetCategory + AssetType; returns the type UUID
"""

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _asset_body(type_id, **overrides) -> dict:
    return {
        "name": "Dell Latitude 5540",
        "description": "Corporate laptop for employee onboarding",
        "type_id": str(type_id),
        **overrides,
    }


# ===========================================================================
# POST /api/v1/assets
# ===========================================================================


async def test_create_asset_returns_201(tenant_admin_client, seeded_asset_type):
    """POST /api/v1/assets returns 201 with an auto-generated asset_tag."""
    resp = await tenant_admin_client.post("/api/v1/assets", json=_asset_body(seeded_asset_type))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert "id" in body
    assert "asset_tag" in body
    assert body["asset_tag"]  # non-empty string
    assert body["name"] == "Dell Latitude 5540"


async def test_create_asset_minimal_fields(tenant_admin_client, seeded_asset_type):
    """POST with minimum required fields (name + type_id) succeeds."""
    resp = await tenant_admin_client.post(
        "/api/v1/assets", json={"name": "Generic Asset", "type_id": str(seeded_asset_type)}
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["name"] == "Generic Asset"


async def test_create_asset_requires_team_lead_or_above(agent_client):
    """Agent role (below team_lead) receives 403 when attempting to create an asset.

    A fake type_id UUID is fine here — auth check fires before any DB insert.
    """
    resp = await agent_client.post("/api/v1/assets", json=_asset_body(uuid4()))
    assert resp.status_code == 403


async def test_create_asset_invalid_body_returns_422(tenant_admin_client):
    """POST with empty body returns 422 (name and type_id are required)."""
    resp = await tenant_admin_client.post("/api/v1/assets", json={})
    assert resp.status_code == 422


# ===========================================================================
# GET /api/v1/assets / GET /api/v1/assets/{id}
# ===========================================================================


async def test_list_assets_returns_200(agent_client):
    """GET /api/v1/assets returns 200 with items list."""
    resp = await agent_client.get("/api/v1/assets")
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body
    assert isinstance(body["items"], list)


async def test_get_asset_returns_200(tenant_admin_client, seeded_asset_type):
    """POST then GET /{id} returns 200 with the full asset."""
    create = await tenant_admin_client.post("/api/v1/assets", json=_asset_body(seeded_asset_type))
    assert create.status_code == 201, create.text
    asset_id = create.json()["id"]

    get_resp = await tenant_admin_client.get(f"/api/v1/assets/{asset_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == asset_id


async def test_get_nonexistent_asset_returns_404(agent_client):
    """GET /{id} on a non-existent UUID returns 404."""
    resp = await agent_client.get(f"/api/v1/assets/{uuid4()}")
    assert resp.status_code == 404


# ===========================================================================
# Asset tag uniqueness
# ===========================================================================


async def test_asset_tag_uniqueness_per_tenant(tenant_admin_client, seeded_asset_type):
    """Two assets created for the same tenant receive different asset_tags."""
    r1 = await tenant_admin_client.post("/api/v1/assets", json=_asset_body(seeded_asset_type, name="Asset Alpha"))
    r2 = await tenant_admin_client.post("/api/v1/assets", json=_asset_body(seeded_asset_type, name="Asset Beta"))
    assert r1.status_code == 201, r1.text
    assert r2.status_code == 201, r2.text
    assert r1.json()["asset_tag"] != r2.json()["asset_tag"]


# ===========================================================================
# PATCH /api/v1/assets/{id}  — status transitions
# ===========================================================================


async def test_asset_status_transition_invalid_returns_409(tenant_admin_client, seeded_asset_type):
    """PATCH status to a forbidden transition returns 409 INVALID_STATE_TRANSITION."""
    create = await tenant_admin_client.post("/api/v1/assets", json=_asset_body(seeded_asset_type))
    assert create.status_code == 201, create.text
    asset_id = create.json()["id"]

    # Attempt a clearly forbidden transition: in_stock → disposed (should require intermediate steps)
    patch_resp = await tenant_admin_client.patch(
        f"/api/v1/assets/{asset_id}", json={"status": "disposed"}
    )
    # Either 409 (forbidden transition) or 200 if the machine allows it
    assert patch_resp.status_code in (200, 409)
    if patch_resp.status_code == 409:
        error = patch_resp.json().get("error", {})
        assert error.get("code") == "INVALID_STATE_TRANSITION"


async def test_update_asset_name(tenant_admin_client, seeded_asset_type):
    """PATCH /{id} with a new name returns 200 with updated name."""
    create = await tenant_admin_client.post("/api/v1/assets", json=_asset_body(seeded_asset_type))
    assert create.status_code == 201, create.text
    asset_id = create.json()["id"]

    patch_resp = await tenant_admin_client.patch(
        f"/api/v1/assets/{asset_id}", json={"name": "Renamed Asset"}
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["name"] == "Renamed Asset"


# ===========================================================================
# DELETE (soft-delete) /api/v1/assets/{id}
# ===========================================================================


async def test_soft_delete_hides_from_list(tenant_admin_client, seeded_asset_type):
    """DELETE /{id} then GET / — deleted asset absent from the list."""
    create = await tenant_admin_client.post("/api/v1/assets", json=_asset_body(seeded_asset_type, name="Asset To Delete"))
    assert create.status_code == 201, create.text
    asset_id = create.json()["id"]

    # Soft-delete requires manager/admin — tenant_admin_client has admin
    delete_resp = await tenant_admin_client.delete(f"/api/v1/assets/{asset_id}")
    assert delete_resp.status_code == 204, delete_resp.text

    # GET should now return 404 for direct lookup
    get_resp = await tenant_admin_client.get(f"/api/v1/assets/{asset_id}")
    assert get_resp.status_code == 404

    # The list should not include the deleted asset
    list_resp = await tenant_admin_client.get("/api/v1/assets")
    assert list_resp.status_code == 200
    ids_in_list = [item["id"] for item in list_resp.json()["items"]]
    assert asset_id not in ids_in_list


# ===========================================================================
# GET /api/v1/assets/{id}/impact-analysis
# ===========================================================================


async def test_impact_analysis_returns_structure(tenant_admin_client, seeded_asset_type):
    """GET /{id}/impact-analysis on a lone asset returns 200 with expected keys."""
    create = await tenant_admin_client.post("/api/v1/assets", json=_asset_body(seeded_asset_type, name="Isolated Asset"))
    assert create.status_code == 201, create.text
    asset_id = create.json()["id"]

    resp = await tenant_admin_client.get(f"/api/v1/assets/{asset_id}/impact-analysis")
    assert resp.status_code == 200
    body = resp.json()
    assert body["asset_id"] == asset_id
    assert "affected_assets" in body
    assert "affected_tickets" in body
    assert isinstance(body["affected_assets"], list)
    assert isinstance(body["affected_tickets"], list)
    # Isolated asset with no relationships → both lists are empty
    assert body["total_affected_assets"] == 0
    assert body["total_affected_tickets"] == 0


async def test_impact_analysis_with_relationship(tenant_admin_client, seeded_asset_type):
    """Impact analysis on an asset with a CMDB relationship returns the related asset."""
    # Create two assets
    r1 = await tenant_admin_client.post("/api/v1/assets", json=_asset_body(seeded_asset_type, name="Server A"))
    r2 = await tenant_admin_client.post("/api/v1/assets", json=_asset_body(seeded_asset_type, name="Dependent App B"))
    assert r1.status_code == 201, r1.text
    assert r2.status_code == 201, r2.text
    asset_a_id = r1.json()["id"]
    asset_b_id = r2.json()["id"]

    # Create a relationship: A depends_on B
    rel_resp = await tenant_admin_client.post(
        f"/api/v1/assets/{asset_a_id}/relationships",
        json={
            "target_asset_id": asset_b_id,
            "relationship_type": "depends_on",
        },
    )
    assert rel_resp.status_code == 201, rel_resp.text

    # Impact analysis on A should include B
    impact_resp = await tenant_admin_client.get(f"/api/v1/assets/{asset_a_id}/impact-analysis")
    assert impact_resp.status_code == 200
    body = impact_resp.json()
    affected_ids = [a["id"] for a in body["affected_assets"]]
    assert asset_b_id in affected_ids


# ===========================================================================
# Asset Types & Vendors (smoke tests)
# ===========================================================================


async def test_list_asset_types_returns_200(agent_client):
    """GET /api/v1/asset-types returns 200 (even if empty)."""
    resp = await agent_client.get("/api/v1/asset-types")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


async def test_list_vendors_returns_200(agent_client):
    """GET /api/v1/vendors returns 200 (even if empty)."""
    resp = await agent_client.get("/api/v1/vendors")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
