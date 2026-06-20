"""
ReplySuggester — generates a professional draft reply for a ticket using Claude.

Security rules (§7A.11):
  - The system prompt contains ONLY static instructions.  No tenant data, no
    user content, and no ticket text is interpolated into it.
  - All variable content (ticket title, description, comments, KB excerpts) is
    placed exclusively in the ``user`` message.
"""

import logging
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.ai_ticket import AIResponseSuggestion
from app.models.ticket import Ticket, TicketComment
from app.services.ai.ai_service import AIService

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Static system prompt — no user/ticket data here
# ---------------------------------------------------------------------------
_SYSTEM_PROMPT = (
    "You are a professional IT support agent. "
    "Write a helpful, empathetic reply to the customer based on the ticket "
    "context provided. "
    "Be concise and actionable. "
    "Do not invent solutions not supported by the context. "
    "Write in plain text — no markdown headers or bullet points unless "
    "explicitly useful for readability."
)

_MODEL_VERSION_PREFIX = "claude-reply-"

# How many recent public comments to include in the prompt
_MAX_COMMENTS = 10


class ReplySuggester:
    """Generate a Claude-powered draft reply for a ticket and persist it."""

    def __init__(self, ai_service: AIService | None = None) -> None:
        self._ai = ai_service or AIService()

    async def suggest_reply(
        self,
        db: AsyncSession,
        redis,
        ticket: Ticket,
        tenant_id: UUID,
    ) -> AIResponseSuggestion:
        """Build context from the ticket and ask Claude for a draft reply.

        Context included in the user message (never in system):
          - Ticket title, type, priority, status
          - Ticket description
          - Last 10 public (non-internal) comments in chronological order
          - Titles + first-500-char excerpts of any linked KB articles

        The resulting suggestion is persisted as an AIResponseSuggestion row
        with ``used=False``; the API layer sets ``used=True`` when the agent
        views it within 60 s.

        Args:
            db:         Async SQLAlchemy session.
            redis:      Redis client (for budget gating + usage tracking).
            ticket:     The ticket to generate a reply for.
            tenant_id:  Tenant UUID for budget gating.

        Returns:
            Persisted AIResponseSuggestion record.

        Raises:
            AIBudgetExhaustedError: if monthly token budget is exhausted.
            ExternalServiceError("anthropic"): after all retries fail.
        """
        s = get_settings()

        # ------------------------------------------------------------------
        # 1. Fetch last 10 public (non-internal) comments, chronological
        # ------------------------------------------------------------------
        comments_result = await db.execute(
            select(TicketComment)
            .where(
                TicketComment.ticket_id == ticket.id,
                TicketComment.is_internal.is_(False),
                TicketComment.deleted_at.is_(None),
            )
            .order_by(TicketComment.created_at.asc())
            .limit(_MAX_COMMENTS)
        )
        comments: list[TicketComment] = list(comments_result.scalars().all())

        # ------------------------------------------------------------------
        # 2. Fetch linked KB articles (titles + excerpts)
        #    ticket_kb_links join kb_articles — we do a raw query so this
        #    module stays decoupled from the KB model until it's formally
        #    imported here.
        # ------------------------------------------------------------------
        kb_context: str = ""
        try:
            from sqlalchemy import text as sql_text

            kb_result = await db.execute(
                sql_text(
                    """
                    SELECT a.title, LEFT(a.body, 500)
                    FROM ticket_kb_links tkl
                    JOIN kb_articles a ON a.id = tkl.article_id
                    WHERE tkl.ticket_id = :ticket_id
                    LIMIT 5
                    """
                ),
                {"ticket_id": ticket.id},
            )
            kb_rows = kb_result.all()
            if kb_rows:
                parts = []
                for i, (title, excerpt) in enumerate(kb_rows, 1):
                    parts.append(f"KB Article {i}: {title}\nExcerpt: {excerpt}")
                kb_context = "\n\n".join(parts)
        except Exception as exc:  # table may not exist yet in test env
            logger.debug("kb_articles_fetch_skipped", extra={"reason": str(exc)})

        # ------------------------------------------------------------------
        # 3. Build user message — ALL variable content goes here
        # ------------------------------------------------------------------
        priority_val = (
            ticket.priority.value
            if hasattr(ticket.priority, "value")
            else str(ticket.priority)
        )
        status_val = (
            ticket.status.value
            if hasattr(ticket.status, "value")
            else str(ticket.status)
        )
        type_val = (
            ticket.type.value
            if hasattr(ticket.type, "value")
            else str(ticket.type)
        )

        lines: list[str] = [
            f"Ticket: {ticket.ticket_number}",
            f"Type: {type_val}",
            f"Priority: {priority_val}",
            f"Status: {status_val}",
            f"Title: {ticket.title}",
            "",
            "Description:",
            ticket.description,
        ]

        if comments:
            lines.append("")
            lines.append("Comment history (oldest to newest):")
            for comment in comments:
                lines.append(f"- {comment.body}")

        if kb_context:
            lines.append("")
            lines.append("Linked knowledge base articles:")
            lines.append(kb_context)

        user_content = "\n".join(lines)
        messages = [{"role": "user", "content": user_content}]

        # ------------------------------------------------------------------
        # 4. Call Claude (budget-gated, retried)
        # ------------------------------------------------------------------
        reply_body = await self._ai.generate(
            tenant_id=str(tenant_id),
            redis=redis,
            messages=messages,
            system=_SYSTEM_PROMPT,
            feature="reply_suggestion",
        )

        # ------------------------------------------------------------------
        # 5. Persist and return
        # ------------------------------------------------------------------
        model_version = _MODEL_VERSION_PREFIX + s.CLAUDE_MODEL

        suggestion = AIResponseSuggestion(
            ticket_id=ticket.id,
            body=reply_body.strip(),
            model_version=model_version,
            used=False,
        )
        db.add(suggestion)
        await db.flush()
        await db.refresh(suggestion)

        logger.info(
            "reply_suggestion_created",
            extra={
                "ticket_id": str(ticket.id),
                "tenant_id": str(tenant_id),
                "suggestion_id": str(suggestion.id),
                "comment_count": len(comments),
                "has_kb_context": bool(kb_context),
            },
        )

        return suggestion
