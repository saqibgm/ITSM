"""
AI ticket enrichment endpoints.

Ticket-scoped:
  GET  /tickets/{ticket_id}/ai-classification
  POST /tickets/{ticket_id}/ai-classification/accept
  POST /tickets/{ticket_id}/ai-classification/reject
  GET  /tickets/{ticket_id}/ai-duplicates
  POST /tickets/{ticket_id}/ai-duplicates/{candidate_id}/dismiss

Admin export (mounted at /ai prefix via router.py):
  GET  /ai/classification-dataset

Route ordering: static sub-paths (/ai-classification/accept, /ai-classification/reject)
are registered BEFORE the bare parametric GET so FastAPI does not swallow
"accept"/"reject" as ticket_id values.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentUser, get_current_user, require_role
from app.database import get_db
from app.exceptions import AuthorizationError, ResourceNotFoundError
from app.models.ai_ticket import AIDuplicateCandidate, AIResponseSuggestion, AITicketClassification
from app.models.ticket import Ticket, TicketHistory
from app.redis_client import get_redis
from app.repositories.ticket_repo import TicketRepository
from app.services.ai.reply_suggester import ReplySuggester

router = APIRouter(prefix="/tickets", tags=["ai"])

# Separate router for non-ticket-scoped AI endpoints (e.g. /ai/classification-dataset)
# mounted at /api/v1 level (no prefix) via router.py
ai_router = APIRouter(prefix="/ai", tags=["ai-admin"])

# ------------------------------------------------------------------
# Response schemas
# ------------------------------------------------------------------


class AIClassificationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    ticket_id: UUID
    suggested_category_id: UUID | None = None
    suggested_priority: str | None = None
    suggested_assignee_id: UUID | None = None
    suggested_team_id: UUID | None = None
    confidence: float
    model_version: str
    accepted: bool | None = None
    accepted_fields: list[str] | None = None
    accepted_by: UUID | None = None
    actual_category_id: UUID | None = None
    actual_priority: str | None = None
    actual_team_id: UUID | None = None


class AIDuplicateCandidateResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    ticket_id: UUID
    candidate_id: UUID
    similarity_score: float
    model_version: str
    dismissed: bool
    dismissed_by: UUID | None = None


class AIResponseSuggestionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    ticket_id: UUID
    body: str
    model_version: str
    used: bool
    created_at: datetime


# ------------------------------------------------------------------
# Request bodies
# ------------------------------------------------------------------


class AcceptClassificationRequest(BaseModel):
    fields: list[str]  # e.g. ["priority", "category"]


# ------------------------------------------------------------------
# Helper: resolve ticket scoped to tenant
# ------------------------------------------------------------------


async def _get_ticket(
    ticket_id: UUID, current_user: CurrentUser, db: AsyncSession
) -> Ticket:
    if current_user.tenant_id is None:
        raise AuthorizationError("Tenant context required")
    repo = TicketRepository(db)
    return await repo.get_or_404(ticket_id, current_user.tenant_id)


# ===========================================================================
# CLASSIFICATION ENDPOINTS
# ===========================================================================

# ---------------------------------------------------------------------------
# POST /tickets/{ticket_id}/ai-classification/accept
# ---------------------------------------------------------------------------


@router.post(
    "/{ticket_id}/ai-classification/accept",
    response_model=AIClassificationResponse,
)
async def accept_classification(
    ticket_id: UUID,
    body: AcceptClassificationRequest,
    current_user: CurrentUser = Depends(
        require_role("agent", "team_lead", "manager", "admin")
    ),
    db: AsyncSession = Depends(get_db),
) -> AIClassificationResponse:
    """Accept one or more AI-suggested classification fields.

    Applies the accepted fields to the ticket, records HITL ground truth
    (actual_* columns), and writes TicketHistory for each applied field.
    """
    if current_user.local_user_id is None:
        raise AuthorizationError("Tenant context required")

    ticket = await _get_ticket(ticket_id, current_user, db)

    # Load classification
    result = await db.execute(
        select(AITicketClassification).where(
            AITicketClassification.ticket_id == ticket_id
        )
    )
    classification = result.scalar_one_or_none()
    if classification is None:
        raise ResourceNotFoundError("ai_classification", str(ticket_id))

    repo = TicketRepository(db)

    # Apply accepted fields to the ticket
    for field in body.fields:
        if field == "priority" and classification.suggested_priority is not None:
            old_val = ticket.priority
            ticket.priority = classification.suggested_priority  # type: ignore[assignment]
            await repo.record_history(
                ticket_id=ticket_id,
                actor_id=current_user.local_user_id,
                field="priority",
                old_val={"priority": old_val.value if hasattr(old_val, "value") else str(old_val)},
                new_val={"priority": classification.suggested_priority},
            )

        elif field == "category" and classification.suggested_category_id is not None:
            old_val = ticket.category_id
            ticket.category_id = classification.suggested_category_id
            await repo.record_history(
                ticket_id=ticket_id,
                actor_id=current_user.local_user_id,
                field="category_id",
                old_val={"category_id": str(old_val) if old_val else None},
                new_val={"category_id": str(classification.suggested_category_id)},
            )

        elif field == "team" and classification.suggested_team_id is not None:
            old_val = ticket.team_id
            ticket.team_id = classification.suggested_team_id
            await repo.record_history(
                ticket_id=ticket_id,
                actor_id=current_user.local_user_id,
                field="team_id",
                old_val={"team_id": str(old_val) if old_val else None},
                new_val={"team_id": str(classification.suggested_team_id)},
            )

    db.add(ticket)
    await db.flush()
    await db.refresh(ticket)

    # Capture HITL ground truth (current ticket values after applying accepted fields)
    classification.accepted = True
    classification.accepted_fields = body.fields
    classification.accepted_by = current_user.local_user_id
    classification.actual_priority = (
        ticket.priority.value
        if hasattr(ticket.priority, "value")
        else str(ticket.priority)
    )
    classification.actual_category_id = ticket.category_id
    classification.actual_team_id = ticket.team_id

    db.add(classification)
    await db.flush()
    await db.refresh(classification)
    await db.commit()

    return AIClassificationResponse.model_validate(classification)


# ---------------------------------------------------------------------------
# POST /tickets/{ticket_id}/ai-classification/reject
# ---------------------------------------------------------------------------


@router.post(
    "/{ticket_id}/ai-classification/reject",
    response_model=AIClassificationResponse,
)
async def reject_classification(
    ticket_id: UUID,
    current_user: CurrentUser = Depends(
        require_role("agent", "team_lead", "manager", "admin")
    ),
    db: AsyncSession = Depends(get_db),
) -> AIClassificationResponse:
    """Reject the AI classification. Records current ticket values as ground truth."""
    if current_user.local_user_id is None:
        raise AuthorizationError("Tenant context required")

    ticket = await _get_ticket(ticket_id, current_user, db)

    result = await db.execute(
        select(AITicketClassification).where(
            AITicketClassification.ticket_id == ticket_id
        )
    )
    classification = result.scalar_one_or_none()
    if classification is None:
        raise ResourceNotFoundError("ai_classification", str(ticket_id))

    classification.accepted = False
    classification.accepted_by = current_user.local_user_id
    # Record ground truth — what the agent has set (or kept)
    classification.actual_priority = (
        ticket.priority.value
        if hasattr(ticket.priority, "value")
        else str(ticket.priority)
    )
    classification.actual_category_id = ticket.category_id
    classification.actual_team_id = ticket.team_id

    db.add(classification)
    await db.flush()
    await db.refresh(classification)
    await db.commit()

    return AIClassificationResponse.model_validate(classification)


# ---------------------------------------------------------------------------
# GET /tickets/{ticket_id}/ai-classification
# ---------------------------------------------------------------------------


@router.get(
    "/{ticket_id}/ai-classification",
    response_model=AIClassificationResponse,
)
async def get_classification(
    ticket_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AIClassificationResponse:
    """Return the AI classification for a ticket, or 404 if not yet generated."""
    await _get_ticket(ticket_id, current_user, db)

    result = await db.execute(
        select(AITicketClassification).where(
            AITicketClassification.ticket_id == ticket_id
        )
    )
    classification = result.scalar_one_or_none()
    if classification is None:
        raise ResourceNotFoundError("ai_classification", str(ticket_id))

    return AIClassificationResponse.model_validate(classification)


# ===========================================================================
# DUPLICATE ENDPOINTS
# ===========================================================================

# ---------------------------------------------------------------------------
# POST /tickets/{ticket_id}/ai-duplicates/{candidate_id}/dismiss
# ---------------------------------------------------------------------------


@router.post(
    "/{ticket_id}/ai-duplicates/{candidate_id}/dismiss",
    status_code=204,
)
async def dismiss_duplicate(
    ticket_id: UUID,
    candidate_id: UUID,
    current_user: CurrentUser = Depends(
        require_role("agent", "team_lead", "manager", "admin")
    ),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Dismiss a duplicate candidate — agent disagrees with the suggestion."""
    if current_user.local_user_id is None:
        raise AuthorizationError("Tenant context required")

    await _get_ticket(ticket_id, current_user, db)

    result = await db.execute(
        select(AIDuplicateCandidate).where(
            AIDuplicateCandidate.id == candidate_id,
            AIDuplicateCandidate.ticket_id == ticket_id,
        )
    )
    candidate = result.scalar_one_or_none()
    if candidate is None:
        raise ResourceNotFoundError("ai_duplicate_candidate", str(candidate_id))

    candidate.dismissed = True
    candidate.dismissed_by = current_user.local_user_id
    db.add(candidate)
    await db.commit()


# ---------------------------------------------------------------------------
# GET /tickets/{ticket_id}/ai-duplicates
# ---------------------------------------------------------------------------


@router.get(
    "/{ticket_id}/ai-duplicates",
    response_model=list[AIDuplicateCandidateResponse],
)
async def list_duplicates(
    ticket_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[AIDuplicateCandidateResponse]:
    """Return all duplicate candidates for a ticket (including dismissed)."""
    await _get_ticket(ticket_id, current_user, db)

    result = await db.execute(
        select(AIDuplicateCandidate)
        .where(AIDuplicateCandidate.ticket_id == ticket_id)
        .order_by(AIDuplicateCandidate.similarity_score.desc())
    )
    rows = list(result.scalars().all())
    return [AIDuplicateCandidateResponse.model_validate(r) for r in rows]


# ===========================================================================
# REPLY SUGGESTION ENDPOINTS
# POST /tickets/{ticket_id}/ai-suggest-reply
# GET  /tickets/{ticket_id}/ai-suggest-reply/latest
# ===========================================================================

# Module-level ReplySuggester singleton (stateless — safe to share)
_reply_suggester = ReplySuggester()


# ---------------------------------------------------------------------------
# POST /tickets/{ticket_id}/ai-suggest-reply   (201)
# ---------------------------------------------------------------------------


@router.post(
    "/{ticket_id}/ai-suggest-reply",
    response_model=AIResponseSuggestionResponse,
    status_code=201,
)
async def suggest_reply(
    ticket_id: UUID,
    current_user: CurrentUser = Depends(
        require_role("agent", "team_lead", "manager", "admin")
    ),
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
) -> AIResponseSuggestionResponse:
    """Generate and persist an AI draft reply for a ticket (agent+ only).

    Calls ReplySuggester which:
      - fetches the last 10 public comments
      - fetches any linked KB article excerpts
      - calls Claude with all dynamic content in the user turn (never system)
      - persists the result as AIResponseSuggestion (used=False)

    Returns the new suggestion with HTTP 201.
    """
    if current_user.tenant_id is None:
        raise AuthorizationError("Tenant context required")

    ticket = await _get_ticket(ticket_id, current_user, db)

    suggestion = await _reply_suggester.suggest_reply(
        db=db,
        redis=redis,
        ticket=ticket,
        tenant_id=current_user.tenant_id,
    )
    await db.commit()

    return AIResponseSuggestionResponse.model_validate(suggestion)


# ---------------------------------------------------------------------------
# GET /tickets/{ticket_id}/ai-suggest-reply/latest
# ---------------------------------------------------------------------------


@router.get(
    "/{ticket_id}/ai-suggest-reply/latest",
    response_model=AIResponseSuggestionResponse,
)
async def get_latest_reply_suggestion(
    ticket_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AIResponseSuggestionResponse:
    """Return the most recent AIResponseSuggestion for the ticket, or 404.

    If the suggestion was created within the last 60 seconds, set used=True
    and record used_by=current_user.local_user_id (per §7A.5 "used within 60s"
    rule — indicates the agent viewed the suggestion to compose their reply).
    """
    from datetime import timezone

    await _get_ticket(ticket_id, current_user, db)

    result = await db.execute(
        select(AIResponseSuggestion)
        .where(AIResponseSuggestion.ticket_id == ticket_id)
        .order_by(AIResponseSuggestion.created_at.desc())
        .limit(1)
    )
    suggestion = result.scalar_one_or_none()
    if suggestion is None:
        raise ResourceNotFoundError("ai_response_suggestion", str(ticket_id))

    # Mark used if viewed within 60 seconds of creation
    now_utc = datetime.now(tz=timezone.utc)
    created_utc = suggestion.created_at
    if created_utc.tzinfo is None:
        # Naive datetime from DB — treat as UTC
        from datetime import timezone as _tz

        created_utc = created_utc.replace(tzinfo=_tz.utc)

    age_seconds = (now_utc - created_utc).total_seconds()
    if age_seconds <= 60 and not suggestion.used:
        suggestion.used = True
        if current_user.local_user_id is not None:
            suggestion.used_by = current_user.local_user_id
        db.add(suggestion)
        await db.flush()
        await db.refresh(suggestion)
        await db.commit()

    return AIResponseSuggestionResponse.model_validate(suggestion)


# ===========================================================================
# HITL EXPORT — /ai/classification-dataset
# Registered on ai_router (prefix="/ai") → mounts at /api/v1/ai/classification-dataset
# ===========================================================================


@ai_router.get(
    "/classification-dataset",
    tags=["ai-admin"],
    include_in_schema=True,
)
async def export_classification_dataset(
    format: str = Query(default="jsonl", pattern="^(jsonl|json)$"),
    limit: int = Query(default=10_000, ge=1, le=100_000),
    current_user: CurrentUser = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Export the HITL classification dataset for fine-tuning.

    Returns all AITicketClassification rows where accepted IS NOT NULL,
    joined with ticket title and description.

    format=jsonl  (default): StreamingResponse, one JSON object per line.
    format=json:              Regular JSON list (may be large).
    """
    if current_user.tenant_id is None:
        raise AuthorizationError("Tenant context required")

    from app.models.identity import Team
    from app.models.ticket import Ticket as TicketModel, TicketCategory

    # Join classification → ticket → category/team names
    q = (
        select(
            AITicketClassification,
            TicketModel.title,
            TicketModel.description,
            TicketCategory.name.label("actual_category_name"),
            Team.name.label("actual_team_name"),
        )
        .join(TicketModel, TicketModel.id == AITicketClassification.ticket_id)
        .outerjoin(
            TicketCategory,
            TicketCategory.id == AITicketClassification.actual_category_id,
        )
        .outerjoin(
            Team,
            Team.id == AITicketClassification.actual_team_id,
        )
        .where(
            TicketModel.tenant_id == current_user.tenant_id,
            AITicketClassification.accepted.isnot(None),
        )
        .order_by(AITicketClassification.created_at.asc())
        .limit(limit)
    )

    result = await db.execute(q)
    rows = result.all()

    def _row_to_dict(row: Any) -> dict:
        clf: AITicketClassification = row[0]
        return {
            "ticket_id": str(clf.ticket_id),
            "title": row[1],
            "description": row[2],
            "actual_priority": clf.actual_priority,
            "actual_category_name": row[3],
            "actual_team_name": row[4],
            "accepted": clf.accepted,
            "confidence": clf.confidence,
            "model_version": clf.model_version,
        }

    if format == "jsonl":
        def _jsonl_generator():
            for row in rows:
                yield json.dumps(_row_to_dict(row), ensure_ascii=False) + "\n"

        return StreamingResponse(
            _jsonl_generator(),
            media_type="application/x-ndjson",
            headers={
                "Content-Disposition": 'attachment; filename="classification_dataset.jsonl"'
            },
        )

    # Regular JSON
    return [_row_to_dict(r) for r in rows]
