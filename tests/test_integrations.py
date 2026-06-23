"""Integration marketplace (4b) — catalog endpoint, Teams payload transform,
and the webhook-endpoint format/secret behaviour that backs it."""
import json

from app.services.webhook_service import _teams_card, format_payload

# asyncio_mode=auto (pyproject) runs the async tests; no module mark needed, so
# the pure sync transform tests below don't get a spurious asyncio warning.


# ── Payload transform (pure) ──────────────────────────────────────────────────
def test_generic_format_signs_and_passes_payload_through():
    payload = {"ticket_number": "INC-1", "status": "open"}
    body, should_sign = format_payload("generic", "ticket.created", payload)
    assert should_sign is True
    assert json.loads(body) == payload


def test_teams_format_builds_unsigned_messagecard():
    payload = {"ticket_number": "INC-1", "title": "Printer down", "status": "open"}
    body, should_sign = format_payload("teams", "ticket.created", payload)
    assert should_sign is False                      # Teams URL is the secret
    card = json.loads(body)
    assert card["@type"] == "MessageCard"
    assert card["title"] == "Ticket Created"
    facts = {f["name"]: f["value"] for f in card["sections"][0]["facts"]}
    assert facts["ticket_number"] == "INC-1"
    assert facts["status"] == "open"


def test_teams_card_skips_nested_and_null_fields():
    card = _teams_card("ticket.updated", {"a": "x", "nested": {"k": 1}, "items": [1], "z": None})
    names = {f["name"] for f in card["sections"][0]["facts"]}
    assert names == {"a"}                            # dict/list/None dropped


def test_unknown_format_falls_back_to_generic():
    body, should_sign = format_payload("bogus", "ping", {"test": True})
    assert should_sign is True
    assert json.loads(body) == {"test": True}


# ── Catalog endpoint ──────────────────────────────────────────────────────────
async def test_catalog_lists_microsoft_teams(agent_client):
    r = await agent_client.get("/api/v1/integrations/catalog")
    assert r.status_code == 200, r.text
    data = r.json()
    keys = {i["key"] for i in data["integrations"]}
    assert "microsoft_teams" in keys and "generic_webhook" in keys
    teams = next(i for i in data["integrations"] if i["key"] == "microsoft_teams")
    assert teams["format"] == "teams"
    assert "ticket.created" in data["event_types"]


async def test_catalog_requires_auth(async_client):
    r = await async_client.get("/api/v1/integrations/catalog")
    assert r.status_code == 401


# ── Endpoint creation with format/integration_key ─────────────────────────────
async def test_create_teams_endpoint_without_secret(tenant_admin_client):
    r = await tenant_admin_client.post("/api/v1/webhooks-config/endpoints", json={
        "name": "Ops Teams channel",
        "url": "https://contoso.webhook.office.com/webhookb2/abc",
        "events": ["ticket.created"],
        "format": "teams",
        "integration_key": "microsoft_teams",
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["format"] == "teams"
    assert body["integration_key"] == "microsoft_teams"
    assert body["secret"] == "*****"                 # auto-generated, still masked


async def test_create_generic_endpoint_without_secret_rejected(tenant_admin_client):
    r = await tenant_admin_client.post("/api/v1/webhooks-config/endpoints", json={
        "name": "No secret",
        "url": "https://hooks.example.com/x",
        "events": [],
        "format": "generic",
    })
    assert r.status_code in (400, 422), r.text


async def test_create_endpoint_invalid_format_rejected(tenant_admin_client):
    r = await tenant_admin_client.post("/api/v1/webhooks-config/endpoints", json={
        "name": "Bad format",
        "url": "https://hooks.example.com/x",
        "secret": "supersecretvalue12345",
        "format": "carrier-pigeon",
    })
    assert r.status_code == 422, r.text


async def test_default_format_is_generic(tenant_admin_client):
    r = await tenant_admin_client.post("/api/v1/webhooks-config/endpoints", json={
        "name": "Default",
        "url": "https://hooks.example.com/x",
        "secret": "supersecretvalue12345",
        "events": [],
    })
    assert r.status_code == 201, r.text
    assert r.json()["format"] == "generic"
