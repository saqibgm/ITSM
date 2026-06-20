"""
KBDrafter — generates KB article drafts from resolved tickets using Claude.

Security contract:
  - The system prompt is 100% static — no user content is ever interpolated
    into it.
  - Ticket fields (title, description, resolution_note, category) are always
    placed in the ``user`` turn of the messages list.
  - AIBudgetExhaustedError propagates to the caller; all other parse/format
    errors are caught and logged, returning None.
"""

import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.services.ai.ai_service import AIService

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Static system prompt — NEVER modified with user content
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "You are an ITSM knowledge base author. "
    "Given a resolved support ticket, produce a concise KB article that "
    "documents the problem and its resolution so future readers can self-serve. "
    "Respond with ONLY a JSON object containing exactly three keys: "
    "\"title\" (string, max 200 chars), "
    "\"content\" (string, the full article body in plain text, at least 3 sentences), "
    "and \"suggested_tags\" (array of strings, 1–5 lowercase tags). "
    "Do not include any text outside the JSON object."
)


class KBDrafter:
    """Generates KB article drafts from resolved tickets via Claude."""

    async def draft_from_ticket(
        self,
        ticket: Any,
        resolution_note: str,
        ai_service: "AIService",
        redis: Any = None,
    ) -> dict | None:
        """Generate a KB draft dict from a resolved ticket.

        Args:
            ticket:          ORM Ticket object (must have id, title, description,
                             category relationship with .name, tenant_id).
            resolution_note: Resolution text (may be empty string).
            ai_service:      AIService instance for generate() + check_budget().
            redis:           Redis client (required for generate() budget gating).

        Returns:
            Dict with keys ``title``, ``content``, ``suggested_tags`` on success.
            None if the AI response cannot be parsed as valid JSON with the
            expected keys.

        Raises:
            AIBudgetExhaustedError: propagated from ai_service.generate() — let
                the caller (Celery task) handle it.
        """
        category_name: str = ""
        try:
            if ticket.category is not None:
                category_name = ticket.category.name
        except Exception:
            pass  # category may not be loaded; skip gracefully

        # Build a structured user turn — all user content stays here, never
        # in the system prompt.
        user_content = (
            f"Ticket ID: {ticket.id}\n"
            f"Title: {ticket.title}\n"
            f"Description: {ticket.description}\n"
            f"Category: {category_name or 'N/A'}\n"
            f"Resolution note: {resolution_note or 'No resolution note provided.'}\n"
            "\nPlease produce the KB article JSON."
        )

        raw = await ai_service.generate(
            tenant_id=str(ticket.tenant_id),
            redis=redis,
            messages=[{"role": "user", "content": user_content}],
            system=_SYSTEM_PROMPT,
            feature="kb_draft",
        )

        # Parse and validate the JSON response
        try:
            # Strip markdown fences if Claude wraps the JSON
            stripped = raw.strip()
            if stripped.startswith("```"):
                lines = stripped.splitlines()
                # Drop the opening ``` line and the closing ``` line
                inner_lines = [
                    ln for ln in lines[1:]
                    if not ln.strip().startswith("```")
                ]
                stripped = "\n".join(inner_lines).strip()

            draft = json.loads(stripped)

            if not isinstance(draft, dict):
                raise ValueError("Response is not a JSON object")

            title = draft.get("title")
            content = draft.get("content")
            tags = draft.get("suggested_tags")

            if not isinstance(title, str) or not title.strip():
                raise ValueError("Missing or empty 'title'")
            if not isinstance(content, str) or not content.strip():
                raise ValueError("Missing or empty 'content'")
            if not isinstance(tags, list):
                tags = []

            return {
                "title": title[:200].strip(),
                "content": content.strip(),
                "suggested_tags": [str(t).lower() for t in tags[:5]],
            }

        except Exception as exc:
            logger.warning(
                "kb_drafter_parse_failure",
                extra={
                    "ticket_id": str(ticket.id),
                    "tenant_id": str(ticket.tenant_id),
                    "exception_type": type(exc).__name__,
                    "exception": str(exc),
                },
            )
            return None
