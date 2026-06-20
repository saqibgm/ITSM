"""
TicketClassifier — uses Claude to suggest category, priority, and team for a ticket.

Security rules (§7A.11):
  - User content (ticket title + description) is ALWAYS in a separate ``user``
    message — it is NEVER interpolated into the system prompt.
  - The system prompt contains only static instructions with no tenant-supplied data.
"""

import json
import logging
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.exceptions import ExternalServiceError
from app.models.ai_ticket import AITicketClassification
from app.models.ticket import Ticket
from app.services.ai.ai_service import AIService

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Static system prompt — no user data here
_SYSTEM_PROMPT = (
    "You are an ITSM ticket classifier. "
    "Given a support ticket title and description, classify it. "
    "Return JSON only — no prose, no markdown fences. "
    "The JSON must have exactly these keys: "
    '{"category_name": string or null, '
    '"priority": "critical"|"high"|"medium"|"low", '
    '"team_name": string or null, '
    '"confidence": float between 0 and 1, '
    '"reasoning": string}. '
    "Match category_name and team_name to the provided lists (case-insensitive). "
    "If no match, use null. "
    "Priority must be one of: critical, high, medium, low."
)

_MODEL_VERSION_PREFIX = "claude-classifier-"

# Valid priority values
_VALID_PRIORITIES = {"critical", "high", "medium", "low"}


class TicketClassifier:
    """Classify a ticket using Claude with structured JSON output."""

    def __init__(self, ai_service: AIService | None = None) -> None:
        self._ai = ai_service or AIService()

    async def classify(
        self,
        db: AsyncSession,
        redis,
        ticket: Ticket,
        tenant_id: UUID,
        available_categories: list[dict],  # [{id: UUID-str, name: str}, ...]
        available_teams: list[dict],       # [{id: UUID-str, name: str}, ...]
    ) -> AITicketClassification:
        """Send ticket title + description to Claude and persist the result.

        User content (title, description) is placed in the ``user`` turn.
        Category and team lists are also in the ``user`` turn so they are
        never mixed into the static system prompt.

        Args:
            db:                   Async SQLAlchemy session.
            redis:                Redis client (for budget tracking).
            ticket:               The ticket to classify.
            tenant_id:            Tenant UUID (for budget gating).
            available_categories: List of {id, name} dicts for this tenant.
            available_teams:      List of {id, name} dicts for this tenant.

        Returns:
            Persisted AITicketClassification record.

        Raises:
            ExternalServiceError: propagated from ai_service if Claude is
                unavailable after retries.
        """
        s = get_settings()

        # Build the user message — user content is ONLY here, never in system
        categories_list = ", ".join(
            f'"{c["name"]}"' for c in available_categories
        ) or "none"
        teams_list = ", ".join(
            f'"{t["name"]}"' for t in available_teams
        ) or "none"

        user_content = (
            f"Available categories: [{categories_list}]\n"
            f"Available teams: [{teams_list}]\n\n"
            f"Ticket title: {ticket.title}\n\n"
            f"Ticket description:\n{ticket.description}"
        )

        messages = [{"role": "user", "content": user_content}]

        raw_response = await self._ai.generate(
            tenant_id=str(tenant_id),
            redis=redis,
            messages=messages,
            system=_SYSTEM_PROMPT,
            feature="ticket_classification",
        )

        # Parse JSON response
        parsed = self._parse_response(raw_response)

        # Resolve category name → UUID
        suggested_category_id: UUID | None = None
        if parsed.get("category_name"):
            name_lower = parsed["category_name"].lower()
            for cat in available_categories:
                if cat["name"].lower() == name_lower:
                    try:
                        suggested_category_id = UUID(str(cat["id"]))
                    except (ValueError, KeyError):
                        pass
                    break

        # Resolve team name → UUID
        suggested_team_id: UUID | None = None
        if parsed.get("team_name"):
            name_lower = parsed["team_name"].lower()
            for team in available_teams:
                if team["name"].lower() == name_lower:
                    try:
                        suggested_team_id = UUID(str(team["id"]))
                    except (ValueError, KeyError):
                        pass
                    break

        # Validate priority
        priority_raw: str | None = parsed.get("priority")
        suggested_priority: str | None = None
        if priority_raw and priority_raw.lower() in _VALID_PRIORITIES:
            suggested_priority = priority_raw.lower()

        # Clamp confidence to [0, 1]
        confidence = float(parsed.get("confidence", 0.5))
        confidence = max(0.0, min(1.0, confidence))

        model_version = _MODEL_VERSION_PREFIX + s.CLAUDE_MODEL

        classification = AITicketClassification(
            ticket_id=ticket.id,
            suggested_category_id=suggested_category_id,
            suggested_priority=suggested_priority,
            suggested_assignee_id=None,  # assignee suggestion reserved for future
            suggested_team_id=suggested_team_id,
            confidence=confidence,
            model_version=model_version,
            accepted=None,  # pending agent decision
        )
        db.add(classification)
        await db.flush()
        await db.refresh(classification)

        logger.info(
            "ticket_classification_complete",
            extra={
                "ticket_id": str(ticket.id),
                "tenant_id": str(tenant_id),
                "confidence": confidence,
                "suggested_priority": suggested_priority,
                "has_category": suggested_category_id is not None,
                "has_team": suggested_team_id is not None,
            },
        )

        return classification

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _parse_response(self, raw: str) -> dict:
        """Attempt to parse Claude's JSON response.

        Falls back to a safe default dict on any parse error so that
        classification failures do not prevent ticket creation.
        """
        try:
            # Strip potential markdown code fences
            cleaned = raw.strip()
            if cleaned.startswith("```"):
                lines = cleaned.split("\n")
                # Remove first and last fence lines
                lines = [l for l in lines if not l.strip().startswith("```")]
                cleaned = "\n".join(lines).strip()
            return json.loads(cleaned)
        except (json.JSONDecodeError, ValueError):
            logger.warning(
                "ticket_classification_json_parse_failed",
                extra={"raw_response_excerpt": raw[:200]},
            )
            return {
                "category_name": None,
                "priority": "medium",
                "team_name": None,
                "confidence": 0.0,
                "reasoning": "parse_error",
            }
