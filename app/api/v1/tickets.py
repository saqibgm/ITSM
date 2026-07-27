"""Ticket CRUD endpoints — comments, attachments, tags, watchers.

IMPORTANT — route ordering:
  Static-path routes (e.g. /tags) are registered BEFORE parametric routes
  (e.g. /{ticket_id}) to prevent FastAPI from capturing the literal segment
  "tags" as a UUID path parameter.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentUser, get_current_user, require_role
from app.config import get_settings
from app.database import get_db
from app.exceptions import AuthorizationError, ResourceNotFoundError, ValidationError
from app.models.ticket import (
    Ticket,
    TicketApproval,
    TicketAttachment,
    TicketComment,
    TicketEscalation,
    TicketHistory,
    TicketPriority,
    TicketStatus,
    TicketTag,
    TicketTagAssignment,
    TicketType,
    TicketWatcher,
)
from app.redis_client import get_redis
from app.repositories.ticket_repo import TicketRepository
from app.schemas.common import PaginatedResponse
from app.schemas.ticket import (
    AddWatcherRequest,
    AssignTagRequest,
    AttachmentPresignResponse,
    AttachmentResponse,
    CommentResponse,
    CreateAttachmentRequest,
    CreateCommentRequest,
    CreateTagRequest,
    CreateTicketRequest,
    TagResponse,
    TicketHistoryResponse,
    TicketResponse,
    UpdateCommentRequest,
    UpdateTicketRequest,
    WatcherResponse,
)
from app.services.storage_service import get_storage_service
from app.services.ticket_service import CreateTicketData, TicketService

router = APIRouter(prefix="/tickets", tags=["tickets"])

_service = TicketService()

# Roles that may see/create internal notes
_INTERNAL_ROLES = {"agent", "team_lead", "manager", "admin"}
# Manager-or-above roles
_MANAGER_ROLES = {"manager", "admin"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_tenant(current_user: CurrentUser) -> None:
    if current_user.tenant_id is None or current_user.local_user_id is None:
        raise AuthorizationError("Tenant context required")


def _resolve_list_scope(current_user: CurrentUser) -> UUID | None:
    """Tenant scope for list/read endpoints.

    Tenant users always carry a resolved tenant_id — returned as-is.
    Platform users may have picked one tenant via X-Tenant-ID (returned as-is)
    or none at all, in which case None is returned to mean "all tenants" —
    the repo then omits the tenant filter and the response includes tenant_id
    so the caller can render a Tenant column / cross-tenant view.
    """
    if current_user.tenant_id is not None:
        return current_user.tenant_id
    if current_user.tier == "platform":
        return None
    raise AuthorizationError("Tenant context required")


def _is_agent_or_above(current_user: CurrentUser) -> bool:
    return bool(set(current_user.roles) & _INTERNAL_ROLES)


def _is_manager_or_above(current_user: CurrentUser) -> bool:
    return bool(set(current_user.roles) & _MANAGER_ROLES)


def _require_owner_or_agent(current_user: CurrentUser, ticket) -> None:
    """Authorize a write on a specific ticket.

    Agents and above may act on any ticket in the tenant. Everyone else
    (end users / service accounts) may act only on a ticket they requested.
    """
    if _is_agent_or_above(current_user):
        return
    if ticket.requester_id != current_user.local_user_id:
        raise AuthorizationError("You can only act on tickets you created")


async def _attach_product_names(
    db: AsyncSession, responses: list[TicketResponse]
) -> list[TicketResponse]:
    """Resolve product_name on ticket responses from the products table.

    One batched query for all distinct product_ids on the page (no N+1).
    Tickets without a product keep product_name=None (shown blank in clients).
    """
    ids = {r.product_id for r in responses if r.product_id}
    if not ids:
        return responses
    from app.models.identity import Product

    rows = (
        await db.execute(select(Product.id, Product.name).where(Product.id.in_(ids)))
    ).all()
    name_map = {pid: name for pid, name in rows}
    for r in responses:
        if r.product_id:
            r.product_name = name_map.get(r.product_id)
    return responses


def _display_name(first: str | None, last: str | None, email: str | None) -> str | None:
    """Human label for a user: "First Last", else email, else None."""
    full = f"{first or ''} {last or ''}".strip()
    return full or (email or None)


async def _load_user_names(db: AsyncSession, ids: set) -> dict:
    """Batch-load {user_id: (display_name, email)} for the given user ids.

    One query for the whole page (no N+1), mirroring _attach_product_names.
    """
    ids = {i for i in ids if i}
    if not ids:
        return {}
    from app.models.identity import User

    rows = (
        await db.execute(
            select(User.id, User.first_name, User.last_name, User.email).where(
                User.id.in_(ids)
            )
        )
    ).all()
    return {uid: (_display_name(fn, ln, em), em) for uid, fn, ln, em in rows}


async def _attach_user_names(
    db: AsyncSession, responses: list[TicketResponse]
) -> list[TicketResponse]:
    """Resolve requester_name/requester_email/assignee_name from the users table."""
    ids = set()
    for r in responses:
        ids.add(r.requester_id)
        if r.assignee_id:
            ids.add(r.assignee_id)
    name_map = await _load_user_names(db, ids)
    for r in responses:
        req = name_map.get(r.requester_id)
        if req:
            r.requester_name, r.requester_email = req
        if r.assignee_id:
            asg = name_map.get(r.assignee_id)
            if asg:
                r.assignee_name = asg[0]
    return responses


async def _attach_comment_authors(
    db: AsyncSession, responses: list[CommentResponse]
) -> list[CommentResponse]:
    """Resolve author_name on comment responses from the users table."""
    name_map = await _load_user_names(db, {r.author_id for r in responses})
    for r in responses:
        author = name_map.get(r.author_id)
        if author:
            r.author_name = author[0]
    return responses


# ===========================================================================
# COLLECTION-LEVEL ROUTES  (no {ticket_id} segment — must come first)
# ===========================================================================


# ---------------------------------------------------------------------------
# GET /tickets
# ---------------------------------------------------------------------------


@router.get("", response_model=PaginatedResponse[TicketResponse])
async def list_tickets(
    status: list[TicketStatus] | None = Query(default=None),
    priority: list[TicketPriority] | None = Query(default=None),
    type: list[TicketType] | None = Query(default=None),
    assignee_id: UUID | None = Query(default=None),
    requester_id: UUID | None = Query(default=None),
    team_id: UUID | None = Query(default=None),
    category_id: UUID | None = Query(default=None),
    product_id: UUID | None = Query(default=None),
    search: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=25, ge=1, le=100),
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PaginatedResponse[TicketResponse]:
    tenant_id = _resolve_list_scope(current_user)

    repo = TicketRepository(db)
    items, next_cursor = await repo.list_tickets(
        tenant_id=tenant_id,
        status=status,
        priority=priority,
        type=type,
        assignee_id=assignee_id,
        requester_id=requester_id,
        team_id=team_id,
        category_id=category_id,
        product_id=product_id,
        search=search,
        cursor=cursor,
        limit=limit,
    )
    return PaginatedResponse(
        items=await _attach_user_names(
            db,
            await _attach_product_names(
                db, [TicketResponse.model_validate(t) for t in items]
            ),
        ),
        next_cursor=next_cursor,
    )


# ---------------------------------------------------------------------------
# POST /tickets
# ---------------------------------------------------------------------------


@router.post("", response_model=TicketResponse, status_code=201)
async def create_ticket(
    body: CreateTicketRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
) -> TicketResponse:
    if current_user.tenant_id is None or current_user.local_user_id is None:
        raise AuthorizationError("Tenant context required")

    data = CreateTicketData(
        title=body.title,
        description=body.description,
        type=body.type,
        priority=body.priority,
        category_id=body.category_id,
        product_id=body.product_id,
        product_slug=body.product_slug,
        product_name=body.product_name,
        department_id=body.department_id,
        idempotency_key=idempotency_key,
    )

    ticket = await _service.create_ticket(
        tenant_id=current_user.tenant_id,
        requester_id=current_user.local_user_id,
        data=data,
        redis=redis,
        db=db,
    )
    await db.commit()
    await db.refresh(ticket)

    # Webhook fan-out — fire-and-forget via Celery; never blocks the response
    try:
        from app.workers.tasks_webhooks import dispatch_webhook_event
        dispatch_webhook_event.delay(
            str(current_user.tenant_id),
            "ticket.created",
            TicketResponse.model_validate(ticket).model_dump(mode="json"),
        )
    except Exception:
        pass  # webhook dispatch failure must never fail the ticket creation

    return (
        await _attach_user_names(
            db, await _attach_product_names(db, [TicketResponse.model_validate(ticket)])
        )
    )[0]


# ===========================================================================
# TAGS — tenant-level collection  (static path — must precede /{ticket_id})
# ===========================================================================


# ---------------------------------------------------------------------------
# GET /tickets/tags
# ---------------------------------------------------------------------------


@router.get("/tags", response_model=list[TagResponse], tags=["ticket-tags"])
async def list_tags(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[TagResponse]:
    if current_user.tenant_id is None:
        raise AuthorizationError("Tenant context required")

    result = await db.execute(
        select(TicketTag)
        .where(TicketTag.tenant_id == current_user.tenant_id)
        .order_by(TicketTag.name.asc())
    )
    rows = list(result.scalars().all())
    return [TagResponse.model_validate(r) for r in rows]


# ---------------------------------------------------------------------------
# POST /tickets/tags  (manager/admin only)
# ---------------------------------------------------------------------------


@router.post(
    "/tags",
    response_model=TagResponse,
    status_code=201,
    tags=["ticket-tags"],
)
async def create_tag(
    body: CreateTagRequest,
    current_user: CurrentUser = Depends(require_role("manager", "admin")),
    db: AsyncSession = Depends(get_db),
) -> TagResponse:
    if current_user.tenant_id is None:
        raise AuthorizationError("Tenant context required")

    tag = TicketTag(
        tenant_id=current_user.tenant_id,
        name=body.name,
        color=body.color,
    )
    db.add(tag)
    await db.flush()
    await db.refresh(tag)
    await db.commit()
    return TagResponse.model_validate(tag)


# ===========================================================================
# SINGLE-TICKET ROUTES  (parametric — registered after all static routes)
# ===========================================================================


# ---------------------------------------------------------------------------
# GET /tickets/by-number/{ticket_number}
# ---------------------------------------------------------------------------


@router.get("/by-number/{ticket_number}", response_model=TicketResponse)
async def get_ticket_by_number(
    ticket_number: str,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TicketResponse:
    """Resolve a human-facing ticket number (e.g. "00042") to the full
    ticket. Used by clients — like the mobile chat client — that only have
    the number a user typed, not the UUID every other route expects."""
    tenant_id = _resolve_list_scope(current_user)

    repo = TicketRepository(db)
    ticket = await repo.get_by_number(ticket_number, tenant_id)
    if ticket is None:
        raise ResourceNotFoundError("Ticket", ticket_number)
    return (
        await _attach_user_names(
            db, await _attach_product_names(db, [TicketResponse.model_validate(ticket)])
        )
    )[0]


# ---------------------------------------------------------------------------
# GET /tickets/{ticket_id}
# ---------------------------------------------------------------------------


@router.get("/{ticket_id}", response_model=TicketResponse)
async def get_ticket(
    ticket_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TicketResponse:
    tenant_id = _resolve_list_scope(current_user)

    repo = TicketRepository(db)
    ticket = await repo.get_or_404(ticket_id, tenant_id)
    return (
        await _attach_user_names(
            db, await _attach_product_names(db, [TicketResponse.model_validate(ticket)])
        )
    )[0]


@router.get("/{ticket_id}/transitions")
async def get_ticket_transitions(
    ticket_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Valid next statuses for this ticket per the state machine
    (``ticket_status_transitions``). The UI uses this to offer only reachable
    statuses, instead of every status — picking an unreachable one would 409
    ("Cannot transition from X to Y")."""
    tenant_id = _resolve_list_scope(current_user)
    repo = TicketRepository(db)
    ticket = await repo.get_or_404(ticket_id, tenant_id)
    valid = await repo.get_valid_transitions(ticket.status, ticket.type)
    return {"current": ticket.status.value, "valid": valid}


# ---------------------------------------------------------------------------
# PATCH /tickets/{ticket_id}
# ---------------------------------------------------------------------------


@router.patch("/{ticket_id}", response_model=TicketResponse)
async def update_ticket(
    ticket_id: UUID,
    body: UpdateTicketRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TicketResponse:
    if current_user.tenant_id is None or current_user.local_user_id is None:
        raise AuthorizationError("Tenant context required")

    repo = TicketRepository(db)
    ticket = await repo.get_or_404(ticket_id, current_user.tenant_id)

    updates = body.model_dump(exclude_none=True)

    # RBAC: agents and above may change any field on any ticket in the tenant.
    # Everyone else (end users / service accounts) may change only `status`,
    # and only on a ticket they themselves requested. This preserves the
    # chatbot's user-resolve flow (it only ever sends {"status": ...}) while
    # blocking IDOR / priority/assignee escalation by non-agents.
    if updates and not _is_agent_or_above(current_user):
        if ticket.requester_id != current_user.local_user_id:
            raise AuthorizationError("You can only modify tickets you created")
        restricted = set(updates) - {"status"}
        if restricted:
            raise AuthorizationError(
                "Requesters may only change a ticket's status, not: "
                + ", ".join(sorted(restricted))
            )

    # Approvals are decided via POST /{id}/approval (with approver authz + audit),
    # never by a direct status PATCH — block the shortcut.
    if updates.get("status") in ("approved", "rejected"):
        raise ValidationError("Use POST /tickets/{id}/approval to approve or reject this ticket.")

    if "assignee_id" in updates or "team_id" in updates:
        ticket = await _service.assign_ticket(
            ticket=ticket,
            assignee_id=updates.pop("assignee_id", ticket.assignee_id),
            team_id=updates.pop("team_id", ticket.team_id),
            actor_id=current_user.local_user_id,
            db=db,
        )

    if updates:
        ticket = await _service.update_ticket(
            ticket=ticket,
            updates=updates,
            actor_id=current_user.local_user_id,
            db=db,
        )

    # Entering pending_approval opens a pending approval record (idempotent) so
    # an approver can act on it via the approval endpoint.
    if ticket.status == TicketStatus.pending_approval:
        _open = await db.execute(
            select(TicketApproval).where(
                TicketApproval.ticket_id == ticket.id,
                TicketApproval.status == "pending",
            )
        )
        if _open.scalar_one_or_none() is None:
            db.add(TicketApproval(ticket_id=ticket.id, requested_by=current_user.local_user_id))

    await db.commit()
    await db.refresh(ticket)

    # Webhook fan-out — fire-and-forget via Celery; never blocks the response
    try:
        from app.workers.tasks_webhooks import dispatch_webhook_event
        dispatch_webhook_event.delay(
            str(current_user.tenant_id),
            "ticket.updated",
            TicketResponse.model_validate(ticket).model_dump(mode="json"),
        )
    except Exception:
        pass  # webhook dispatch failure must never fail the update

    # Fire the (previously-unused) pending-approval automation trigger so tenant
    # rules / approver notifications can run.
    if ticket.status == TicketStatus.pending_approval:
        try:
            from app.workers.tasks_automation import run_ticket_automation
            run_ticket_automation.delay(str(ticket.id), "ticket_pending_approval")
        except Exception:
            pass

    return (
        await _attach_user_names(
            db, await _attach_product_names(db, [TicketResponse.model_validate(ticket)])
        )
    )[0]


# ---------------------------------------------------------------------------
# POST /tickets/{ticket_id}/approval  (decide a pending approval)
# ---------------------------------------------------------------------------


class ApprovalDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: str = Field(..., pattern="^(approve|reject)$")
    comment: str | None = Field(default=None, max_length=2000)


@router.post("/{ticket_id}/approval", response_model=TicketResponse)
async def decide_approval(
    ticket_id: UUID,
    body: ApprovalDecisionRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TicketResponse:
    """Approve/reject a ticket pending approval — authz'd + audited, and drives
    the validated status transition (the only way to reach approved/rejected)."""
    _require_tenant(current_user)
    repo = TicketRepository(db)
    ticket = await repo.get_or_404(ticket_id, current_user.tenant_id)

    res = await db.execute(
        select(TicketApproval)
        .where(TicketApproval.ticket_id == ticket.id, TicketApproval.status == "pending")
        .order_by(TicketApproval.created_at.desc())
    )
    approval = res.scalars().first()
    if approval is None:
        raise ValidationError("This ticket has no pending approval.")

    # Only a manager/admin, or the explicitly-designated approver, may decide.
    is_designated = (
        approval.approver_id is not None
        and approval.approver_id == current_user.local_user_id
    )
    if not (_is_manager_or_above(current_user) or is_designated):
        raise AuthorizationError("Only an approver or a manager/admin may decide this approval.")

    approved = body.decision == "approve"
    new_status = TicketStatus.approved if approved else TicketStatus.rejected

    # Record the decision (audit), then drive the validated status transition.
    approval.status = "approved" if approved else "rejected"
    approval.decided_by = current_user.local_user_id
    approval.decided_at = func.now()
    approval.comment = body.comment
    db.add(approval)

    ticket = await _service.update_ticket(
        ticket=ticket,
        updates={"status": new_status.value},
        actor_id=current_user.local_user_id,
        db=db,
    )
    await db.commit()
    await db.refresh(ticket)
    return (
        await _attach_user_names(
            db, await _attach_product_names(db, [TicketResponse.model_validate(ticket)])
        )
    )[0]


# ---------------------------------------------------------------------------
# DELETE /tickets/{ticket_id}  (soft-delete — manager/admin only)
# ---------------------------------------------------------------------------


@router.delete("/{ticket_id}", status_code=204)
async def delete_ticket(
    ticket_id: UUID,
    current_user: CurrentUser = Depends(require_role("manager", "admin")),
    db: AsyncSession = Depends(get_db),
) -> None:
    if current_user.tenant_id is None or current_user.local_user_id is None:
        raise AuthorizationError("Tenant context required")

    repo = TicketRepository(db)
    ticket = await repo.get_or_404(ticket_id, current_user.tenant_id)

    ticket.deleted_at = func.now()  # type: ignore[assignment]
    db.add(ticket)
    await db.commit()


# ---------------------------------------------------------------------------
# GET /tickets/{ticket_id}/history
# ---------------------------------------------------------------------------


@router.get("/{ticket_id}/history", response_model=list[TicketHistoryResponse])
async def get_ticket_history(
    ticket_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[TicketHistoryResponse]:
    if current_user.tenant_id is None:
        raise AuthorizationError("Tenant context required")

    repo = TicketRepository(db)
    await repo.get_or_404(ticket_id, current_user.tenant_id)

    result = await db.execute(
        select(TicketHistory)
        .where(TicketHistory.ticket_id == ticket_id)
        .order_by(TicketHistory.changed_at.asc())
    )
    rows = list(result.scalars().all())
    return [TicketHistoryResponse.model_validate(r) for r in rows]


# ===========================================================================
# COMMENTS
# ===========================================================================


# ---------------------------------------------------------------------------
# POST /tickets/{ticket_id}/comments
# ---------------------------------------------------------------------------


@router.post("/{ticket_id}/comments", response_model=CommentResponse, status_code=201)
async def create_comment(
    ticket_id: UUID,
    body: CreateCommentRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CommentResponse:
    _require_tenant(current_user)

    # Internal notes require agent+ role
    if body.is_internal and not _is_agent_or_above(current_user):
        raise AuthorizationError("Only agents and above may create internal notes")

    repo = TicketRepository(db)
    # Scoped to tenant — IDOR guard
    ticket = await repo.get_or_404(ticket_id, current_user.tenant_id)  # type: ignore[arg-type]
    # Non-agents may only comment on tickets they themselves requested
    _require_owner_or_agent(current_user, ticket)

    # Validate parent comment belongs to the same ticket
    if body.parent_comment_id is not None:
        parent_result = await db.execute(
            select(TicketComment).where(
                TicketComment.id == body.parent_comment_id,
                TicketComment.ticket_id == ticket_id,
            )
        )
        if parent_result.scalar_one_or_none() is None:
            raise ResourceNotFoundError("ticket_comment", str(body.parent_comment_id))

    comment = TicketComment(
        ticket_id=ticket_id,
        author_id=current_user.local_user_id,
        body=body.body,
        is_internal=body.is_internal,
        parent_comment_id=body.parent_comment_id,
    )
    db.add(comment)
    await db.flush()
    await db.refresh(comment)
    await db.commit()
    return (await _attach_comment_authors(db, [CommentResponse.model_validate(comment)]))[0]


# ---------------------------------------------------------------------------
# GET /tickets/{ticket_id}/comments
# ---------------------------------------------------------------------------


@router.get("/{ticket_id}/comments", response_model=list[CommentResponse])
async def list_comments(
    ticket_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[CommentResponse]:
    # Read scope only — platform users (no local_user_id) may view; resolves the
    # ticket's tenant from X-Tenant-ID, like get_ticket.
    tenant_id = _resolve_list_scope(current_user)

    repo = TicketRepository(db)
    await repo.get_or_404(ticket_id, tenant_id)

    q = select(TicketComment).where(TicketComment.ticket_id == ticket_id)

    # end_users see only public (non-internal) comments — filtered at query level
    if not _is_agent_or_above(current_user):
        q = q.where(TicketComment.is_internal.is_(False))

    q = q.order_by(TicketComment.created_at.asc())
    result = await db.execute(q)
    rows = list(result.scalars().all())
    return await _attach_comment_authors(
        db, [CommentResponse.model_validate(r) for r in rows]
    )


# ---------------------------------------------------------------------------
# PATCH /tickets/{ticket_id}/comments/{comment_id}
# ---------------------------------------------------------------------------


@router.patch("/{ticket_id}/comments/{comment_id}", response_model=CommentResponse)
async def update_comment(
    ticket_id: UUID,
    comment_id: UUID,
    body: UpdateCommentRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CommentResponse:
    _require_tenant(current_user)

    repo = TicketRepository(db)
    await repo.get_or_404(ticket_id, current_user.tenant_id)  # type: ignore[arg-type]

    result = await db.execute(
        select(TicketComment).where(
            TicketComment.id == comment_id,
            TicketComment.ticket_id == ticket_id,
        )
    )
    comment = result.scalar_one_or_none()
    if comment is None:
        raise ResourceNotFoundError("ticket_comment", str(comment_id))

    # Only the author may edit
    if comment.author_id != current_user.local_user_id:
        raise AuthorizationError("Only the comment author may edit this comment")

    # 5-minute edit window — comparison uses DB server time (func.now()), never app clock
    elapsed_result = await db.execute(
        select(
            func.extract("epoch", func.now() - comment.created_at).label("elapsed_seconds")
        )
    )
    elapsed_seconds = elapsed_result.scalar_one()
    if elapsed_seconds is None or elapsed_seconds > 300:
        raise ValidationError("Comment edit window has expired (5 minutes)")

    comment.body = body.body
    db.add(comment)
    await db.flush()
    await db.refresh(comment)
    await db.commit()
    return (await _attach_comment_authors(db, [CommentResponse.model_validate(comment)]))[0]


# ---------------------------------------------------------------------------
# DELETE /tickets/{ticket_id}/comments/{comment_id}  (soft delete)
# ---------------------------------------------------------------------------


@router.delete("/{ticket_id}/comments/{comment_id}", status_code=204)
async def delete_comment(
    ticket_id: UUID,
    comment_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    _require_tenant(current_user)

    repo = TicketRepository(db)
    await repo.get_or_404(ticket_id, current_user.tenant_id)  # type: ignore[arg-type]

    result = await db.execute(
        select(TicketComment).where(
            TicketComment.id == comment_id,
            TicketComment.ticket_id == ticket_id,
        )
    )
    comment = result.scalar_one_or_none()
    if comment is None:
        raise ResourceNotFoundError("ticket_comment", str(comment_id))

    is_author = comment.author_id == current_user.local_user_id
    if not is_author and not _is_manager_or_above(current_user):
        raise AuthorizationError(
            "Only the comment author or a manager/admin may delete this comment"
        )

    # Soft delete: body redacted, deleted_at stamped
    comment.deleted_at = func.now()  # type: ignore[assignment]
    comment.body = "[deleted]"
    db.add(comment)
    await db.commit()


# ===========================================================================
# ATTACHMENTS
# ===========================================================================


# ---------------------------------------------------------------------------
# POST /tickets/{ticket_id}/attachments  — initiate presigned upload
# ---------------------------------------------------------------------------


@router.post(
    "/{ticket_id}/attachments",
    response_model=AttachmentPresignResponse,
    status_code=201,
)
async def create_attachment_presign(
    ticket_id: UUID,
    body: CreateAttachmentRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage=Depends(get_storage_service),
) -> AttachmentPresignResponse:
    _require_tenant(current_user)

    repo = TicketRepository(db)
    # Tenant-scoped lookup — IDOR guard; tenant_id never from request body
    ticket = await repo.get_or_404(ticket_id, current_user.tenant_id)  # type: ignore[arg-type]
    # Non-agents may only attach to tickets they themselves requested
    _require_owner_or_agent(current_user, ticket)

    # Validates MIME allowlist + extension blocklist + filename length
    upload_url, storage_key = await storage.presigned_upload_url(
        tenant_id=str(current_user.tenant_id),
        filename=body.filename,
        mime_type=body.mime_type,
        max_bytes=body.file_size if body.file_size > 0 else (25 * 1024 * 1024),
    )

    # Create DB record immediately; file_size is the declared value
    attachment = TicketAttachment(
        ticket_id=ticket_id,
        uploaded_by=current_user.local_user_id,
        filename=body.filename,
        storage_url=storage_key,
        file_size=body.file_size,
        mime_type=body.mime_type,
    )
    db.add(attachment)
    await db.flush()
    await db.refresh(attachment)
    await db.commit()

    settings = get_settings()
    return AttachmentPresignResponse(
        attachment_id=attachment.id,
        upload_url=upload_url,
        storage_key=storage_key,
        expires_in_seconds=settings.STORAGE_PRESIGN_EXPIRY_SECONDS,
    )


# ---------------------------------------------------------------------------
# GET /tickets/{ticket_id}/attachments
# ---------------------------------------------------------------------------


@router.get("/{ticket_id}/attachments", response_model=list[AttachmentResponse])
async def list_attachments(
    ticket_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage=Depends(get_storage_service),
) -> list[AttachmentResponse]:
    # Read scope only — platform users (no local_user_id) may view attachments.
    tenant_id = _resolve_list_scope(current_user)

    repo = TicketRepository(db)
    await repo.get_or_404(ticket_id, tenant_id)

    result = await db.execute(
        select(TicketAttachment)
        .where(TicketAttachment.ticket_id == ticket_id)
        .order_by(TicketAttachment.created_at.asc())
    )
    rows = list(result.scalars().all())
    out: list[AttachmentResponse] = []
    for r in rows:
        resp = AttachmentResponse.model_validate(r)
        try:
            resp.download_url = await storage.presigned_download_url(r.storage_url)
        except Exception:
            resp.download_url = None
        out.append(resp)
    return out


# ---------------------------------------------------------------------------
# DELETE /tickets/{ticket_id}/attachments/{attachment_id}
# ---------------------------------------------------------------------------


@router.delete("/{ticket_id}/attachments/{attachment_id}", status_code=204)
async def delete_attachment(
    ticket_id: UUID,
    attachment_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage=Depends(get_storage_service),
) -> None:
    _require_tenant(current_user)

    repo = TicketRepository(db)
    ticket = await repo.get_or_404(ticket_id, current_user.tenant_id)  # type: ignore[arg-type]
    # Non-agents may only delete attachments on tickets they requested
    _require_owner_or_agent(current_user, ticket)

    result = await db.execute(
        select(TicketAttachment).where(
            TicketAttachment.id == attachment_id,
            TicketAttachment.ticket_id == ticket_id,
        )
    )
    attachment = result.scalar_one_or_none()
    if attachment is None:
        raise ResourceNotFoundError("ticket_attachment", str(attachment_id))

    storage_key = attachment.storage_url

    # Hard-delete the DB record first
    await db.delete(attachment)
    await db.commit()

    # Best-effort S3 deletion — errors swallowed inside the service
    await storage.delete_object(storage_key)


# ===========================================================================
# TICKET-LEVEL TAG ASSIGNMENT
# ===========================================================================


# ---------------------------------------------------------------------------
# POST /tickets/{ticket_id}/tags
# ---------------------------------------------------------------------------


@router.post("/{ticket_id}/tags", status_code=204, tags=["ticket-tags"])
async def assign_tag(
    ticket_id: UUID,
    body: AssignTagRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    _require_tenant(current_user)

    repo = TicketRepository(db)
    ticket = await repo.get_or_404(ticket_id, current_user.tenant_id)  # type: ignore[arg-type]
    # Non-agents may only tag tickets they themselves requested
    _require_owner_or_agent(current_user, ticket)

    # Tag must belong to the same tenant — IDOR guard
    tag_result = await db.execute(
        select(TicketTag).where(
            TicketTag.id == body.tag_id,
            TicketTag.tenant_id == current_user.tenant_id,
        )
    )
    if tag_result.scalar_one_or_none() is None:
        raise ResourceNotFoundError("ticket_tag", str(body.tag_id))

    # Idempotent — ignore if already assigned
    existing = await db.execute(
        select(TicketTagAssignment).where(
            TicketTagAssignment.ticket_id == ticket_id,
            TicketTagAssignment.tag_id == body.tag_id,
        )
    )
    if existing.scalar_one_or_none() is None:
        db.add(TicketTagAssignment(ticket_id=ticket_id, tag_id=body.tag_id))
        await db.flush()
        await db.commit()


# ---------------------------------------------------------------------------
# DELETE /tickets/{ticket_id}/tags/{tag_id}
# ---------------------------------------------------------------------------


@router.delete("/{ticket_id}/tags/{tag_id}", status_code=204, tags=["ticket-tags"])
async def remove_tag(
    ticket_id: UUID,
    tag_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    _require_tenant(current_user)

    repo = TicketRepository(db)
    ticket = await repo.get_or_404(ticket_id, current_user.tenant_id)  # type: ignore[arg-type]
    # Non-agents may only untag tickets they themselves requested
    _require_owner_or_agent(current_user, ticket)

    result = await db.execute(
        select(TicketTagAssignment).where(
            TicketTagAssignment.ticket_id == ticket_id,
            TicketTagAssignment.tag_id == tag_id,
        )
    )
    assignment = result.scalar_one_or_none()
    if assignment is None:
        raise ResourceNotFoundError("ticket_tag_assignment", f"{ticket_id}/{tag_id}")

    await db.delete(assignment)
    await db.commit()


# ===========================================================================
# WATCHERS
# ===========================================================================


# ---------------------------------------------------------------------------
# POST /tickets/{ticket_id}/watchers
# ---------------------------------------------------------------------------


@router.post("/{ticket_id}/watchers", status_code=204, tags=["ticket-watchers"])
async def add_watcher(
    ticket_id: UUID,
    body: AddWatcherRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    _require_tenant(current_user)

    repo = TicketRepository(db)
    await repo.get_or_404(ticket_id, current_user.tenant_id)  # type: ignore[arg-type]

    await repo.add_watcher(ticket_id=ticket_id, user_id=body.user_id)
    await db.commit()


# ---------------------------------------------------------------------------
# DELETE /tickets/{ticket_id}/watchers/{user_id}
# ---------------------------------------------------------------------------


@router.delete(
    "/{ticket_id}/watchers/{user_id}",
    status_code=204,
    tags=["ticket-watchers"],
)
async def remove_watcher(
    ticket_id: UUID,
    user_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    _require_tenant(current_user)

    repo = TicketRepository(db)
    await repo.get_or_404(ticket_id, current_user.tenant_id)  # type: ignore[arg-type]

    # Self-removal always allowed; removing others requires manager+
    is_self = user_id == current_user.local_user_id
    if not is_self and not _is_manager_or_above(current_user):
        raise AuthorizationError(
            "You may only remove yourself as a watcher unless you are a manager or admin"
        )

    result = await db.execute(
        select(TicketWatcher).where(
            TicketWatcher.ticket_id == ticket_id,
            TicketWatcher.user_id == user_id,
        )
    )
    watcher = result.scalar_one_or_none()
    if watcher is None:
        raise ResourceNotFoundError("ticket_watcher", f"{ticket_id}/{user_id}")

    await db.delete(watcher)
    await db.commit()


# ---------------------------------------------------------------------------
# GET /tickets/{ticket_id}/watchers
# ---------------------------------------------------------------------------


@router.get(
    "/{ticket_id}/watchers",
    response_model=list[WatcherResponse],
    tags=["ticket-watchers"],
)
async def list_watchers(
    ticket_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[WatcherResponse]:
    # Read scope only — platform users (no local_user_id) may view watchers.
    tenant_id = _resolve_list_scope(current_user)

    repo = TicketRepository(db)
    await repo.get_or_404(ticket_id, tenant_id)

    result = await db.execute(
        select(TicketWatcher)
        .where(TicketWatcher.ticket_id == ticket_id)
        .order_by(TicketWatcher.added_at.asc())
    )
    rows = list(result.scalars().all())
    return [WatcherResponse.model_validate(r) for r in rows]


# ===========================================================================
# ESCALATION
# ===========================================================================


class EscalateTicketRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    escalated_to: UUID | None = None
    reason: str = Field(min_length=1, max_length=2000)


class EscalationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    ticket_id: UUID
    escalated_by: UUID
    escalated_to: UUID | None
    reason: str


# ---------------------------------------------------------------------------
# POST /tickets/{ticket_id}/escalate
# ---------------------------------------------------------------------------


@router.post(
    "/{ticket_id}/escalate",
    response_model=EscalationResponse,
    status_code=201,
    tags=["ticket-escalation"],
)
async def escalate_ticket(
    ticket_id: UUID,
    body: EscalateTicketRequest,
    current_user: CurrentUser = Depends(require_role("agent", "team_lead", "manager", "admin")),
    db: AsyncSession = Depends(get_db),
) -> EscalationResponse:
    """Escalate a ticket. Requires agent or above role.

    Creates a TicketEscalation record and sends a notification to the
    escalated_to user (or team lead if escalated_to is omitted).
    """
    if current_user.tenant_id is None or current_user.local_user_id is None:
        raise AuthorizationError("Tenant context required")

    repo = TicketRepository(db)
    ticket = await repo.get_or_404(ticket_id, current_user.tenant_id)

    escalation = TicketEscalation(
        ticket_id=ticket_id,
        escalated_by=current_user.local_user_id,
        escalated_to=body.escalated_to,
        reason=body.reason,
    )
    db.add(escalation)

    # Record in ticket history
    await repo.record_history(
        ticket_id=ticket_id,
        actor_id=current_user.local_user_id,
        field="escalated",
        old_val=None,
        new_val={
            "escalated_to": str(body.escalated_to) if body.escalated_to else None,
            "reason": body.reason,
        },
    )

    await db.flush()
    await db.refresh(escalation)
    await db.commit()

    # Send notification to escalation target (best-effort — not blocking)
    try:
        from app.models.notification import NotificationType
        from app.services.notification_service import NotificationService

        notify_service = NotificationService()
        recipients: list[UUID] = []

        if body.escalated_to is not None:
            recipients.append(body.escalated_to)
        elif ticket.team_id is not None:
            from app.models.identity import Team as TeamModel
            team_result = await db.execute(
                select(TeamModel.lead_id).where(TeamModel.id == ticket.team_id)
            )
            lead_id = team_result.scalar_one_or_none()
            if lead_id is not None:
                recipients.append(lead_id)

        if recipients:
            await notify_service.send_bulk(
                db=db,
                tenant_id=current_user.tenant_id,
                user_ids=recipients,
                type=NotificationType.ticket_updated,
                title=f"Ticket {ticket.ticket_number} has been escalated to you",
                body=body.reason,
                entity_type="ticket",
                entity_id=ticket_id,
            )
            await db.commit()
    except Exception:
        pass  # Notification failure must not fail the escalation

    return EscalationResponse.model_validate(escalation)


# ===========================================================================
# BULK OPERATIONS
# ===========================================================================


class BulkTicketOperation(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    ticket_ids: list[UUID] = Field(min_length=1, max_length=50)
    operation: str = Field(
        description="One of: assign, close, tag, change_priority"
    )
    assignee_id: UUID | None = None
    tag_id: UUID | None = None
    priority: TicketPriority | None = None


class BulkOperationResponse(BaseModel):
    updated: int
    failed: list[UUID]


# ---------------------------------------------------------------------------
# POST /tickets/bulk
# ---------------------------------------------------------------------------


@router.post(
    "/bulk",
    response_model=BulkOperationResponse,
    tags=["ticket-bulk"],
)
async def bulk_ticket_operation(
    body: BulkTicketOperation,
    current_user: CurrentUser = Depends(require_role("agent", "team_lead", "manager", "admin")),
    db: AsyncSession = Depends(get_db),
) -> BulkOperationResponse:
    """Apply an operation to up to 50 tickets at once.

    Supported operations: assign, close, tag, change_priority.
    Each ticket gets a history record. Tickets not found or not in the tenant
    are collected in the `failed` list — others are still processed.
    """
    if current_user.tenant_id is None or current_user.local_user_id is None:
        raise AuthorizationError("Tenant context required")

    valid_ops = {"assign", "close", "tag", "change_priority"}
    if body.operation not in valid_ops:
        raise ValidationError(f"Invalid operation '{body.operation}'. Valid: {sorted(valid_ops)}")

    if len(body.ticket_ids) > 50:
        raise ValidationError("Bulk operations are limited to 50 tickets per request")

    repo = TicketRepository(db)

    # Load all requested tickets scoped to tenant in one query
    result = await db.execute(
        select(Ticket).where(
            and_(
                Ticket.id.in_(body.ticket_ids),
                Ticket.tenant_id == current_user.tenant_id,
            )
        )
    )
    tickets = {t.id: t for t in result.scalars().all()}

    failed: list[UUID] = [tid for tid in body.ticket_ids if tid not in tickets]
    updated = 0

    for ticket in tickets.values():
        try:
            if body.operation == "assign":
                await _service.assign_ticket(
                    ticket=ticket,
                    assignee_id=body.assignee_id,
                    team_id=ticket.team_id,
                    actor_id=current_user.local_user_id,
                    db=db,
                )
                updated += 1

            elif body.operation == "close":
                valid = await repo.get_valid_transitions(ticket.status, ticket.type)
                if TicketStatus.closed.value in valid:
                    await _service.update_ticket(
                        ticket=ticket,
                        updates={"status": TicketStatus.closed},
                        actor_id=current_user.local_user_id,
                        db=db,
                    )
                    updated += 1
                else:
                    failed.append(ticket.id)

            elif body.operation == "tag":
                if body.tag_id is None:
                    failed.append(ticket.id)
                    continue

                # Verify tag belongs to tenant
                tag_result = await db.execute(
                    select(TicketTag).where(
                        and_(
                            TicketTag.id == body.tag_id,
                            TicketTag.tenant_id == current_user.tenant_id,
                        )
                    )
                )
                if tag_result.scalar_one_or_none() is None:
                    failed.append(ticket.id)
                    continue

                existing_tag = await db.execute(
                    select(TicketTagAssignment).where(
                        and_(
                            TicketTagAssignment.ticket_id == ticket.id,
                            TicketTagAssignment.tag_id == body.tag_id,
                        )
                    )
                )
                if existing_tag.scalar_one_or_none() is None:
                    db.add(TicketTagAssignment(ticket_id=ticket.id, tag_id=body.tag_id))
                await repo.record_history(
                    ticket_id=ticket.id,
                    actor_id=current_user.local_user_id,
                    field="tag",
                    old_val=None,
                    new_val={"tag_id": str(body.tag_id)},
                )
                updated += 1

            elif body.operation == "change_priority":
                if body.priority is None:
                    failed.append(ticket.id)
                    continue
                await _service.update_ticket(
                    ticket=ticket,
                    updates={"priority": body.priority},
                    actor_id=current_user.local_user_id,
                    db=db,
                )
                updated += 1

        except Exception:
            failed.append(ticket.id)

    await db.commit()
    return BulkOperationResponse(updated=updated, failed=failed)


# ===========================================================================
# TICKET-ASSET LINKS
# ===========================================================================

from app.models.asset import Asset as AssetModel
from app.models.asset import AssetTicketLink
from app.schemas.asset import LinkAssetRequest, LinkedAssetResponse


@router.post("/{ticket_id}/assets", status_code=201, response_model=dict)
async def link_asset_to_ticket(
    ticket_id: UUID,
    body: LinkAssetRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Link an asset to a ticket.  Idempotent — returns 201 even if already linked."""
    _require_tenant(current_user)

    if not _is_agent_or_above(current_user):
        raise AuthorizationError("Agents and above may link assets to tickets")

    repo = TicketRepository(db)
    await repo.get_or_404(ticket_id, current_user.tenant_id)  # type: ignore[arg-type]

    # Verify asset belongs to same tenant (IDOR guard)
    asset_result = await db.execute(
        select(AssetModel).where(
            AssetModel.id == body.asset_id,
            AssetModel.tenant_id == current_user.tenant_id,
            AssetModel.deleted_at.is_(None),
        )
    )
    if asset_result.scalar_one_or_none() is None:
        raise ResourceNotFoundError("assets", str(body.asset_id))

    # Idempotent: skip insert if link already exists
    existing = await db.execute(
        select(AssetTicketLink).where(
            AssetTicketLink.asset_id == body.asset_id,
            AssetTicketLink.ticket_id == ticket_id,
        )
    )
    if existing.scalar_one_or_none() is None:
        link = AssetTicketLink(
            asset_id=body.asset_id,
            ticket_id=ticket_id,
            linked_by=current_user.local_user_id,
        )
        db.add(link)
        await db.commit()

    return {"ticket_id": str(ticket_id), "asset_id": str(body.asset_id)}


@router.delete("/{ticket_id}/assets/{asset_id}", status_code=204)
async def unlink_asset_from_ticket(
    ticket_id: UUID,
    asset_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Remove the link between a ticket and an asset."""
    _require_tenant(current_user)

    if not _is_agent_or_above(current_user):
        raise AuthorizationError("Agents and above may unlink assets from tickets")

    repo = TicketRepository(db)
    await repo.get_or_404(ticket_id, current_user.tenant_id)  # type: ignore[arg-type]

    result = await db.execute(
        select(AssetTicketLink).where(
            AssetTicketLink.asset_id == asset_id,
            AssetTicketLink.ticket_id == ticket_id,
        )
    )
    link = result.scalar_one_or_none()
    if link is None:
        raise ResourceNotFoundError(
            "asset_ticket_links", f"{asset_id}/{ticket_id}"
        )

    await db.delete(link)
    await db.commit()


@router.get("/{ticket_id}/assets", response_model=list[LinkedAssetResponse])
async def list_assets_for_ticket(
    ticket_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[LinkedAssetResponse]:
    """List all assets linked to a ticket."""
    # Read scope only — platform users (no local_user_id) may view linked assets.
    tenant_id = _resolve_list_scope(current_user)

    repo = TicketRepository(db)
    await repo.get_or_404(ticket_id, tenant_id)

    result = await db.execute(
        select(AssetModel)
        .join(AssetTicketLink, AssetTicketLink.asset_id == AssetModel.id)
        .where(
            AssetTicketLink.ticket_id == ticket_id,
            AssetModel.tenant_id == current_user.tenant_id,
            AssetModel.deleted_at.is_(None),
        )
        .order_by(AssetTicketLink.linked_at.asc())
    )
    assets = list(result.scalars().all())
    return [
        LinkedAssetResponse(
            id=a.id,
            asset_tag=a.asset_tag,
            name=a.name,
            status=a.status.value,
        )
        for a in assets
    ]
