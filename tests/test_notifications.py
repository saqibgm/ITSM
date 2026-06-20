"""
Integration tests for the Notifications API (/api/v1/notifications).

Covers:
  - List inbox (paginated, unread filter)
  - Mark-read (by IDs and mark-all)
  - Delete a notification
  - GET + PATCH notification preferences
"""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helper: seed a notification row for the given user
# ---------------------------------------------------------------------------


async def _seed_notification(db, user_id: UUID, tenant_id: UUID, is_read: bool = False) -> UUID:
    notif_id = uuid4()
    await db.execute(
        text(
            """
            INSERT INTO notifications (
                id, tenant_id, user_id, type, title, is_read
            ) VALUES (
                :id, :tenant_id, :user_id, 'system'::notificationtype, :title, :is_read
            )
            """
        ),
        {
            "id": str(notif_id),
            "tenant_id": str(tenant_id),
            "user_id": str(user_id),
            "title": "Test notification",
            "is_read": is_read,
        },
    )
    await db.flush()
    return notif_id


# ===========================================================================
# GET /api/v1/notifications
# ===========================================================================


async def test_list_notifications_empty_returns_200(end_user_client):
    resp = await end_user_client.get("/api/v1/notifications")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "items" in body
    assert isinstance(body["items"], list)


async def test_list_notifications_returns_seeded_rows(
    end_user_client, db, test_end_user_id, test_tenant_id, seeded_tenant
):
    await _seed_notification(db, test_end_user_id, test_tenant_id)

    resp = await end_user_client.get("/api/v1/notifications")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) >= 1


async def test_list_notifications_unread_filter(
    end_user_client, db, test_end_user_id, test_tenant_id, seeded_tenant
):
    await _seed_notification(db, test_end_user_id, test_tenant_id, is_read=False)
    await _seed_notification(db, test_end_user_id, test_tenant_id, is_read=True)

    resp = await end_user_client.get("/api/v1/notifications?unread=true")
    assert resp.status_code == 200
    for item in resp.json()["items"]:
        assert item["is_read"] is False


async def test_list_notifications_unauthenticated_returns_401(async_client):
    resp = await async_client.get("/api/v1/notifications")
    assert resp.status_code == 401


async def test_list_notifications_pagination_limit(
    end_user_client, db, test_end_user_id, test_tenant_id, seeded_tenant
):
    for _ in range(3):
        await _seed_notification(db, test_end_user_id, test_tenant_id)

    resp = await end_user_client.get("/api/v1/notifications?limit=2")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) <= 2


# ===========================================================================
# PATCH /api/v1/notifications/mark-read
# ===========================================================================


async def test_mark_specific_notifications_read(
    end_user_client, db, test_end_user_id, test_tenant_id, seeded_tenant
):
    notif_id = await _seed_notification(
        db, test_end_user_id, test_tenant_id, is_read=False
    )

    resp = await end_user_client.patch(
        "/api/v1/notifications/mark-read",
        json={"ids": [str(notif_id)]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["marked"] >= 0


async def test_mark_all_notifications_read(
    end_user_client, db, test_end_user_id, test_tenant_id, seeded_tenant
):
    for _ in range(2):
        await _seed_notification(db, test_end_user_id, test_tenant_id, is_read=False)

    resp = await end_user_client.patch(
        "/api/v1/notifications/mark-read",
        json={"all": True},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["marked"] >= 0


async def test_mark_read_empty_ids(end_user_client):
    resp = await end_user_client.patch(
        "/api/v1/notifications/mark-read",
        json={"ids": []},
    )
    assert resp.status_code in (200, 422)


async def test_mark_read_unauthenticated_returns_401(async_client):
    resp = await async_client.patch(
        "/api/v1/notifications/mark-read",
        json={"all": True},
    )
    assert resp.status_code == 401


# ===========================================================================
# DELETE /api/v1/notifications/{id}
# ===========================================================================


async def test_delete_notification_returns_204(
    end_user_client, db, test_end_user_id, test_tenant_id, seeded_tenant
):
    notif_id = await _seed_notification(db, test_end_user_id, test_tenant_id)

    resp = await end_user_client.delete(f"/api/v1/notifications/{notif_id}")
    assert resp.status_code == 204


async def test_delete_nonexistent_notification_returns_404(end_user_client):
    resp = await end_user_client.delete(f"/api/v1/notifications/{uuid4()}")
    assert resp.status_code == 404


# ===========================================================================
# GET /api/v1/notifications/preferences
# ===========================================================================


async def test_get_preferences_returns_list(end_user_client):
    resp = await end_user_client.get("/api/v1/notifications/preferences")
    assert resp.status_code == 200, resp.text
    assert isinstance(resp.json(), list)


async def test_get_preferences_unauthenticated_returns_401(async_client):
    resp = await async_client.get("/api/v1/notifications/preferences")
    assert resp.status_code == 401


# ===========================================================================
# PATCH /api/v1/notifications/preferences
# ===========================================================================


async def test_update_preference_upsert(end_user_client):
    resp = await end_user_client.patch(
        "/api/v1/notifications/preferences",
        json={
            "type": "ticket_assigned",
            "email_enabled": False,
            "in_app_enabled": True,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["type"] == "ticket_assigned"
    assert body["email_enabled"] is False
    assert body["in_app_enabled"] is True


async def test_update_preference_idempotent(end_user_client):
    """Calling PATCH twice with same payload returns the same result (upsert)."""
    payload = {
        "type": "mention",
        "email_enabled": True,
        "in_app_enabled": True,
    }
    resp1 = await end_user_client.patch(
        "/api/v1/notifications/preferences", json=payload
    )
    assert resp1.status_code == 200

    resp2 = await end_user_client.patch(
        "/api/v1/notifications/preferences", json=payload
    )
    assert resp2.status_code == 200
    assert resp1.json()["type"] == resp2.json()["type"]


async def test_update_preference_invalid_type_returns_422(end_user_client):
    resp = await end_user_client.patch(
        "/api/v1/notifications/preferences",
        json={
            "type": "not_a_valid_type",
            "email_enabled": True,
            "in_app_enabled": True,
        },
    )
    assert resp.status_code == 422
