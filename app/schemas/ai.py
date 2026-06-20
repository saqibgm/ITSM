"""Pydantic v2 response schemas for AI enrichment resources.

These are the canonical API-layer representations of:
  - AIResponseSuggestion    (§7A.5 Agent Response Suggestions)
  - AITicketClassification  (§7A.2 Auto-Classification / HITL dataset)
  - AIDuplicateCandidate    (§7A.3 Semantic Duplicate Detection)

The in-module schemas inside api/v1/ai_tickets.py remain for backward
compatibility; these canonical schemas should be preferred for new code.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class AIResponseSuggestionResponse(BaseModel):
    """API representation of an ai_response_suggestions row."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    ticket_id: UUID
    body: str
    model_version: str
    used: bool
    created_at: datetime


class AIClassificationResponse(BaseModel):
    """API representation of an ai_ticket_classifications row."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    ticket_id: UUID
    suggested_priority: str | None = None
    suggested_category_id: UUID | None = None
    suggested_team_id: UUID | None = None
    confidence: float
    model_version: str
    accepted: bool | None = None
    accepted_fields: list[str] | None = None
    actual_priority: str | None = None
    actual_category_id: UUID | None = None
    actual_team_id: UUID | None = None
    created_at: datetime


class AIDuplicateCandidateResponse(BaseModel):
    """API representation of an ai_duplicate_candidates row."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    ticket_id: UUID
    candidate_id: UUID
    similarity_score: float
    dismissed: bool
    created_at: datetime


__all__ = [
    "AIResponseSuggestionResponse",
    "AIClassificationResponse",
    "AIDuplicateCandidateResponse",
]
