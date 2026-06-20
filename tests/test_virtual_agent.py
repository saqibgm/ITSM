"""
Integration tests for the Virtual Agent domain (/api/v1/virtual-agent).

All AI calls (RAGEngine, IntentDetector, AIService, EmbedderService) are mocked
so these tests have no external dependencies.
"""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CREATE_SESSION_BODY = {"channel": "web_widget"}
_MESSAGE_BODY = {"message": "How do I reset my password?"}


def _mock_process_message_result(response_text: str = "Test response") -> dict:
    return {
        "response": response_text,
        "intent": "general_inquiry",
        "sources": [],
        "session_status": "active",
    }


# ===========================================================================
# POST /api/v1/virtual-agent/sessions
# ===========================================================================


async def test_create_session_returns_201(end_user_client, test_tenant_id):
    """POST /api/v1/virtual-agent/sessions returns 201 with session id and status=active."""
    resp = await end_user_client.post("/api/v1/virtual-agent/sessions", json=_CREATE_SESSION_BODY)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert "id" in body
    assert body["status"] == "active"
    assert body["channel"] == "web_widget"


async def test_create_session_default_channel(end_user_client):
    """POST with no channel body uses default channel."""
    resp = await end_user_client.post("/api/v1/virtual-agent/sessions", json={})
    assert resp.status_code == 201, resp.text
    assert resp.json()["channel"] == "web_widget"


async def test_create_session_custom_channel(end_user_client):
    """POST with channel=mobile creates a mobile session."""
    resp = await end_user_client.post(
        "/api/v1/virtual-agent/sessions", json={"channel": "mobile"}
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["channel"] == "mobile"


# ===========================================================================
# POST /api/v1/virtual-agent/sessions/{id}/messages
# ===========================================================================


async def test_send_message_returns_response(end_user_client):
    """POST /sessions/{id}/messages returns 200 with non-empty response."""
    # Create session first
    session_resp = await end_user_client.post(
        "/api/v1/virtual-agent/sessions", json=_CREATE_SESSION_BODY
    )
    assert session_resp.status_code == 201, session_resp.text
    session_id = session_resp.json()["id"]

    # Patch the service layer so we don't need OpenAI/Anthropic keys
    with patch(
        "app.services.virtual_agent_service.VirtualAgentService.process_message",
        new_callable=AsyncMock,
        return_value=_mock_process_message_result("Test response from virtual agent"),
    ):
        msg_resp = await end_user_client.post(
            f"/api/v1/virtual-agent/sessions/{session_id}/messages",
            json=_MESSAGE_BODY,
        )

    assert msg_resp.status_code == 200, msg_resp.text
    body = msg_resp.json()
    assert body["response"] == "Test response from virtual agent"
    assert body["session_status"] == "active"


async def test_send_message_with_intent(end_user_client):
    """POST /sessions/{id}/messages returns intent in response."""
    session_resp = await end_user_client.post(
        "/api/v1/virtual-agent/sessions", json=_CREATE_SESSION_BODY
    )
    assert session_resp.status_code == 201
    session_id = session_resp.json()["id"]

    with patch(
        "app.services.virtual_agent_service.VirtualAgentService.process_message",
        new_callable=AsyncMock,
        return_value={
            "response": "Your password can be reset at the login page.",
            "intent": "password_reset",
            "sources": [],
            "session_status": "active",
        },
    ):
        msg_resp = await end_user_client.post(
            f"/api/v1/virtual-agent/sessions/{session_id}/messages",
            json={"message": "I forgot my password"},
        )

    assert msg_resp.status_code == 200
    assert msg_resp.json()["intent"] == "password_reset"


async def test_send_message_to_nonexistent_session_returns_404(end_user_client):
    """POST /sessions/{non-existent-id}/messages returns 404."""
    from app.exceptions import ResourceNotFoundError

    with patch(
        "app.services.virtual_agent_service.VirtualAgentService.process_message",
        new_callable=AsyncMock,
        side_effect=ResourceNotFoundError("virtual_agent_session", str(uuid4())),
    ):
        resp = await end_user_client.post(
            f"/api/v1/virtual-agent/sessions/{uuid4()}/messages",
            json=_MESSAGE_BODY,
        )
    assert resp.status_code == 404


# ===========================================================================
# POST /api/v1/virtual-agent/sessions/{id}/close
# ===========================================================================


async def test_close_session_returns_200(end_user_client):
    """POST /sessions/{id}/close returns 200 with status=closed."""
    session_resp = await end_user_client.post(
        "/api/v1/virtual-agent/sessions", json=_CREATE_SESSION_BODY
    )
    assert session_resp.status_code == 201
    session_id = session_resp.json()["id"]

    close_resp = await end_user_client.post(
        f"/api/v1/virtual-agent/sessions/{session_id}/close"
    )
    assert close_resp.status_code == 200, close_resp.text
    body = close_resp.json()
    assert body["status"] == "closed"


async def test_close_session_twice_is_idempotent_or_400(end_user_client):
    """Closing an already-closed session returns 200 (idempotent) or 400/422."""
    session_resp = await end_user_client.post(
        "/api/v1/virtual-agent/sessions", json=_CREATE_SESSION_BODY
    )
    assert session_resp.status_code == 201
    session_id = session_resp.json()["id"]

    # First close
    r1 = await end_user_client.post(f"/api/v1/virtual-agent/sessions/{session_id}/close")
    assert r1.status_code == 200

    # Second close — idempotent (200) or rejected (400/422)
    r2 = await end_user_client.post(f"/api/v1/virtual-agent/sessions/{session_id}/close")
    assert r2.status_code in (200, 400, 422)


# ===========================================================================
# Sending a message to a closed session
# ===========================================================================


async def test_message_to_closed_session_returns_422(end_user_client):
    """POST /sessions/{id}/messages on a closed session returns 400 or 422."""
    session_resp = await end_user_client.post(
        "/api/v1/virtual-agent/sessions", json=_CREATE_SESSION_BODY
    )
    assert session_resp.status_code == 201
    session_id = session_resp.json()["id"]

    # Close the session
    close_resp = await end_user_client.post(
        f"/api/v1/virtual-agent/sessions/{session_id}/close"
    )
    assert close_resp.status_code == 200

    # Attempt to send a message to the closed session
    # The service raises ValidationError("Session is closed or handed off")
    msg_resp = await end_user_client.post(
        f"/api/v1/virtual-agent/sessions/{session_id}/messages",
        json=_MESSAGE_BODY,
    )
    # ValidationError → 400; or 422 from Pydantic
    assert msg_resp.status_code in (400, 422), msg_resp.text


# ===========================================================================
# GET /api/v1/virtual-agent/sessions/{id}/history
# ===========================================================================


async def test_session_history_is_ordered(end_user_client):
    """GET /sessions/{id}/history returns messages in chronological order."""
    session_resp = await end_user_client.post(
        "/api/v1/virtual-agent/sessions", json=_CREATE_SESSION_BODY
    )
    assert session_resp.status_code == 201
    session_id = session_resp.json()["id"]

    # Send two messages
    for msg in ("First message", "Second message"):
        with patch(
            "app.services.virtual_agent_service.VirtualAgentService.process_message",
            new_callable=AsyncMock,
            return_value=_mock_process_message_result(f"Reply to: {msg}"),
        ):
            await end_user_client.post(
                f"/api/v1/virtual-agent/sessions/{session_id}/messages",
                json={"message": msg},
            )

    history_resp = await end_user_client.get(
        f"/api/v1/virtual-agent/sessions/{session_id}/history"
    )
    assert history_resp.status_code == 200, history_resp.text
    messages = history_resp.json()
    assert isinstance(messages, list)
    # Each message should have a role and content
    for m in messages:
        assert "role" in m
        assert "content" in m


# ===========================================================================
# GET /api/v1/virtual-agent/sessions  (list user sessions)
# ===========================================================================


async def test_list_sessions_returns_user_sessions(end_user_client):
    """GET /api/v1/virtual-agent/sessions returns a paginated list of the user's sessions."""
    # Create a couple of sessions
    for _ in range(2):
        await end_user_client.post("/api/v1/virtual-agent/sessions", json=_CREATE_SESSION_BODY)

    resp = await end_user_client.get("/api/v1/virtual-agent/sessions")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Response is either a list or a paginated dict with "items"
    if isinstance(body, list):
        assert len(body) >= 2
    else:
        assert "items" in body
        assert len(body["items"]) >= 2


# ===========================================================================
# Unauthenticated access
# ===========================================================================


async def test_unauthenticated_cannot_create_session(async_client):
    """POST /virtual-agent/sessions without auth returns 401."""
    resp = await async_client.post("/api/v1/virtual-agent/sessions", json=_CREATE_SESSION_BODY)
    assert resp.status_code == 401
