"""
VirtualAgentService — orchestrates session lifecycle and message processing
for the Virtual Agent RAG engine (S4B.1).

Flow for process_message:
  1. Load & validate session (tenant-scoped, must be active)
  2. Persist user message
  3. Detect intent (gracefully degraded — never blocks)
  4. Retrieve RAG context (gracefully degraded — never blocks)
  5. Load conversation history (last 10 messages)
  6. Generate AI response
  7. Persist assistant message
  8. Update session intent / status
  9. Return structured result dict
"""

from __future__ import annotations

import logging
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import ResourceNotFoundError, ValidationError
from app.models.virtual_agent import VirtualAgentMessage, VirtualAgentSession
from app.services.ai.ai_service import AIService
from app.services.ai.embedder import EmbedderService
from app.services.ai.intent_detector import IntentDetector
from app.services.ai.rag_engine import RAGEngine
from app.services.pii_masker import pii_masker

logger = logging.getLogger(__name__)

_intent_detector = IntentDetector()
_rag_engine = RAGEngine()


class VirtualAgentService:
    """Stateless service; instantiate once and reuse across requests."""

    # ------------------------------------------------------------------
    # create_session
    # ------------------------------------------------------------------

    async def create_session(
        self,
        db: AsyncSession,
        tenant_id: str,
        user_id: str | None,
        channel: str,
    ) -> VirtualAgentSession:
        """Create and persist a new virtual agent session.

        Args:
            db:        Async DB session.
            tenant_id: Tenant UUID string (from JWT — never from request body).
            user_id:   Local user UUID string, or None for anonymous sessions.
            channel:   One of web_widget / mobile / slack / teams / api.

        Returns:
            The persisted VirtualAgentSession instance.
        """
        session = VirtualAgentSession(
            tenant_id=UUID(tenant_id),
            user_id=UUID(user_id) if user_id else None,
            channel=channel,
            status="active",
        )
        db.add(session)
        await db.flush()
        await db.refresh(session)
        logger.info(
            "virtual_agent_session_created",
            extra={
                "session_id": str(session.id),
                "tenant_id": tenant_id,
                "channel": channel,
            },
        )
        return session

    # ------------------------------------------------------------------
    # process_message
    # ------------------------------------------------------------------

    async def process_message(
        self,
        db: AsyncSession,
        redis,
        session_id: str,
        tenant_id: str,
        user_message: str,
        ai_service: AIService,
        embedder: EmbedderService,
    ) -> dict:
        """Process one user message turn and return the assistant response.

        Args:
            db:           Async DB session.
            redis:        Redis client (for AI budget checks).
            session_id:   UUID string of the target session.
            tenant_id:    Tenant UUID string from JWT.
            user_message: Raw user message text.
            ai_service:   AIService instance.
            embedder:     EmbedderService instance.

        Returns:
            {
                "response":       str,
                "intent":         str | None,
                "sources":        list[dict],
                "session_status": str,
            }

        Raises:
            ResourceNotFoundError: if session not found for this tenant.
            ValidationError:       if session is not active.
        """
        # Step 1 — Load session (tenant-scoped)
        result = await db.execute(
            select(VirtualAgentSession).where(
                VirtualAgentSession.id == UUID(session_id),
                VirtualAgentSession.tenant_id == UUID(tenant_id),
            )
        )
        va_session = result.scalar_one_or_none()
        if va_session is None:
            raise ResourceNotFoundError("virtual_agent_session", session_id)

        # Step 2 — Validate session status
        if va_session.status != "active":
            raise ValidationError("Session is closed or handed off")

        # Step 3 — Persist user message (with PII masking on stored content)
        masked_user_message = pii_masker.mask(user_message)
        user_msg = VirtualAgentMessage(
            session_id=va_session.id,
            role="user",
            content=masked_user_message,
        )
        db.add(user_msg)
        await db.flush()

        # Step 4 — Detect intent (graceful fallback on any error)
        intent: str | None = await _intent_detector.detect(
            message=masked_user_message,
            ai_service=ai_service,
            tenant_id=tenant_id,
            redis=redis,
        )

        # Step 5 — Retrieve RAG context (graceful fallback on any error)
        context_articles: list[dict] = await _rag_engine.retrieve_context(
            query=masked_user_message,
            tenant_id=tenant_id,
            db=db,
            embedder=embedder,
        )

        # Step 6 — Load last 10 session messages for history
        history_result = await db.execute(
            select(VirtualAgentMessage)
            .where(VirtualAgentMessage.session_id == va_session.id)
            .order_by(VirtualAgentMessage.created_at.asc())
            .limit(10)
        )
        session_history = list(history_result.scalars().all())

        # Step 7 — Generate assistant response
        response_text = await _rag_engine.generate_response(
            user_message=masked_user_message,
            context_articles=context_articles,
            session_history=session_history,
            ai_service=ai_service,
            tenant_id=tenant_id,
            redis=redis,
        )

        # Step 8 — Persist assistant message
        assistant_msg = VirtualAgentMessage(
            session_id=va_session.id,
            role="assistant",
            content=response_text,
            intent_detected=intent,
            sources=context_articles if context_articles else None,
        )
        db.add(assistant_msg)

        # Step 9 — Update session intent and status
        if intent:
            va_session.intent = intent

        if intent == "handoff_to_agent":
            va_session.status = "handed_off"
            va_session.ended_at = func.now()

        await db.flush()
        await db.refresh(va_session)

        logger.info(
            "virtual_agent_message_processed",
            extra={
                "session_id": session_id,
                "tenant_id": tenant_id,
                "intent": intent,
                "sources_count": len(context_articles),
                "session_status": va_session.status,
            },
        )

        return {
            "response": response_text,
            "intent": intent,
            "sources": context_articles,
            "session_status": va_session.status,
        }

    # ------------------------------------------------------------------
    # close_session
    # ------------------------------------------------------------------

    async def close_session(
        self,
        db: AsyncSession,
        session_id: str,
        tenant_id: str,
    ) -> VirtualAgentSession:
        """Mark a session as closed.

        Raises:
            ResourceNotFoundError: if session not found for this tenant.
        """
        result = await db.execute(
            select(VirtualAgentSession).where(
                VirtualAgentSession.id == UUID(session_id),
                VirtualAgentSession.tenant_id == UUID(tenant_id),
            )
        )
        va_session = result.scalar_one_or_none()
        if va_session is None:
            raise ResourceNotFoundError("virtual_agent_session", session_id)

        va_session.status = "closed"
        va_session.ended_at = func.now()
        await db.flush()
        await db.refresh(va_session)
        return va_session

    # ------------------------------------------------------------------
    # get_session_history
    # ------------------------------------------------------------------

    async def get_session_history(
        self,
        db: AsyncSession,
        session_id: str,
        tenant_id: str,
    ) -> list[VirtualAgentMessage]:
        """Return all messages for a session, ordered chronologically.

        Raises:
            ResourceNotFoundError: if session not found for this tenant.
        """
        # Validate session ownership first
        session_result = await db.execute(
            select(VirtualAgentSession).where(
                VirtualAgentSession.id == UUID(session_id),
                VirtualAgentSession.tenant_id == UUID(tenant_id),
            )
        )
        if session_result.scalar_one_or_none() is None:
            raise ResourceNotFoundError("virtual_agent_session", session_id)

        msg_result = await db.execute(
            select(VirtualAgentMessage)
            .where(VirtualAgentMessage.session_id == UUID(session_id))
            .order_by(VirtualAgentMessage.created_at.asc())
        )
        return list(msg_result.scalars().all())
