"""Inbound IAM webhook — user-lifecycle handling (Q2 phase 3).

Verifies that user.deactivated / reactivated / role_changed events immediately
update the user's ITSM mirror, plus HMAC enforcement + forward-compatibility.
The webhook secret is read from settings (never hardcoded)."""
import hashlib
import hmac
import json
from unittest.mock import AsyncMock, patch

import pytest

from app.config import get_settings

pytestmark = pytest.mark.asyncio

ORG = "org_test_acme_01"           # seeded_tenant.iam_org_id
AGENT_IAM = "iam_agent_fixed"      # seeded agent user


def _signed(payload: dict):
    body = json.dumps(payload).encode()
    sig = hmac.new(get_settings().IAM_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return body, {"X-IAM-Signature": f"sha256={sig}", "Content-Type": "application/json"}


async def _post(async_client, payload):
    body, headers = _signed(payload)
    return await async_client.post("/api/v1/webhooks/iam", content=body, headers=headers)


async def test_bad_signature_rejected(async_client):
    body = json.dumps({"event": "user.deactivated", "user_id": AGENT_IAM, "org_id": ORG}).encode()
    r = await async_client.post("/api/v1/webhooks/iam", content=body,
                                headers={"X-IAM-Signature": "sha256=deadbeef", "Content-Type": "application/json"})
    assert r.status_code == 401, r.text


async def test_unknown_event_is_forward_compatible(async_client, seeded_tenant):
    r = await _post(async_client, {"event": "something.new", "org_id": ORG})
    assert r.status_code == 200, r.text


async def test_unknown_user_is_noop(async_client, seeded_tenant):
    # Exercises the real _set_user_active path (its own session) — no mirror to
    # match → no-op, still 200. (Seeded users live in the test's uncommitted
    # session, invisible to the webhook's separate session, so we assert dispatch
    # via mocks below rather than cross-session DB state.)
    r = await _post(async_client, {"event": "user.deactivated", "user_id": "iam_nobody", "org_id": ORG})
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("event,active", [
    ("user.deactivated", False), ("user.suspended", False), ("user.deleted", False),
    ("member.removed", False), ("user.offboarded", False),
    ("user.reactivated", True), ("member.added", True), ("user.restored", True),
])
async def test_lifecycle_dispatches_set_user_active(async_client, event, active):
    with patch("app.api.v1.webhooks._set_user_active", new=AsyncMock()) as m:
        r = await _post(async_client, {"event": event, "user_id": AGENT_IAM, "org_id": ORG})
    assert r.status_code == 200, r.text
    m.assert_awaited_once_with(AGENT_IAM, ORG, is_active=active)


@pytest.mark.parametrize("event", ["user.role_changed", "user.updated", "user.roles_changed"])
async def test_role_change_dispatches_sync_roles(async_client, event):
    with patch("app.api.v1.webhooks._sync_user_roles", new=AsyncMock()) as m:
        r = await _post(async_client, {"event": event, "user_id": AGENT_IAM,
                                       "org_id": ORG, "roles": ["operator", "user"]})
    assert r.status_code == 200, r.text
    m.assert_awaited_once_with(AGENT_IAM, ORG, ["operator", "user"])
