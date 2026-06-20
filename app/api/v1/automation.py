"""Automation Rules Engine API (S6A).

Routes:
  GET    /automation/rules              list rules (paginated); agent+
  POST   /automation/rules              create rule; tenant_admin
  GET    /automation/rules/{id}         get rule detail
  PATCH  /automation/rules/{id}         update rule; tenant_admin
  DELETE /automation/rules/{id}         delete rule; tenant_admin
  GET    /automation/rules/{id}/logs    execution logs (last 100, paginated)
  POST   /automation/rules/{id}/test    dry-run — returns condition pass/fail

Pagination style: cursor (consistent with the rest of the codebase).
"""

import base64
from datetime import datetime
from typing import Any, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentUser, get_current_user, require_role
from app.database import get_db
from app.exceptions import AuthorizationError, ResourceNotFoundError, ValidationError
from app.models.automation import AutomationLog, AutomationRule
from app.schemas.common import PaginatedResponse

router = APIRouter(prefix="/automation", tags=["automation"])

# Valid trigger values
_VALID_TRIGGERS = frozenset(
    {
        "ticket_created",
        "ticket_updated",
        "ticket_sla_breached",
        "asset_status_changed",
        "ticket_pending_approval",
    }
)

_AGENT_ROLES = ("admin", "agent", "team_lead", "manager")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_tenant(current_user: CurrentUser) -> None:
    if current_user.tenant_id is None:
        raise AuthorizationError("Tenant context required")


def _require_admin(current_user: CurrentUser) -> None:
    _require_tenant(current_user)
    if not set(current_user.roles) & {"admin"}:
        raise AuthorizationError("admin role is required")


def _encode_cursor(value: str) -> str:
    return base64.b64encode(value.encode()).decode()


def _decode_cursor(cursor: str) -> str:
    try:
        return base64.b64decode(cursor.encode()).decode()
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class CreateAutomationRuleRequest(BaseModel):
    name: str = Field(..., max_length=255)
    trigger: str
    conditions: list[dict] = Field(default_factory=list)
    actions: list[dict] = Field(default_factory=list)
    description: Optional[str] = None
    run_order: int = 0
    is_active: bool = True


class UpdateAutomationRuleRequest(BaseModel):
    name: Optional[str] = Field(default=None, max_length=255)
    description: Optional[str] = None
    trigger: Optional[str] = None
    conditions: Optional[list[dict]] = None
    actions: Optional[list[dict]] = None
    is_active: Optional[bool] = None
    run_order: Optional[int] = None


class AutomationRuleResponse(BaseModel):
    id: UUID
    tenant_id: UUID
    name: str
    description: Optional[str]
    trigger: str
    conditions: list
    actions: list
    is_active: bool
    run_order: int
    trigger_count: int
    last_triggered_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AutomationLogResponse(BaseModel):
    id: UUID
    rule_id: UUID
    tenant_id: UUID
    entity_type: str
    entity_id: UUID
    actions_executed: list
    error: Optional[str]
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class TestRuleRequest(BaseModel):
    entity_type: Literal["ticket", "asset"]
    entity_id: UUID


class TestRuleResponse(BaseModel):
    conditions_pass: bool
    condition_results: list[dict]
    actions_would_execute: list[dict]


# ---------------------------------------------------------------------------
# GET /automation/rules
# ---------------------------------------------------------------------------


@router.get("/rules", response_model=PaginatedResponse[AutomationRuleResponse])
async def list_rules(
    cursor: Optional[str] = Query(default=None),
    limit: int = Query(default=25, ge=1, le=100),
    trigger: Optional[str] = Query(default=None),
    is_active: Optional[bool] = Query(default=None),
    current_user: CurrentUser = Depends(require_role(*_AGENT_ROLES)),
    db: AsyncSession = Depends(get_db),
) -> PaginatedResponse[AutomationRuleResponse]:
    _require_tenant(current_user)
    tenant_id = current_user.tenant_id

    query = select(AutomationRule).where(AutomationRule.tenant_id == tenant_id)
    if trigger is not None:
        query = query.where(AutomationRule.trigger == trigger)
    if is_active is not None:
        query = query.where(AutomationRule.is_active == is_active)

    if cursor:
        decoded = _decode_cursor(cursor)
        if decoded:
            query = query.where(AutomationRule.id > decoded)

    query = (
        query.order_by(AutomationRule.run_order.asc(), AutomationRule.id.asc())
        .limit(limit + 1)
    )

    result = await db.execute(query)
    rules = list(result.scalars().all())

    next_cursor: Optional[str] = None
    if len(rules) > limit:
        rules = rules[:limit]
        next_cursor = _encode_cursor(str(rules[-1].id))

    return PaginatedResponse(
        items=[AutomationRuleResponse.model_validate(r) for r in rules],
        next_cursor=next_cursor,
        total=None,
    )


# ---------------------------------------------------------------------------
# POST /automation/rules
# ---------------------------------------------------------------------------


@router.post("/rules", response_model=AutomationRuleResponse, status_code=201)
async def create_rule(
    body: CreateAutomationRuleRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AutomationRuleResponse:
    _require_admin(current_user)

    if body.trigger not in _VALID_TRIGGERS:
        raise ValidationError(
            f"trigger must be one of: {', '.join(sorted(_VALID_TRIGGERS))}"
        )

    rule = AutomationRule(
        tenant_id=current_user.tenant_id,
        name=body.name,
        description=body.description,
        trigger=body.trigger,
        conditions=body.conditions,
        actions=body.actions,
        is_active=body.is_active,
        run_order=body.run_order,
    )
    db.add(rule)
    await db.flush()
    await db.refresh(rule)
    return AutomationRuleResponse.model_validate(rule)


# ---------------------------------------------------------------------------
# GET /automation/rules/{rule_id}
# ---------------------------------------------------------------------------


@router.get("/rules/{rule_id}", response_model=AutomationRuleResponse)
async def get_rule(
    rule_id: UUID,
    current_user: CurrentUser = Depends(require_role(*_AGENT_ROLES)),
    db: AsyncSession = Depends(get_db),
) -> AutomationRuleResponse:
    _require_tenant(current_user)
    rule = await _get_rule_or_404(db, rule_id, current_user.tenant_id)
    return AutomationRuleResponse.model_validate(rule)


# ---------------------------------------------------------------------------
# PATCH /automation/rules/{rule_id}
# ---------------------------------------------------------------------------


@router.patch("/rules/{rule_id}", response_model=AutomationRuleResponse)
async def update_rule(
    rule_id: UUID,
    body: UpdateAutomationRuleRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AutomationRuleResponse:
    _require_admin(current_user)
    rule = await _get_rule_or_404(db, rule_id, current_user.tenant_id)

    if body.trigger is not None and body.trigger not in _VALID_TRIGGERS:
        raise ValidationError(
            f"trigger must be one of: {', '.join(sorted(_VALID_TRIGGERS))}"
        )

    if body.name is not None:
        rule.name = body.name
    if body.description is not None:
        rule.description = body.description
    if body.trigger is not None:
        rule.trigger = body.trigger
    if body.conditions is not None:
        rule.conditions = body.conditions
    if body.actions is not None:
        rule.actions = body.actions
    if body.is_active is not None:
        rule.is_active = body.is_active
    if body.run_order is not None:
        rule.run_order = body.run_order

    db.add(rule)
    await db.flush()
    await db.refresh(rule)
    return AutomationRuleResponse.model_validate(rule)


# ---------------------------------------------------------------------------
# DELETE /automation/rules/{rule_id}
# ---------------------------------------------------------------------------


@router.delete("/rules/{rule_id}", status_code=204)
async def delete_rule(
    rule_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    _require_admin(current_user)
    rule = await _get_rule_or_404(db, rule_id, current_user.tenant_id)
    await db.delete(rule)
    await db.flush()


# ---------------------------------------------------------------------------
# GET /automation/rules/{rule_id}/logs
# ---------------------------------------------------------------------------


@router.get("/rules/{rule_id}/logs", response_model=PaginatedResponse[AutomationLogResponse])
async def list_rule_logs(
    rule_id: UUID,
    cursor: Optional[str] = Query(default=None),
    limit: int = Query(default=25, ge=1, le=100),
    current_user: CurrentUser = Depends(require_role(*_AGENT_ROLES)),
    db: AsyncSession = Depends(get_db),
) -> PaginatedResponse[AutomationLogResponse]:
    _require_tenant(current_user)
    rule = await _get_rule_or_404(db, rule_id, current_user.tenant_id)

    effective_limit = min(limit, 100)

    query = select(AutomationLog).where(AutomationLog.rule_id == rule.id)

    if cursor:
        decoded = _decode_cursor(cursor)
        if decoded:
            query = query.where(AutomationLog.id < decoded)

    query = query.order_by(AutomationLog.created_at.desc()).limit(effective_limit + 1)

    result = await db.execute(query)
    logs = list(result.scalars().all())

    next_cursor: Optional[str] = None
    if len(logs) > effective_limit:
        logs = logs[:effective_limit]
        next_cursor = _encode_cursor(str(logs[-1].id))

    return PaginatedResponse(
        items=[AutomationLogResponse.model_validate(log) for log in logs],
        next_cursor=next_cursor,
        total=None,
    )


# ---------------------------------------------------------------------------
# POST /automation/rules/{rule_id}/test
# ---------------------------------------------------------------------------


@router.post("/rules/{rule_id}/test", response_model=TestRuleResponse)
async def test_rule(
    rule_id: UUID,
    body: TestRuleRequest,
    current_user: CurrentUser = Depends(require_role(*_AGENT_ROLES)),
    db: AsyncSession = Depends(get_db),
) -> TestRuleResponse:
    _require_tenant(current_user)
    rule = await _get_rule_or_404(db, rule_id, current_user.tenant_id)

    from app.services.automation_service import automation_service

    if body.entity_type == "ticket":
        from app.models.ticket import Ticket

        result = await db.execute(
            select(Ticket).where(
                Ticket.id == body.entity_id,
                Ticket.tenant_id == current_user.tenant_id,
            )
        )
        ticket = result.scalar_one_or_none()
        if ticket is None:
            raise ResourceNotFoundError("ticket", str(body.entity_id))

        test_result = await automation_service.test_rule_against_ticket(db, rule, ticket)

    elif body.entity_type == "asset":
        from app.models.asset import Asset

        result = await db.execute(
            select(Asset).where(
                Asset.id == body.entity_id,
                Asset.tenant_id == current_user.tenant_id,
            )
        )
        asset = result.scalar_one_or_none()
        if asset is None:
            raise ResourceNotFoundError("asset", str(body.entity_id))

        test_result = await automation_service.test_rule_against_asset(db, rule, asset)

    else:
        raise ValidationError("entity_type must be 'ticket' or 'asset'")

    return TestRuleResponse(**test_result)


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------


async def _get_rule_or_404(
    db: AsyncSession, rule_id: UUID, tenant_id: UUID
) -> AutomationRule:
    result = await db.execute(
        select(AutomationRule).where(
            AutomationRule.id == rule_id,
            AutomationRule.tenant_id == tenant_id,
        )
    )
    rule = result.scalar_one_or_none()
    if rule is None:
        raise ResourceNotFoundError("automation_rule", str(rule_id))
    return rule
