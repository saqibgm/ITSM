"""Virtual Agent API endpoints (S4B.1 / S4B.2).

Routes:
  POST   /virtual-agent/sessions                                — create session
  POST   /virtual-agent/sessions/{session_id}/messages          — send message (full response)
  POST   /virtual-agent/sessions/{session_id}/messages/stream   — send message (SSE stream)
  GET    /virtual-agent/sessions/{session_id}/history           — list messages
  POST   /virtual-agent/sessions/{session_id}/close             — close session
  GET    /virtual-agent/sessions                                — list user's sessions

All routes require an authenticated tenant user.
tenant_id is always sourced from the JWT (current_user.tenant_id) — never
accepted from the request body.
"""

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, AsyncGenerator
from uuid import UUID

import openai
from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentUser, get_current_user
from app.config import get_settings
from app.database import get_db
from app.exceptions import AuthorizationError
from app.models.virtual_agent import VirtualAgentMessage, VirtualAgentSession
from app.redis_client import get_redis
from app.services.ai.ai_service import AIService
from app.services.ai.embedder import EmbedderService
from app.services.pii_masker import pii_masker
from app.services.virtual_agent_service import VirtualAgentService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/virtual-agent", tags=["virtual-agent"])

# ---------------------------------------------------------------------------
# Module-level singletons
# ---------------------------------------------------------------------------

_ai_service = AIService()
_settings = get_settings()
_openai_client = openai.AsyncOpenAI(api_key=_settings.OPENAI_API_KEY)


def _get_embedder(redis) -> EmbedderService:
    """Construct an EmbedderService bound to the shared redis pool."""
    return EmbedderService(openai_client=_openai_client, redis=redis)


_va_service = VirtualAgentService()


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------


class CreateSessionRequest(BaseModel):
    channel: str = Field(
        default="web_widget",
        description="web_widget | mobile | slack | teams | api",
    )


class SessionResponse(BaseModel):
    id: UUID
    status: str
    channel: str
    intent: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class SendMessageRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)


class SendMessageResponse(BaseModel):
    response: str
    intent: str | None
    sources: list[dict[str, Any]]
    session_status: str


class MessageResponse(BaseModel):
    role: str
    content: str
    intent_detected: str | None
    sources: list[dict[str, Any]] | None
    created_at: datetime

    model_config = {"from_attributes": True}


class SessionListResponse(BaseModel):
    items: list[SessionResponse]
    page: int
    page_size: int
    total: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_tenant(current_user: CurrentUser) -> None:
    """Raise AuthorizationError if the user has no tenant context."""
    if current_user.tenant_id is None or current_user.local_user_id is None:
        raise AuthorizationError("Tenant context is required")


def _tenant_id(current_user: CurrentUser) -> str:
    return str(current_user.tenant_id)


def _user_id(current_user: CurrentUser) -> str:
    return str(current_user.local_user_id)


# ---------------------------------------------------------------------------
# POST /virtual-agent/sessions
# ---------------------------------------------------------------------------


@router.post("/sessions", response_model=SessionResponse, status_code=201)
async def create_session(
    body: CreateSessionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> SessionResponse:
    """Create a new virtual agent session for the authenticated user."""
    _require_tenant(current_user)

    session = await _va_service.create_session(
        db=db,
        tenant_id=_tenant_id(current_user),
        user_id=_user_id(current_user),
        channel=body.channel,
    )
    await db.commit()
    return SessionResponse.model_validate(session)


# ---------------------------------------------------------------------------
# POST /virtual-agent/sessions/{session_id}/messages
# ---------------------------------------------------------------------------


@router.post(
    "/sessions/{session_id}/messages",
    response_model=SendMessageResponse,
)
async def send_message(
    session_id: UUID,
    body: SendMessageRequest,
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
    current_user: CurrentUser = Depends(get_current_user),
) -> SendMessageResponse:
    """Send a user message to the virtual agent and receive a response."""
    _require_tenant(current_user)

    embedder = _get_embedder(redis)

    result = await _va_service.process_message(
        db=db,
        redis=redis,
        session_id=str(session_id),
        tenant_id=_tenant_id(current_user),
        user_message=body.message,
        ai_service=_ai_service,
        embedder=embedder,
    )
    await db.commit()
    return SendMessageResponse(**result)


# ---------------------------------------------------------------------------
# POST /virtual-agent/sessions/{session_id}/messages/stream  (SSE)
# ---------------------------------------------------------------------------

_SSE_KEEPALIVE_SECONDS = 15
_rag_engine_module = None  # lazy import to avoid circular imports


def _get_rag_engine():
    """Lazy-import the RAGEngine singleton to avoid module-level circular deps."""
    from app.services.ai.rag_engine import RAGEngine  # noqa: PLC0415
    global _rag_engine_module
    if _rag_engine_module is None:
        _rag_engine_module = RAGEngine()
    return _rag_engine_module


def _get_intent_detector():
    """Lazy-import IntentDetector singleton."""
    from app.services.ai.intent_detector import IntentDetector  # noqa: PLC0415
    return IntentDetector()


@router.post(
    "/sessions/{session_id}/messages/stream",
    response_class=StreamingResponse,
    summary="Send a message and receive a streaming SSE response",
)
async def send_message_stream(
    session_id: UUID,
    body: SendMessageRequest,
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
    current_user: CurrentUser = Depends(get_current_user),
) -> StreamingResponse:
    """Stream the virtual agent response using Server-Sent Events (SSE).

    Each token delta is sent as ``data: {"token": "<delta>"}\\n\\n``.
    A keep-alive ping (``data: {"ping": true}\\n\\n``) is emitted when no token
    has been yielded for 15 seconds.
    The final event is ``data: {"done": true, "intent": ..., "sources": [...]}\\n\\n``.
    On error: ``data: {"error": "Stream interrupted"}\\n\\n``.
    """
    _require_tenant(current_user)

    tenant_id = _tenant_id(current_user)
    embedder = _get_embedder(redis)

    async def _event_generator() -> AsyncGenerator[str, None]:
        # ----------------------------------------------------------------
        # 1. Load and validate session
        # ----------------------------------------------------------------
        from app.exceptions import ResourceNotFoundError, ValidationError  # noqa: PLC0415

        try:
            result = await db.execute(
                select(VirtualAgentSession).where(
                    VirtualAgentSession.id == session_id,
                    VirtualAgentSession.tenant_id == current_user.tenant_id,
                )
            )
            va_session = result.scalar_one_or_none()
            if va_session is None:
                yield f"data: {json.dumps({'error': 'Session not found'})}\n\n"
                return

            if va_session.status != "active":
                yield f"data: {json.dumps({'error': 'Session is closed or handed off'})}\n\n"
                return

            # ----------------------------------------------------------------
            # 2. Save user message (PII-masked)
            # ----------------------------------------------------------------
            masked_message = pii_masker.mask(body.message)
            user_msg = VirtualAgentMessage(
                session_id=va_session.id,
                role="user",
                content=masked_message,
            )
            db.add(user_msg)
            await db.flush()

            # ----------------------------------------------------------------
            # 3. Detect intent (non-blocking, best-effort)
            # ----------------------------------------------------------------
            intent_detector = _get_intent_detector()
            intent_task = asyncio.ensure_future(
                intent_detector.detect(
                    message=masked_message,
                    ai_service=_ai_service,
                    tenant_id=tenant_id,
                    redis=redis,
                )
            )

            # ----------------------------------------------------------------
            # 4. Retrieve RAG context
            # ----------------------------------------------------------------
            context_articles: list[dict] = []
            try:
                context_articles = await _get_rag_engine().retrieve_context(
                    query=masked_message,
                    tenant_id=tenant_id,
                    db=db,
                    embedder=embedder,
                )
            except Exception:
                pass  # RAG failures are non-fatal — stream will use empty context

            # ----------------------------------------------------------------
            # 5. Load session history (last 10 messages)
            # ----------------------------------------------------------------
            history_result = await db.execute(
                select(VirtualAgentMessage)
                .where(VirtualAgentMessage.session_id == va_session.id)
                .order_by(VirtualAgentMessage.created_at.asc())
                .limit(10)
            )
            session_history = list(history_result.scalars().all())

            # ----------------------------------------------------------------
            # 6. Stream response tokens with keep-alive
            # ----------------------------------------------------------------
            rag_engine = _get_rag_engine()
            accumulated: list[str] = []
            last_yield_at = asyncio.get_event_loop().time()

            stream_gen = rag_engine.generate_response_stream(
                user_message=masked_message,
                context_articles=context_articles,
                session_history=session_history,
                ai_service=_ai_service,
                tenant_id=tenant_id,
                redis=redis,
            )

            async for delta in stream_gen:
                now = asyncio.get_event_loop().time()
                # Emit keep-alive if idle for too long
                if now - last_yield_at >= _SSE_KEEPALIVE_SECONDS:
                    yield f"data: {json.dumps({'ping': True})}\n\n"
                accumulated.append(delta)
                yield f"data: {json.dumps({'token': delta})}\n\n"
                last_yield_at = asyncio.get_event_loop().time()

            full_response = "".join(accumulated)

            # ----------------------------------------------------------------
            # 7. Save assistant message and update session
            # ----------------------------------------------------------------
            # Collect intent result (with a short timeout so we don't hang)
            intent: str | None = None
            try:
                intent = await asyncio.wait_for(intent_task, timeout=2.0)
            except (asyncio.TimeoutError, Exception):
                intent_task.cancel()

            assistant_msg = VirtualAgentMessage(
                session_id=va_session.id,
                role="assistant",
                content=full_response,
                intent_detected=intent,
                sources=context_articles if context_articles else None,
            )
            db.add(assistant_msg)

            if intent:
                va_session.intent = intent

            if intent == "handoff_to_agent":
                va_session.status = "handed_off"
                va_session.ended_at = func.now()

            await db.flush()
            await db.commit()

            # ----------------------------------------------------------------
            # 8. Final done event
            # ----------------------------------------------------------------
            yield f"data: {json.dumps({'done': True, 'intent': intent, 'sources': context_articles})}\n\n"

        except Exception as exc:
            logger.error(
                "sse_stream_error",
                extra={
                    "session_id": str(session_id),
                    "tenant_id": tenant_id,
                    "exception_type": type(exc).__name__,
                },
            )
            try:
                yield f"data: {json.dumps({'error': 'Stream interrupted'})}\n\n"
            except Exception:
                pass

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# GET /virtual-agent/sessions/{session_id}/history
# ---------------------------------------------------------------------------


@router.get(
    "/sessions/{session_id}/history",
    response_model=list[MessageResponse],
)
async def get_session_history(
    session_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> list[MessageResponse]:
    """Return all messages in a session, ordered chronologically."""
    _require_tenant(current_user)

    messages = await _va_service.get_session_history(
        db=db,
        session_id=str(session_id),
        tenant_id=_tenant_id(current_user),
    )
    return [MessageResponse.model_validate(m) for m in messages]


# ---------------------------------------------------------------------------
# POST /virtual-agent/sessions/{session_id}/close
# ---------------------------------------------------------------------------


@router.post(
    "/sessions/{session_id}/close",
    response_model=SessionResponse,
)
async def close_session(
    session_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> SessionResponse:
    """Close an active virtual agent session."""
    _require_tenant(current_user)

    session = await _va_service.close_session(
        db=db,
        session_id=str(session_id),
        tenant_id=_tenant_id(current_user),
    )
    await db.commit()
    return SessionResponse.model_validate(session)


# ---------------------------------------------------------------------------
# GET /virtual-agent/sessions
# ---------------------------------------------------------------------------


@router.get("/sessions", response_model=SessionListResponse)
async def list_sessions(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> SessionListResponse:
    """Return a paginated list of the user's virtual agent sessions."""
    _require_tenant(current_user)

    tenant_uuid = current_user.tenant_id
    user_uuid = current_user.local_user_id

    # Count total (scalar subquery — no full table scan of rows)
    count_result = await db.execute(
        select(func.count(VirtualAgentSession.id)).where(
            VirtualAgentSession.tenant_id == tenant_uuid,
            VirtualAgentSession.user_id == user_uuid,
        )
    )
    total = count_result.scalar_one()

    # Paginate
    offset = (page - 1) * page_size
    page_result = await db.execute(
        select(VirtualAgentSession)
        .where(
            VirtualAgentSession.tenant_id == tenant_uuid,
            VirtualAgentSession.user_id == user_uuid,
        )
        .order_by(VirtualAgentSession.created_at.desc())
        .offset(offset)
        .limit(page_size)
    )
    sessions = list(page_result.scalars().all())

    return SessionListResponse(
        items=[SessionResponse.model_validate(s) for s in sessions],
        page=page,
        page_size=page_size,
        total=total,
    )
