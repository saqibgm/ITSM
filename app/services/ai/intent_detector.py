"""
Intent detector for the Virtual Agent (S4B.1).

Classifies a user message into one of KNOWN_INTENTS using Claude.
Returns None on budget exhaustion or any failure — never blocks the flow.

Security rules:
  - System prompt is 100% static.
  - User content is NEVER interpolated into the system prompt.
"""

import logging

from app.exceptions import AIBudgetExhaustedError
from app.services.ai.ai_service import AIService

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are an intent classifier for an IT support virtual agent.

Your task is to identify the user's intent from their message and respond with
EXACTLY ONE of the following intent strings — nothing else:

  create_ticket        — user wants to open a new support ticket
  check_ticket_status  — user is asking about the status of an existing ticket
  search_kb            — user wants to search the knowledge base or find documentation
  reset_password       — user needs a password reset or unlock
  asset_request        — user is requesting hardware, software, or other IT assets
  report_incident      — user is reporting an outage, security incident, or urgent issue
  request_change       — user wants a change to their environment or access
  general_inquiry      — general question that does not fit other categories
  handoff_to_agent     — user explicitly wants to speak with or be transferred to a human agent
  unknown              — the message does not match any of the above intents

Rules:
- Reply with ONLY the intent string, no punctuation, no explanation.
- If uncertain, prefer the closest matching intent over "unknown".
- Use "unknown" only when no intent applies at all.
"""


class IntentDetector:
    KNOWN_INTENTS = [
        "create_ticket",
        "check_ticket_status",
        "search_kb",
        "reset_password",
        "asset_request",
        "report_incident",
        "request_change",
        "general_inquiry",
        "handoff_to_agent",
    ]

    async def detect(self, message: str, ai_service: AIService, tenant_id: str, redis) -> str | None:
        """Classify *message* into one of KNOWN_INTENTS.

        Returns:
            One of KNOWN_INTENTS if classification succeeds, or None for
            "unknown" responses, parse failures, budget exhaustion, and any
            other exception.  Never raises.

        Args:
            message:    Raw user message text (user turn only — never in system).
            ai_service: AIService instance for Claude calls.
            tenant_id:  Tenant UUID string for budget tracking.
            redis:      Redis client for budget checks.
        """
        try:
            raw = await ai_service.generate(
                tenant_id=tenant_id,
                redis=redis,
                messages=[{"role": "user", "content": message}],
                system=_SYSTEM_PROMPT,
                feature="intent_detection",
            )
        except AIBudgetExhaustedError:
            logger.warning(
                "intent_detection_budget_exhausted",
                extra={"tenant_id": str(tenant_id)},
            )
            return None
        except Exception as exc:
            logger.warning(
                "intent_detection_failed",
                extra={
                    "tenant_id": str(tenant_id),
                    "exception_type": type(exc).__name__,
                },
            )
            return None

        detected = raw.strip().lower()
        if detected in self.KNOWN_INTENTS:
            return detected

        # "unknown" or unrecognised response → return None
        return None
