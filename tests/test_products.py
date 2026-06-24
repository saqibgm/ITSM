"""Product entity (IAM-synced + manual) — CRUD, IAM-delete guard, and that the
IAM sync never touches manual products. Backs the admin Products UI + the bot's
product picker (which now reads /tenant/products = IAM + manual)."""
import pytest

pytestmark = pytest.mark.asyncio


async def _sync(client, slugs):
    return await client.post("/api/v1/tenant/products/sync",
                             json={"products": [{"slug": s, "name": s.title()} for s in slugs]})


# ── Create / list (manual) ────────────────────────────────────────────────────
async def test_create_manual_product(tenant_admin_client):
    r = await tenant_admin_client.post("/api/v1/tenant/products",
                                       json={"name": "Custom Portal"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["source"] == "manual"
    assert body["slug"] == "custom-portal"
    assert body["is_active"] is True


async def test_create_product_explicit_slug(tenant_admin_client):
    r = await tenant_admin_client.post("/api/v1/tenant/products",
                                       json={"name": "My Tool", "slug": "My Tool 2"})
    assert r.status_code == 201, r.text
    assert r.json()["slug"] == "my-tool-2"


async def test_duplicate_slug_rejected(tenant_admin_client):
    await tenant_admin_client.post("/api/v1/tenant/products", json={"name": "Dup", "slug": "dup-x"})
    r = await tenant_admin_client.post("/api/v1/tenant/products", json={"name": "Dup2", "slug": "dup-x"})
    assert r.status_code in (400, 422), r.text


async def test_agent_cannot_create_product(agent_client):
    r = await agent_client.post("/api/v1/tenant/products", json={"name": "Nope"})
    assert r.status_code == 403, r.text


# ── Delete guard (IAM protected) ───────────────────────────────────────────────
async def test_delete_manual_product(tenant_admin_client):
    c = await tenant_admin_client.post("/api/v1/tenant/products", json={"name": "Throwaway"})
    pid = c.json()["id"]
    d = await tenant_admin_client.delete(f"/api/v1/tenant/products/{pid}")
    assert d.status_code == 204, d.text


async def test_cannot_delete_iam_product(tenant_admin_client):
    await _sync(tenant_admin_client, ["greenloop"])
    lst = (await tenant_admin_client.get("/api/v1/tenant/products")).json()
    iam = next(p for p in lst if p["slug"] == "greenloop")
    assert iam["source"] == "iam"
    d = await tenant_admin_client.delete(f"/api/v1/tenant/products/{iam['id']}")
    assert d.status_code in (400, 422), d.text          # IAM products are undeletable


# ── Sync isolation ──────────────────────────────────────────────────────────────
async def test_sync_does_not_touch_manual_products(tenant_admin_client):
    # Manual product, then a sync whose subscription list does NOT include it.
    m = await tenant_admin_client.post("/api/v1/tenant/products", json={"name": "Manual Keep", "slug": "manual-keep"})
    assert m.status_code == 201
    await _sync(tenant_admin_client, ["greenloop", "account-wise"])
    lst = (await tenant_admin_client.get("/api/v1/tenant/products")).json()
    keep = next((p for p in lst if p["slug"] == "manual-keep"), None)
    assert keep is not None and keep["is_active"] is True and keep["source"] == "manual"


async def test_sync_marks_products_iam(tenant_admin_client):
    await _sync(tenant_admin_client, ["stock-wise"])
    lst = (await tenant_admin_client.get("/api/v1/tenant/products")).json()
    p = next(p for p in lst if p["slug"] == "stock-wise")
    assert p["source"] == "iam"
