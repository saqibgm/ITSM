"""
Integration tests for reference-data management endpoints added in the
"final push" effort. These back the grouped Administration hub UI.

Covered route groups:
  - /asset-categories                    (assets.py)
  - /tenant/departments                  (admin.py)
  - /tenant/ticket-categories            (admin.py)
  - /kb/spaces/{id}/categories + /kb/categories/{id}   (kb.py)
  - /kb/tags + /kb/articles/{id}/tags    (kb.py)
  - /kb/articles/{id}/attachments        (kb.py)

Fixtures from conftest:
  - tenant_admin_client — admin role (all operations)
  - agent_client        — agent role (limited: tags yes, categories/depts no)
  - end_user_client     — end_user role (read-only)
"""

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_space_id(client) -> str:
    body = {
        "name": "Ref Data Space",
        "slug": f"refdata-{uuid4().hex[:8]}",
        "description": "Space for reference-data tests",
        "is_public": True,
    }
    resp = await client.post("/api/v1/kb/spaces", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _create_article_id(client, space_id) -> str:
    body = {
        "space_id": str(space_id),
        "title": "Reference data article",
        "body": "Body content for reference-data attachment/tag tests.",
        "visibility": "public",
    }
    resp = await client.post("/api/v1/kb/articles", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


# ===========================================================================
# Asset Categories — /api/v1/asset-categories
# ===========================================================================


async def test_create_asset_category_returns_201(tenant_admin_client):
    resp = await tenant_admin_client.post(
        "/api/v1/asset-categories",
        json={"name": "Networking", "description": "Switches and routers", "icon": "wifi"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "Networking"
    assert body["is_active"] is True
    assert body["parent_id"] is None


async def test_list_asset_categories(tenant_admin_client):
    await tenant_admin_client.post("/api/v1/asset-categories", json={"name": "Peripherals"})
    resp = await tenant_admin_client.get("/api/v1/asset-categories")
    assert resp.status_code == 200, resp.text
    assert any(c["name"] == "Peripherals" for c in resp.json())


async def test_create_asset_category_with_parent(tenant_admin_client):
    parent = (
        await tenant_admin_client.post("/api/v1/asset-categories", json={"name": "Hardware"})
    ).json()
    resp = await tenant_admin_client.post(
        "/api/v1/asset-categories",
        json={"name": "Laptops", "parent_id": parent["id"]},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["parent_id"] == parent["id"]


async def test_create_asset_category_bad_parent_404(tenant_admin_client):
    resp = await tenant_admin_client.post(
        "/api/v1/asset-categories",
        json={"name": "Orphan", "parent_id": str(uuid4())},
    )
    assert resp.status_code == 404, resp.text


async def test_update_asset_category(tenant_admin_client):
    cat = (
        await tenant_admin_client.post("/api/v1/asset-categories", json={"name": "Old"})
    ).json()
    resp = await tenant_admin_client.patch(
        f"/api/v1/asset-categories/{cat['id']}",
        json={"name": "New", "is_active": False},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "New"
    assert resp.json()["is_active"] is False


async def test_asset_category_cannot_be_own_parent(tenant_admin_client):
    cat = (
        await tenant_admin_client.post("/api/v1/asset-categories", json={"name": "Self"})
    ).json()
    resp = await tenant_admin_client.patch(
        f"/api/v1/asset-categories/{cat['id']}", json={"parent_id": cat["id"]}
    )
    assert resp.status_code == 403, resp.text


async def test_create_asset_category_requires_manager(agent_client):
    resp = await agent_client.post("/api/v1/asset-categories", json={"name": "Nope"})
    assert resp.status_code == 403


async def test_get_asset_category_404(tenant_admin_client):
    resp = await tenant_admin_client.get(f"/api/v1/asset-categories/{uuid4()}")
    assert resp.status_code == 404


# ===========================================================================
# Global (platform) scoping — reference data with tenant_id NULL
# ===========================================================================


async def test_platform_creates_global_asset_category(platform_admin_client, tenant_admin_client):
    # Platform user (all-tenants view) creates a GLOBAL category (tenant_id NULL)
    resp = await platform_admin_client.post(
        "/api/v1/asset-categories", json={"name": "Global Hardware"}
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["tenant_id"] is None  # global

    # A tenant user sees the global category in their list
    lst = await tenant_admin_client.get("/api/v1/asset-categories")
    assert lst.status_code == 200
    names = [c["name"] for c in lst.json()]
    assert "Global Hardware" in names


async def test_tenant_cannot_modify_global_reference(platform_admin_client, tenant_admin_client):
    created = await platform_admin_client.post(
        "/api/v1/asset-categories", json={"name": "Locked Global"}
    )
    gid = created.json()["id"]
    assert created.json()["tenant_id"] is None

    # Tenant admin can SEE it but NOT modify it (global is platform-managed)
    upd = await tenant_admin_client.patch(f"/api/v1/asset-categories/{gid}", json={"name": "Hacked"})
    assert upd.status_code == 403, upd.text


async def test_platform_creates_global_vendor_and_department(platform_admin_client):
    v = await platform_admin_client.post("/api/v1/vendors", json={"name": "Global Vendor"})
    assert v.status_code == 201 and v.json()["tenant_id"] is None

    d = await platform_admin_client.post("/api/v1/tenant/departments", json={"name": "Shared Dept"})
    assert d.status_code == 201 and d.json()["tenant_id"] is None

    tc = await platform_admin_client.post("/api/v1/tenant/ticket-categories", json={"name": "Shared Cat"})
    assert tc.status_code == 201 and tc.json()["tenant_id"] is None


async def test_tenant_create_is_tenant_scoped(tenant_admin_client):
    # A tenant admin's create is stamped with their tenant (not global)
    resp = await tenant_admin_client.post("/api/v1/asset-categories", json={"name": "Tenant Cat"})
    assert resp.status_code == 201, resp.text
    assert resp.json()["tenant_id"] is not None


async def test_duplicate_name_rejected_within_scope(tenant_admin_client):
    name = f"Dup-{uuid4().hex[:6]}"
    r1 = await tenant_admin_client.post("/api/v1/asset-categories", json={"name": name})
    assert r1.status_code == 201, r1.text
    # Second create with the same name in the same tenant scope -> rejected by
    # the partial unique index (surfaces as a 4xx, not a second row).
    r2 = await tenant_admin_client.post("/api/v1/asset-categories", json={"name": name})
    assert r2.status_code >= 400, r2.text


# ===========================================================================
# Departments — /api/v1/tenant/departments
# ===========================================================================


async def test_create_department_returns_201(tenant_admin_client):
    resp = await tenant_admin_client.post(
        "/api/v1/tenant/departments",
        json={"name": "IT Support", "description": "Helpdesk team"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["name"] == "IT Support"
    assert resp.json()["is_active"] is True


async def test_list_departments(tenant_admin_client):
    await tenant_admin_client.post("/api/v1/tenant/departments", json={"name": "Finance"})
    resp = await tenant_admin_client.get("/api/v1/tenant/departments")
    assert resp.status_code == 200, resp.text
    assert any(d["name"] == "Finance" for d in resp.json())


async def test_department_hierarchy(tenant_admin_client):
    parent = (
        await tenant_admin_client.post("/api/v1/tenant/departments", json={"name": "HQ"})
    ).json()
    resp = await tenant_admin_client.post(
        "/api/v1/tenant/departments",
        json={"name": "Branch", "parent_id": parent["id"]},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["parent_id"] == parent["id"]


async def test_update_department(tenant_admin_client):
    dept = (
        await tenant_admin_client.post("/api/v1/tenant/departments", json={"name": "Temp"})
    ).json()
    resp = await tenant_admin_client.patch(
        f"/api/v1/tenant/departments/{dept['id']}", json={"is_active": False}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_active"] is False


async def test_create_department_requires_manager(agent_client):
    resp = await agent_client.post("/api/v1/tenant/departments", json={"name": "Nope"})
    assert resp.status_code == 403


async def test_update_department_404(tenant_admin_client):
    resp = await tenant_admin_client.patch(
        f"/api/v1/tenant/departments/{uuid4()}", json={"name": "Ghost"}
    )
    assert resp.status_code == 404


# ===========================================================================
# Ticket Categories — /api/v1/tenant/ticket-categories
# ===========================================================================


async def test_create_ticket_category_returns_201(tenant_admin_client):
    resp = await tenant_admin_client.post(
        "/api/v1/tenant/ticket-categories",
        json={"name": "Network Issue", "description": "Connectivity problems"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["name"] == "Network Issue"
    assert resp.json()["is_active"] is True


async def test_list_ticket_categories(tenant_admin_client):
    await tenant_admin_client.post(
        "/api/v1/tenant/ticket-categories", json={"name": "Hardware Fault"}
    )
    resp = await tenant_admin_client.get("/api/v1/tenant/ticket-categories")
    assert resp.status_code == 200, resp.text
    assert any(c["name"] == "Hardware Fault" for c in resp.json())


async def test_update_ticket_category(tenant_admin_client):
    cat = (
        await tenant_admin_client.post(
            "/api/v1/tenant/ticket-categories", json={"name": "Access Request"}
        )
    ).json()
    resp = await tenant_admin_client.patch(
        f"/api/v1/tenant/ticket-categories/{cat['id']}",
        json={"name": "Access Management"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "Access Management"


async def test_create_ticket_category_requires_manager(agent_client):
    resp = await agent_client.post(
        "/api/v1/tenant/ticket-categories", json={"name": "Nope"}
    )
    assert resp.status_code == 403


# ===========================================================================
# KB Categories — /api/v1/kb/spaces/{id}/categories + /kb/categories/{id}
# ===========================================================================


async def test_create_kb_category_returns_201(tenant_admin_client):
    space_id = await _create_space_id(tenant_admin_client)
    resp = await tenant_admin_client.post(
        f"/api/v1/kb/spaces/{space_id}/categories",
        json={"name": "Getting Started", "display_order": 1},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["name"] == "Getting Started"
    assert resp.json()["space_id"] == space_id


async def test_list_kb_categories(tenant_admin_client):
    space_id = await _create_space_id(tenant_admin_client)
    await tenant_admin_client.post(
        f"/api/v1/kb/spaces/{space_id}/categories", json={"name": "FAQ"}
    )
    resp = await tenant_admin_client.get(f"/api/v1/kb/spaces/{space_id}/categories")
    assert resp.status_code == 200, resp.text
    assert any(c["name"] == "FAQ" for c in resp.json())


async def test_update_and_delete_kb_category(tenant_admin_client):
    space_id = await _create_space_id(tenant_admin_client)
    cat = (
        await tenant_admin_client.post(
            f"/api/v1/kb/spaces/{space_id}/categories", json={"name": "Tmp"}
        )
    ).json()
    upd = await tenant_admin_client.patch(
        f"/api/v1/kb/categories/{cat['id']}", json={"name": "Renamed"}
    )
    assert upd.status_code == 200, upd.text
    assert upd.json()["name"] == "Renamed"

    dele = await tenant_admin_client.delete(f"/api/v1/kb/categories/{cat['id']}")
    assert dele.status_code == 204, dele.text


async def test_create_kb_category_requires_manager(agent_client, tenant_admin_client):
    space_id = await _create_space_id(tenant_admin_client)
    resp = await agent_client.post(
        f"/api/v1/kb/spaces/{space_id}/categories", json={"name": "Nope"}
    )
    assert resp.status_code == 403


async def test_create_kb_category_bad_space_404(tenant_admin_client):
    resp = await tenant_admin_client.post(
        f"/api/v1/kb/spaces/{uuid4()}/categories", json={"name": "Ghost"}
    )
    assert resp.status_code == 404


# ===========================================================================
# KB Tags — /api/v1/kb/tags + /kb/articles/{id}/tags
# ===========================================================================


async def test_create_kb_tag_returns_201(agent_client):
    resp = await agent_client.post("/api/v1/kb/tags", json={"name": "howto"})
    assert resp.status_code == 201, resp.text
    assert resp.json()["name"] == "howto"


async def test_kb_tag_duplicate_rejected(agent_client):
    name = f"dup-{uuid4().hex[:6]}"
    r1 = await agent_client.post("/api/v1/kb/tags", json={"name": name})
    assert r1.status_code == 201, r1.text
    r2 = await agent_client.post("/api/v1/kb/tags", json={"name": name})
    assert r2.status_code == 400, r2.text


async def test_list_and_rename_and_delete_kb_tag(agent_client):
    tag = (await agent_client.post("/api/v1/kb/tags", json={"name": "temp-tag"})).json()
    lst = await agent_client.get("/api/v1/kb/tags")
    assert lst.status_code == 200
    assert any(t["id"] == tag["id"] for t in lst.json())

    upd = await agent_client.patch(
        f"/api/v1/kb/tags/{tag['id']}", json={"name": "renamed-tag"}
    )
    assert upd.status_code == 200, upd.text
    assert upd.json()["name"] == "renamed-tag"

    dele = await agent_client.delete(f"/api/v1/kb/tags/{tag['id']}")
    assert dele.status_code == 204, dele.text


async def test_create_kb_tag_requires_agent(end_user_client):
    resp = await end_user_client.post("/api/v1/kb/tags", json={"name": "nope"})
    assert resp.status_code == 403


async def test_assign_and_unassign_article_tag(tenant_admin_client):
    agent_client = tenant_admin_client  # admin satisfies agent-level tag ops
    space_id = await _create_space_id(agent_client)
    article_id = await _create_article_id(agent_client, space_id)
    tag = (await agent_client.post("/api/v1/kb/tags", json={"name": "tagme"})).json()

    assign = await agent_client.post(
        f"/api/v1/kb/articles/{article_id}/tags", json={"tag_id": tag["id"]}
    )
    assert assign.status_code == 201, assign.text
    assert any(t["id"] == tag["id"] for t in assign.json())

    # Idempotent re-assign
    again = await agent_client.post(
        f"/api/v1/kb/articles/{article_id}/tags", json={"tag_id": tag["id"]}
    )
    assert again.status_code == 201, again.text
    assert len([t for t in again.json() if t["id"] == tag["id"]]) == 1

    listed = await agent_client.get(f"/api/v1/kb/articles/{article_id}/tags")
    assert listed.status_code == 200
    assert any(t["id"] == tag["id"] for t in listed.json())

    unassign = await agent_client.delete(
        f"/api/v1/kb/articles/{article_id}/tags/{tag['id']}"
    )
    assert unassign.status_code == 204, unassign.text


# ===========================================================================
# KB Article Attachments — /api/v1/kb/articles/{id}/attachments
# ===========================================================================


async def test_create_article_attachment_presign(tenant_admin_client):
    agent_client = tenant_admin_client  # admin satisfies agent-level attachment ops
    space_id = await _create_space_id(agent_client)
    article_id = await _create_article_id(agent_client, space_id)

    mock_storage = AsyncMock()
    mock_storage.presigned_upload_url = AsyncMock(
        return_value=("https://minio.local/upload-url", "uploads/quarantine/x/abc.pdf")
    )
    with patch("app.api.v1.kb.get_storage_service", return_value=mock_storage):
        resp = await agent_client.post(
            f"/api/v1/kb/articles/{article_id}/attachments",
            json={"filename": "manual.pdf", "file_size": 1024, "mime_type": "application/pdf"},
        )
    assert resp.status_code == 201, resp.text
    assert resp.json()["upload_url"] == "https://minio.local/upload-url"
    assert "attachment_id" in resp.json()

    listed = await agent_client.get(f"/api/v1/kb/articles/{article_id}/attachments")
    assert listed.status_code == 200, listed.text
    assert len(listed.json()) == 1
    assert listed.json()[0]["filename"] == "manual.pdf"


# ===========================================================================
# Team members — /api/v1/tenant/teams/{id}/members
# ===========================================================================


async def _create_team(client) -> str:
    resp = await client.post("/api/v1/tenant/teams", json={"name": f"Team {uuid4().hex[:6]}"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def test_add_list_remove_team_member(tenant_admin_client, test_agent_user_id):
    team_id = await _create_team(tenant_admin_client)

    # initially empty
    lst = await tenant_admin_client.get(f"/api/v1/tenant/teams/{team_id}/members")
    assert lst.status_code == 200 and lst.json() == []

    add = await tenant_admin_client.post(
        f"/api/v1/tenant/teams/{team_id}/members", json={"user_id": str(test_agent_user_id)}
    )
    assert add.status_code == 201, add.text
    assert add.json()["user_id"] == str(test_agent_user_id)
    assert add.json()["email"]

    # idempotent re-add
    again = await tenant_admin_client.post(
        f"/api/v1/tenant/teams/{team_id}/members", json={"user_id": str(test_agent_user_id)}
    )
    assert again.status_code == 201, again.text

    lst2 = await tenant_admin_client.get(f"/api/v1/tenant/teams/{team_id}/members")
    assert len(lst2.json()) == 1

    rem = await tenant_admin_client.delete(f"/api/v1/tenant/teams/{team_id}/members/{test_agent_user_id}")
    assert rem.status_code == 204, rem.text
    lst3 = await tenant_admin_client.get(f"/api/v1/tenant/teams/{team_id}/members")
    assert lst3.json() == []


async def test_add_team_member_unknown_user_404(tenant_admin_client):
    team_id = await _create_team(tenant_admin_client)
    resp = await tenant_admin_client.post(
        f"/api/v1/tenant/teams/{team_id}/members", json={"user_id": str(uuid4())}
    )
    assert resp.status_code == 404, resp.text


async def test_add_team_member_requires_manager(agent_client, tenant_admin_client, test_agent_user_id):
    team_id = await _create_team(tenant_admin_client)
    resp = await agent_client.post(
        f"/api/v1/tenant/teams/{team_id}/members", json={"user_id": str(test_agent_user_id)}
    )
    assert resp.status_code == 403


# ===========================================================================
# System logs — /api/v1/tenant/system-logs (admin only)
# ===========================================================================


async def test_system_logs_admin_only(agent_client):
    resp = await agent_client.get("/api/v1/tenant/system-logs")
    assert resp.status_code == 403


async def test_system_logs_list_excludes_stack_trace(tenant_admin_client, db, test_tenant_id):
    from sqlalchemy import text
    await db.execute(
        text(
            """
            INSERT INTO system_logs (id, tenant_id, level, event, message, stack_trace, metadata)
            VALUES (:id, :tid, 'error', 'db_timeout', 'Statement timed out', 'SECRET TRACE', '{}'::jsonb)
            """
        ),
        {"id": str(uuid4()), "tid": str(test_tenant_id)},
    )
    await db.flush()

    resp = await tenant_admin_client.get("/api/v1/tenant/system-logs")
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert any(r["event"] == "db_timeout" for r in rows)
    # stack_trace must NEVER be forwarded
    assert all("stack_trace" not in r for r in rows)


async def test_system_logs_level_filter(tenant_admin_client, db, test_tenant_id):
    from sqlalchemy import text
    for lvl, evt in [("warning", "w_evt"), ("critical", "c_evt")]:
        await db.execute(
            text(
                """
                INSERT INTO system_logs (id, tenant_id, level, event, message, metadata)
                VALUES (:id, :tid, :lvl, :evt, 'msg', '{}'::jsonb)
                """
            ),
            {"id": str(uuid4()), "tid": str(test_tenant_id), "lvl": lvl, "evt": evt},
        )
    await db.flush()

    resp = await tenant_admin_client.get("/api/v1/tenant/system-logs", params={"level": "critical"})
    assert resp.status_code == 200, resp.text
    events = {r["event"] for r in resp.json()}
    assert "c_evt" in events and "w_evt" not in events


# ===========================================================================
# KB feedback list — /api/v1/kb/articles/{id}/feedback
# ===========================================================================


async def test_list_article_feedback(tenant_admin_client):
    space_id = await _create_space_id(tenant_admin_client)
    article_id = await _create_article_id(tenant_admin_client, space_id)
    # publish so feedback is accepted
    pub = await tenant_admin_client.post(f"/api/v1/kb/articles/{article_id}/publish")
    assert pub.status_code == 200, pub.text

    fb = await tenant_admin_client.post(
        f"/api/v1/kb/articles/{article_id}/feedback",
        json={"is_helpful": True, "comment": "Very clear"},
    )
    assert fb.status_code == 201, fb.text

    listed = await tenant_admin_client.get(f"/api/v1/kb/articles/{article_id}/feedback")
    assert listed.status_code == 200, listed.text
    assert len(listed.json()) == 1
    assert listed.json()[0]["comment"] == "Very clear"
    assert listed.json()[0]["is_helpful"] is True


async def test_list_article_feedback_requires_agent(end_user_client, tenant_admin_client):
    space_id = await _create_space_id(tenant_admin_client)
    article_id = await _create_article_id(tenant_admin_client, space_id)
    resp = await end_user_client.get(f"/api/v1/kb/articles/{article_id}/feedback")
    assert resp.status_code == 403


async def test_delete_article_attachment(tenant_admin_client):
    agent_client = tenant_admin_client  # admin satisfies agent-level attachment ops
    space_id = await _create_space_id(agent_client)
    article_id = await _create_article_id(agent_client, space_id)

    mock_storage = AsyncMock()
    mock_storage.presigned_upload_url = AsyncMock(
        return_value=("https://minio.local/u", "uploads/quarantine/x/y.png")
    )
    with patch("app.api.v1.kb.get_storage_service", return_value=mock_storage):
        created = await agent_client.post(
            f"/api/v1/kb/articles/{article_id}/attachments",
            json={"filename": "photo.png", "file_size": 2048, "mime_type": "image/png"},
        )
    att_id = created.json()["attachment_id"]

    dele = await agent_client.delete(
        f"/api/v1/kb/articles/{article_id}/attachments/{att_id}"
    )
    assert dele.status_code == 204, dele.text

    listed = await agent_client.get(f"/api/v1/kb/articles/{article_id}/attachments")
    assert listed.json() == []
