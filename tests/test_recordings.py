"""Support Session Recording module (specs/08, Phase 1) — linking, access
logging, permissions, consent enforcement."""
from unittest.mock import patch

import pytest

_TICKET_BODY = {
    "title": "Customer escalation — needs screen share",
    "description": "Customer reported repeated login failures.",
    "type": "incident",
    "priority": "medium",
}


async def _create_ticket(client):
    with patch("app.workers.tasks_ai_ticket.process_new_ticket.delay", return_value=None):
        r = await client.post("/api/v1/tickets", json=_TICKET_BODY)
    assert r.status_code == 201, r.text
    return r.json()


@pytest.mark.asyncio
async def test_link_from_url_classifies_teams_source(agent_client):
    ticket = await _create_ticket(agent_client)
    r = await agent_client.post("/api/v1/support-recordings/link-from-url", json={
        "ticket_id": ticket["id"],
        "url": "https://teams.microsoft.com/l/meetup-join/abc123",
        "title": "Support call recording",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source_type"] == "teams"
    assert body["status"] == "linked"
    assert body["consent_status"] == "not_required"


@pytest.mark.asyncio
async def test_link_from_url_classifies_sharepoint_and_onedrive(agent_client):
    ticket = await _create_ticket(agent_client)
    sp = await agent_client.post("/api/v1/support-recordings/link-from-url", json={
        "ticket_id": ticket["id"], "url": "https://contoso.sharepoint.com/recording.mp4", "title": "SP rec",
    })
    assert sp.json()["source_type"] == "sharepoint"

    od = await agent_client.post("/api/v1/support-recordings/link-from-url", json={
        "ticket_id": ticket["id"], "url": "https://onedrive.live.com/recording.mp4", "title": "OD rec",
    })
    assert od.json()["source_type"] == "onedrive"


@pytest.mark.asyncio
async def test_link_from_url_rejects_unsupported_host(agent_client):
    ticket = await _create_ticket(agent_client)
    r = await agent_client.post("/api/v1/support-recordings/link-from-url", json={
        "ticket_id": ticket["id"], "url": "https://youtube.com/watch?v=abc", "title": "Not supported",
    })
    assert r.status_code == 400, r.text


@pytest.mark.asyncio
async def test_viewing_recording_writes_access_log(agent_client, tenant_admin_client):
    ticket = await _create_ticket(agent_client)
    rec = (await agent_client.post("/api/v1/support-recordings/link-from-url", json={
        "ticket_id": ticket["id"], "url": "https://teams.microsoft.com/l/meetup-join/xyz", "title": "Rec",
    })).json()

    await agent_client.get(f"/api/v1/support-recordings/{rec['id']}")

    log = await tenant_admin_client.get(f"/api/v1/support-recordings/{rec['id']}/access-log")
    assert log.status_code == 200, log.text
    actions = [row["action"] for row in log.json()["items"]]
    assert "viewed_metadata" in actions
    assert len(actions) >= 2  # one from linking, one from the GET above


@pytest.mark.asyncio
async def test_agent_cannot_view_access_log(agent_client):
    ticket = await _create_ticket(agent_client)
    rec = (await agent_client.post("/api/v1/support-recordings/link-from-url", json={
        "ticket_id": ticket["id"], "url": "https://teams.microsoft.com/l/meetup-join/xyz", "title": "Rec",
    })).json()
    r = await agent_client.get(f"/api/v1/support-recordings/{rec['id']}/access-log")
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_use_as_rca_evidence_toggle(agent_client):
    ticket = await _create_ticket(agent_client)
    rec = (await agent_client.post("/api/v1/support-recordings/link-from-url", json={
        "ticket_id": ticket["id"], "url": "https://teams.microsoft.com/l/meetup-join/xyz", "title": "Rec",
    })).json()

    links = (await agent_client.get(f"/api/v1/tickets/{ticket['id']}/recordings")).json()["items"]
    assert len(links) == 1 and links[0]["evidence_weight"] == "none"

    patch_r = await agent_client.patch(
        f"/api/v1/tickets/{ticket['id']}/recordings/{rec['id']}", json={"evidence_weight": "required_for_rca"}
    )
    assert patch_r.status_code == 200, patch_r.text

    links2 = (await agent_client.get(f"/api/v1/tickets/{ticket['id']}/recordings")).json()["items"]
    assert links2[0]["evidence_weight"] == "required_for_rca"


@pytest.mark.asyncio
async def test_consent_blocking_policy(tenant_admin_client, agent_client):
    policy = await tenant_admin_client.patch("/api/v1/tenant/recording-policies", json={
        "block_link_if_missing_consent": True,
    })
    assert policy.status_code == 200, policy.text

    ticket = await _create_ticket(agent_client)
    blocked = await agent_client.post("/api/v1/support-recordings/link-from-url", json={
        "ticket_id": ticket["id"], "url": "https://teams.microsoft.com/l/meetup-join/xyz",
        "title": "Rec", "consent_status": "missing",
    })
    assert blocked.status_code == 400, blocked.text


@pytest.mark.asyncio
async def test_unlink_does_not_delete_recording(agent_client):
    ticket = await _create_ticket(agent_client)
    rec = (await agent_client.post("/api/v1/support-recordings/link-from-url", json={
        "ticket_id": ticket["id"], "url": "https://teams.microsoft.com/l/meetup-join/xyz", "title": "Rec",
    })).json()

    unlinked = await agent_client.delete(f"/api/v1/tickets/{ticket['id']}/recordings/{rec['id']}")
    assert unlinked.status_code == 200, unlinked.text

    still_exists = await agent_client.get(f"/api/v1/support-recordings/{rec['id']}")
    assert still_exists.status_code == 200


@pytest.mark.asyncio
async def test_recordings_dashboard_summary(agent_client):
    ticket = await _create_ticket(agent_client)
    await agent_client.post("/api/v1/support-recordings/link-from-url", json={
        "ticket_id": ticket["id"], "url": "https://teams.microsoft.com/l/meetup-join/xyz", "title": "Rec",
    })
    r = await agent_client.get("/api/v1/dashboards/recordings/summary")
    assert r.status_code == 200
    assert r.json()["recordings"] >= 1
