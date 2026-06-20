"""Socket.IO ASGI application (S4B.2).

Exposes a ``socket_app`` ASGI app that is mounted at ``/ws`` in main.py.

Authentication:
  Clients must supply a valid JWT in the Socket.IO handshake ``auth`` dict::

      socket = io("https://...", { auth: { token: "<bearer>" } })

  The ``connect`` handler verifies the token using the existing JWKS
  verifier and refuses the connection if the token is invalid or expired.

Rooms:
  ``tenant:<tenant_id>``  — all users within a tenant (ticket_update broadcasts)
  ``user:<user_id>``      — per-user notifications
  ``operators``           — operator dashboard clients (joined via
                            ``join_operator_room`` client event)

Redis adapter:
  ``socketio.AsyncRedisManager`` is configured so that Celery workers can
  emit events without an HTTP round-trip.  Workers import ``sio`` and emit
  directly.

  If REDIS_URL is not available at startup the server falls back to the
  in-process memory adapter (single-process mode only, no worker support).
"""

from __future__ import annotations

import logging
from typing import Any

import socketio

from app.config import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()

# ---------------------------------------------------------------------------
# Redis-backed Socket.IO manager
# ---------------------------------------------------------------------------

try:
    _mgr = socketio.AsyncRedisManager(settings.REDIS_URL)
    logger.info("socketio_redis_manager_initialised", extra={"redis_url": settings.REDIS_URL})
except Exception as _exc:
    logger.warning(
        "socketio_redis_manager_failed_using_memory",
        extra={"error": str(_exc)},
    )
    _mgr = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# AsyncServer
# ---------------------------------------------------------------------------

_server_kwargs: dict[str, Any] = {
    "async_mode": "asgi",
    "cors_allowed_origins": "*",  # CORS is enforced by FastAPI CORSMiddleware
    "logger": False,
    "engineio_logger": False,
}
if _mgr is not None:
    _server_kwargs["client_manager"] = _mgr

sio = socketio.AsyncServer(**_server_kwargs)
socket_app = socketio.ASGIApp(sio)

# ---------------------------------------------------------------------------
# Event: connect
# ---------------------------------------------------------------------------


@sio.event
async def connect(sid: str, environ: dict, data: dict | None = None) -> None:
    """Authenticate and register the client in tenant/user rooms.

    The JWT token must be present in the handshake ``auth`` dict.
    A missing or invalid token causes a ``ConnectionRefusedError`` which
    signals python-socketio to reject the connection with a 401-equivalent.
    """
    from app.auth.jwks import verify_token  # local import to avoid startup-time cycles
    from app.exceptions import AuthenticationError

    token: str | None = None
    if isinstance(data, dict):
        token = data.get("token")

    if not token:
        logger.debug("socketio_connect_rejected_no_token", extra={"sid": sid})
        raise ConnectionRefusedError("Unauthorized")

    try:
        payload = verify_token(token)
    except AuthenticationError as exc:
        logger.debug(
            "socketio_connect_rejected_invalid_token",
            extra={"sid": sid, "reason": str(exc)},
        )
        raise ConnectionRefusedError("Unauthorized") from exc

    tenant_id: str = payload.get("tenant_id") or payload.get("org_id") or ""
    user_id: str = payload.get("sub") or ""
    roles: list[str] = payload.get("roles", [])

    if not tenant_id or not user_id:
        logger.debug("socketio_connect_rejected_missing_claims", extra={"sid": sid})
        raise ConnectionRefusedError("Unauthorized")

    # Persist identity for this session
    await sio.save_session(
        sid,
        {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "roles": roles,
        },
    )

    # Join rooms
    await sio.enter_room(sid, f"tenant:{tenant_id}")
    await sio.enter_room(sid, f"user:{user_id}")

    logger.debug(
        "socketio_client_connected",
        extra={"sid": sid, "tenant_id": tenant_id, "user_id": user_id},
    )


# ---------------------------------------------------------------------------
# Event: disconnect
# ---------------------------------------------------------------------------


@sio.event
async def disconnect(sid: str) -> None:
    """Log client disconnection at DEBUG level."""
    logger.debug("socketio_client_disconnected", extra={"sid": sid})


# ---------------------------------------------------------------------------
# Event: join_operator_room  (emitted by operator dashboard clients)
# ---------------------------------------------------------------------------


@sio.event
async def join_operator_room(sid: str) -> None:
    """Add the client to the ``operators`` broadcast room.

    Only clients with the ``agent`` or ``admin`` role are allowed.
    Silently ignores unauthorised clients.
    """
    session = await sio.get_session(sid)
    roles: list[str] = session.get("roles", []) if session else []

    if not any(r in roles for r in ("agent", "admin", "operator")):
        logger.debug(
            "socketio_join_operator_room_denied",
            extra={"sid": sid, "roles": roles},
        )
        return

    await sio.enter_room(sid, "operators")
    logger.debug("socketio_joined_operator_room", extra={"sid": sid})


# ---------------------------------------------------------------------------
# Helper functions — called from notification_service.py and Celery tasks
# ---------------------------------------------------------------------------


async def emit_notification(tenant_id: str, user_id: str, payload: dict) -> None:
    """Emit a ``notification`` event to the given user's private room.

    This function is designed to be called from within the FastAPI async
    context (e.g. via ``asyncio.create_task``).  For Celery worker contexts
    use the Redis manager emit path in ``tasks_notifications.py``.
    """
    try:
        await sio.emit("notification", payload, room=f"user:{user_id}")
    except Exception as exc:
        # Socket.IO emit MUST NOT propagate — notification DB row already created.
        logger.warning(
            "socketio_emit_notification_failed",
            extra={"tenant_id": tenant_id, "user_id": user_id, "error": str(exc)},
        )


async def emit_ticket_update(tenant_id: str, payload: dict) -> None:
    """Broadcast a ``ticket_update`` event to all clients in the tenant room."""
    try:
        await sio.emit("ticket_update", payload, room=f"tenant:{tenant_id}")
    except Exception as exc:
        logger.warning(
            "socketio_emit_ticket_update_failed",
            extra={"tenant_id": tenant_id, "error": str(exc)},
        )
