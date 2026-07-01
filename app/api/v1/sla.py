"""SLM API (Phase 7 / S7.1) — agreements, targets, underpinning, rules, coverage windows.

Prefix ``/sla``. Write = manager/admin; read = agent+ (spec A.3). Tenant isolation
via RLS + explicit tenant filter (same as admin.py). Editing an agreement bumps
its ``version`` (in-flight tickets freeze the version on their instance in S7.2 —
no retroactive breach).
"""

from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentUser, require_role
from app.database import get_db
from app.exceptions import AuthorizationError, ResourceNotFoundError, ValidationError
from app.models.sla import (
    CoverageWindow, SLAAgreement, SLAAgreementKind, SLAInstance, SLAMetric,
    SLARule, SLATarget, SLAUnderpinning,
)
from app.models.ticket import Ticket
from app.services import slm_service

router = APIRouter(prefix="/sla", tags=["sla"])

_WRITE_ROLES = ("admin", "tenant_admin", "manager")
_READ_ROLES = ("admin", "tenant_admin", "manager", "team_lead", "agent")


def _tenant(cu: CurrentUser) -> UUID:
    if cu.tenant_id is None:
        raise AuthorizationError("Tenant context required")
    return cu.tenant_id


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class CoverageWindowResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str
    timezone: str
    is_247: bool
    work_days: list[int]
    windows: Optional[list] = None
    holidays: Optional[list] = None
    compose_of: Optional[list[UUID]] = None


class CoverageWindowCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    timezone: str = "UTC"
    is_247: bool = False
    work_days: list[int] = Field(default_factory=lambda: [1, 2, 3, 4, 5])
    windows: Optional[list] = None
    holidays: Optional[list] = None
    compose_of: Optional[list[UUID]] = None


class CoverageWindowUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=255)
    timezone: Optional[str] = None
    is_247: Optional[bool] = None
    work_days: Optional[list[int]] = None
    windows: Optional[list] = None
    holidays: Optional[list] = None
    compose_of: Optional[list[UUID]] = None


class TargetResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    agreement_id: UUID
    metric: str
    duration_minutes: int
    coverage_window_id: Optional[UUID] = None
    applies_priority: Optional[str] = None
    applies_type: Optional[str] = None
    applies_category_id: Optional[UUID] = None
    start_event: Optional[str] = None
    stop_event: Optional[str] = None
    pause_conditions: Optional[list[str]] = None
    warn_thresholds_pct: list[int]


class TargetCreate(BaseModel):
    metric: SLAMetric
    duration_minutes: int = Field(gt=0)
    coverage_window_id: Optional[UUID] = None
    applies_priority: Optional[str] = None
    applies_type: Optional[str] = None
    applies_category_id: Optional[UUID] = None
    start_event: Optional[str] = None
    stop_event: Optional[str] = None
    pause_conditions: Optional[list[str]] = None
    warn_thresholds_pct: list[int] = Field(default_factory=lambda: [50, 75, 90])


class TargetUpdate(BaseModel):
    duration_minutes: Optional[int] = Field(default=None, gt=0)
    coverage_window_id: Optional[UUID] = None
    applies_priority: Optional[str] = None
    applies_type: Optional[str] = None
    applies_category_id: Optional[UUID] = None
    start_event: Optional[str] = None
    stop_event: Optional[str] = None
    pause_conditions: Optional[list[str]] = None
    warn_thresholds_pct: Optional[list[int]] = None


class AgreementResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str
    kind: str
    description: Optional[str] = None
    owner_team_id: Optional[UUID] = None
    vendor_id: Optional[UUID] = None
    version: int
    is_active: bool
    targets: list[TargetResponse] = []


class AgreementCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    kind: SLAAgreementKind = SLAAgreementKind.sla
    description: Optional[str] = None
    owner_team_id: Optional[UUID] = None
    vendor_id: Optional[UUID] = None


class AgreementUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=255)
    description: Optional[str] = None
    owner_team_id: Optional[UUID] = None
    vendor_id: Optional[UUID] = None
    is_active: Optional[bool] = None


class UnderpinningCreate(BaseModel):
    support_target_id: UUID


class UnderpinningResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    parent_target_id: UUID
    support_target_id: UUID


class RuleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    position: int
    conditions: dict
    agreement_id: UUID
    is_active: bool


class RuleCreate(BaseModel):
    agreement_id: UUID
    conditions: dict = Field(default_factory=dict)
    position: Optional[int] = None  # appended to the end if omitted
    is_active: bool = True


class RuleUpdate(BaseModel):
    conditions: Optional[dict] = None
    agreement_id: Optional[UUID] = None
    is_active: Optional[bool] = None


class RuleReorder(BaseModel):
    ordered_ids: list[UUID]


class MatchPreviewRequest(BaseModel):
    conditions: dict = Field(default_factory=dict)  # ticket attrs: type/priority/category_id/...


# ---------------------------------------------------------------------------
# Coverage windows
# ---------------------------------------------------------------------------


@router.get("/coverage-windows", response_model=list[CoverageWindowResponse])
async def list_coverage_windows(
    cu: CurrentUser = Depends(require_role(*_READ_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    tid = _tenant(cu)
    rows = (await db.execute(
        select(CoverageWindow).where(CoverageWindow.tenant_id == tid).order_by(CoverageWindow.name)
    )).scalars().all()
    return rows


@router.post("/coverage-windows", response_model=CoverageWindowResponse, status_code=201)
async def create_coverage_window(
    body: CoverageWindowCreate,
    cu: CurrentUser = Depends(require_role(*_WRITE_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    tid = _tenant(cu)
    cw = CoverageWindow(tenant_id=tid, **body.model_dump())
    db.add(cw)
    await db.commit()
    await db.refresh(cw)
    return cw


@router.patch("/coverage-windows/{cw_id}", response_model=CoverageWindowResponse)
async def update_coverage_window(
    cw_id: UUID,
    body: CoverageWindowUpdate,
    cu: CurrentUser = Depends(require_role(*_WRITE_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    tid = _tenant(cu)
    cw = (await db.execute(
        select(CoverageWindow).where(CoverageWindow.id == cw_id, CoverageWindow.tenant_id == tid)
    )).scalar_one_or_none()
    if cw is None:
        raise ResourceNotFoundError("Coverage window not found")
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(cw, k, v)
    await db.commit()
    await db.refresh(cw)
    return cw


@router.delete("/coverage-windows/{cw_id}", status_code=204)
async def delete_coverage_window(
    cw_id: UUID,
    cu: CurrentUser = Depends(require_role(*_WRITE_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    tid = _tenant(cu)
    cw = (await db.execute(
        select(CoverageWindow).where(CoverageWindow.id == cw_id, CoverageWindow.tenant_id == tid)
    )).scalar_one_or_none()
    if cw is None:
        raise ResourceNotFoundError("Coverage window not found")
    await db.delete(cw)
    await db.commit()


# ---------------------------------------------------------------------------
# Agreements (+ targets, versions)
# ---------------------------------------------------------------------------


async def _get_agreement(db: AsyncSession, tid: UUID, agreement_id: UUID) -> SLAAgreement:
    ag = (await db.execute(
        select(SLAAgreement).where(
            SLAAgreement.id == agreement_id,
            SLAAgreement.tenant_id == tid,
            SLAAgreement.deleted_at.is_(None),
        ).options(selectinload(SLAAgreement.targets))
    )).scalar_one_or_none()
    if ag is None:
        raise ResourceNotFoundError("Agreement not found")
    return ag


@router.get("/agreements", response_model=list[AgreementResponse])
async def list_agreements(
    kind: Optional[SLAAgreementKind] = None,
    cu: CurrentUser = Depends(require_role(*_READ_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    tid = _tenant(cu)
    q = select(SLAAgreement).where(
        SLAAgreement.tenant_id == tid, SLAAgreement.deleted_at.is_(None)
    )
    if kind is not None:
        q = q.where(SLAAgreement.kind == kind.value)
    q = q.options(selectinload(SLAAgreement.targets)).order_by(SLAAgreement.name)
    rows = (await db.execute(q)).scalars().unique().all()
    return rows


@router.post("/agreements", response_model=AgreementResponse, status_code=201)
async def create_agreement(
    body: AgreementCreate,
    cu: CurrentUser = Depends(require_role(*_WRITE_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    tid = _tenant(cu)
    data = body.model_dump()
    data["kind"] = data["kind"].value if hasattr(data["kind"], "value") else data["kind"]
    if data["kind"] == SLAAgreementKind.ola.value and not data.get("owner_team_id"):
        raise ValidationError("owner_team_id is required for an OLA")
    if data["kind"] == SLAAgreementKind.uc.value and not data.get("vendor_id"):
        raise ValidationError("vendor_id is required for a UC")
    ag = SLAAgreement(tenant_id=tid, **data)
    db.add(ag)
    await db.commit()
    await db.refresh(ag)
    return await _get_agreement(db, tid, ag.id)


@router.get("/agreements/{agreement_id}", response_model=AgreementResponse)
async def get_agreement(
    agreement_id: UUID,
    cu: CurrentUser = Depends(require_role(*_READ_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    return await _get_agreement(db, _tenant(cu), agreement_id)


@router.patch("/agreements/{agreement_id}", response_model=AgreementResponse)
async def update_agreement(
    agreement_id: UUID,
    body: AgreementUpdate,
    cu: CurrentUser = Depends(require_role(*_WRITE_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    tid = _tenant(cu)
    ag = await _get_agreement(db, tid, agreement_id)
    updates = body.model_dump(exclude_none=True)
    for k, v in updates.items():
        setattr(ag, k, v)
    # Editing a meaningful field bumps the version (freeze-on-assign in S7.2).
    if updates:
        ag.version = (ag.version or 1) + 1
    await db.commit()
    return await _get_agreement(db, tid, agreement_id)


@router.delete("/agreements/{agreement_id}", status_code=204)
async def delete_agreement(
    agreement_id: UUID,
    cu: CurrentUser = Depends(require_role(*_WRITE_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    tid = _tenant(cu)
    ag = await _get_agreement(db, tid, agreement_id)
    ag.deleted_at = func.now()  # soft delete; in-flight tickets keep their instance
    ag.is_active = False
    await db.commit()


@router.get("/agreements/{agreement_id}/versions")
async def get_agreement_versions(
    agreement_id: UUID,
    cu: CurrentUser = Depends(require_role(*_READ_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    ag = await _get_agreement(db, _tenant(cu), agreement_id)
    # S7.1 tracks the current version counter; full version history lands with
    # frozen runtime snapshots in S7.2.
    return {"current_version": ag.version, "versions": [ag.version]}


@router.post("/agreements/{agreement_id}/targets", response_model=TargetResponse, status_code=201)
async def add_target(
    agreement_id: UUID,
    body: TargetCreate,
    cu: CurrentUser = Depends(require_role(*_WRITE_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    tid = _tenant(cu)
    await _get_agreement(db, tid, agreement_id)  # tenant-scoped existence check
    data = body.model_dump()
    data["metric"] = data["metric"].value if hasattr(data["metric"], "value") else data["metric"]
    tgt = SLATarget(tenant_id=tid, agreement_id=agreement_id, **data)
    db.add(tgt)
    await db.commit()
    await db.refresh(tgt)
    return tgt


# ---------------------------------------------------------------------------
# Targets (patch/delete) + underpinning
# ---------------------------------------------------------------------------


async def _get_target(db: AsyncSession, tid: UUID, target_id: UUID) -> SLATarget:
    tgt = (await db.execute(
        select(SLATarget).where(SLATarget.id == target_id, SLATarget.tenant_id == tid)
    )).scalar_one_or_none()
    if tgt is None:
        raise ResourceNotFoundError("Target not found")
    return tgt


@router.patch("/targets/{target_id}", response_model=TargetResponse)
async def update_target(
    target_id: UUID,
    body: TargetUpdate,
    cu: CurrentUser = Depends(require_role(*_WRITE_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    tid = _tenant(cu)
    tgt = await _get_target(db, tid, target_id)
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(tgt, k, v)
    await db.commit()
    await db.refresh(tgt)
    return tgt


@router.delete("/targets/{target_id}", status_code=204)
async def delete_target(
    target_id: UUID,
    cu: CurrentUser = Depends(require_role(*_WRITE_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    tid = _tenant(cu)
    tgt = await _get_target(db, tid, target_id)
    await db.delete(tgt)
    await db.commit()


@router.get("/targets/{target_id}/underpinning", response_model=list[UnderpinningResponse])
async def list_underpinning(
    target_id: UUID,
    cu: CurrentUser = Depends(require_role(*_READ_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    tid = _tenant(cu)
    await _get_target(db, tid, target_id)
    rows = (await db.execute(
        select(SLAUnderpinning).where(SLAUnderpinning.parent_target_id == target_id)
    )).scalars().all()
    return rows


@router.post("/targets/{target_id}/underpinning", response_model=UnderpinningResponse, status_code=201)
async def add_underpinning(
    target_id: UUID,
    body: UnderpinningCreate,
    cu: CurrentUser = Depends(require_role(*_WRITE_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    tid = _tenant(cu)
    parent = await _get_target(db, tid, target_id)
    support = await _get_target(db, tid, body.support_target_id)  # tenant-scoped
    if parent.id == support.id:
        raise ValidationError("A target cannot underpin itself")
    link = SLAUnderpinning(parent_target_id=parent.id, support_target_id=support.id)
    db.add(link)
    await db.commit()
    await db.refresh(link)
    return link


@router.delete("/underpinning/{link_id}", status_code=204)
async def delete_underpinning(
    link_id: UUID,
    cu: CurrentUser = Depends(require_role(*_WRITE_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    tid = _tenant(cu)
    link = (await db.execute(
        select(SLAUnderpinning).join(
            SLATarget, SLATarget.id == SLAUnderpinning.parent_target_id
        ).where(SLAUnderpinning.id == link_id, SLATarget.tenant_id == tid)
    )).scalar_one_or_none()
    if link is None:
        raise ResourceNotFoundError("Underpinning link not found")
    await db.delete(link)
    await db.commit()


# ---------------------------------------------------------------------------
# Rules (+ reorder) + match preview
# ---------------------------------------------------------------------------


@router.get("/rules", response_model=list[RuleResponse])
async def list_rules(
    cu: CurrentUser = Depends(require_role(*_READ_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    tid = _tenant(cu)
    rows = (await db.execute(
        select(SLARule).where(SLARule.tenant_id == tid).order_by(SLARule.position)
    )).scalars().all()
    return rows


@router.post("/rules", response_model=RuleResponse, status_code=201)
async def create_rule(
    body: RuleCreate,
    cu: CurrentUser = Depends(require_role(*_WRITE_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    tid = _tenant(cu)
    await _get_agreement(db, tid, body.agreement_id)  # tenant-scoped existence check
    position = body.position
    if position is None:
        max_pos = (await db.execute(
            select(func.coalesce(func.max(SLARule.position), 0)).where(SLARule.tenant_id == tid)
        )).scalar_one()
        position = int(max_pos) + 1
    rule = SLARule(
        tenant_id=tid, position=position, conditions=body.conditions,
        agreement_id=body.agreement_id, is_active=body.is_active,
    )
    db.add(rule)
    await db.commit()
    await db.refresh(rule)
    return rule


@router.patch("/rules/reorder", response_model=list[RuleResponse])
async def reorder_rules(
    body: RuleReorder,
    cu: CurrentUser = Depends(require_role(*_WRITE_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    tid = _tenant(cu)
    rows = (await db.execute(
        select(SLARule).where(SLARule.tenant_id == tid)
    )).scalars().all()
    by_id = {r.id: r for r in rows}
    unknown = [rid for rid in body.ordered_ids if rid not in by_id]
    if unknown:
        raise ValidationError(f"Unknown rule id(s): {', '.join(str(u) for u in unknown)}")
    for pos, rid in enumerate(body.ordered_ids, start=1):
        by_id[rid].position = pos
    await db.commit()
    rows = (await db.execute(
        select(SLARule).where(SLARule.tenant_id == tid).order_by(SLARule.position)
    )).scalars().all()
    return rows


@router.patch("/rules/{rule_id}", response_model=RuleResponse)
async def update_rule(
    rule_id: UUID,
    body: RuleUpdate,
    cu: CurrentUser = Depends(require_role(*_WRITE_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    tid = _tenant(cu)
    rule = (await db.execute(
        select(SLARule).where(SLARule.id == rule_id, SLARule.tenant_id == tid)
    )).scalar_one_or_none()
    if rule is None:
        raise ResourceNotFoundError("Rule not found")
    updates = body.model_dump(exclude_none=True)
    if "agreement_id" in updates:
        await _get_agreement(db, tid, updates["agreement_id"])
    for k, v in updates.items():
        setattr(rule, k, v)
    await db.commit()
    await db.refresh(rule)
    return rule


@router.delete("/rules/{rule_id}", status_code=204)
async def delete_rule(
    rule_id: UUID,
    cu: CurrentUser = Depends(require_role(*_WRITE_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    tid = _tenant(cu)
    rule = (await db.execute(
        select(SLARule).where(SLARule.id == rule_id, SLARule.tenant_id == tid)
    )).scalar_one_or_none()
    if rule is None:
        raise ResourceNotFoundError("Rule not found")
    await db.delete(rule)
    await db.commit()


@router.post("/match-preview")
async def match_preview(
    body: MatchPreviewRequest,
    cu: CurrentUser = Depends(require_role(*_READ_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    """Given a ticket-like condition set, show which SLA would apply and why."""
    tid = _tenant(cu)
    rules = (await db.execute(
        select(SLARule).where(SLARule.tenant_id == tid).order_by(SLARule.position)
    )).scalars().all()
    return slm_service.match_explanation(list(rules), body.conditions)


# ---------------------------------------------------------------------------
# Reporting (Phase 7 / S7.3) — reads sla_instances
# ---------------------------------------------------------------------------


@router.get("/reports/at-risk")
async def report_at_risk(
    window_minutes: int = 60,
    cu: CurrentUser = Depends(require_role(*_READ_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    """Live board: running (not paused) clocks already overdue or due within the
    window, soonest first. Powers the "at-risk" operator view."""
    tid = _tenant(cu)
    cutoff = func.now() + func.make_interval(0, 0, 0, 0, 0, window_minutes, 0)
    rows = (await db.execute(
        select(
            SLAInstance, SLATarget.metric, Ticket.ticket_number,
            func.extract("epoch", SLAInstance.due_at - func.now()).label("remaining_sec"),
        )
        .join(SLATarget, SLATarget.id == SLAInstance.target_id)
        .join(Ticket, Ticket.id == SLAInstance.ticket_id)
        .where(
            SLAInstance.tenant_id == tid,
            SLAInstance.status == "running",
            SLAInstance.paused_at.is_(None),
            SLAInstance.due_at <= cutoff,
        )
        .order_by(SLAInstance.due_at.asc())
    )).all()
    return {"items": [
        {
            "instance_id": str(inst.id),
            "ticket_id": str(inst.ticket_id),
            "ticket_number": ticket_number,
            "metric": metric,
            "due_at": inst.due_at.isoformat() if inst.due_at else None,
            "remaining_seconds": float(remaining_sec) if remaining_sec is not None else None,
        }
        for inst, metric, ticket_number, remaining_sec in rows
    ]}


@router.get("/reports/breaches")
async def report_breaches(
    cu: CurrentUser = Depends(require_role(*_READ_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    """Breached instances with attributed party (team/vendor via OLA/UC chain)."""
    tid = _tenant(cu)
    rows = (await db.execute(
        select(SLAInstance, SLATarget.metric, Ticket.ticket_number)
        .join(SLATarget, SLATarget.id == SLAInstance.target_id)
        .join(Ticket, Ticket.id == SLAInstance.ticket_id)
        .where(SLAInstance.tenant_id == tid, SLAInstance.status == "breached")
        .order_by(SLAInstance.breached_at.desc())
    )).all()
    return {"items": [
        {
            "instance_id": str(inst.id),
            "ticket_id": str(inst.ticket_id),
            "ticket_number": ticket_number,
            "metric": metric,
            "breached_at": inst.breached_at.isoformat() if inst.breached_at else None,
            "attributed_party": inst.attributed_party,
        }
        for inst, metric, ticket_number in rows
    ]}


@router.get("/reports/overview")
async def report_overview(
    cu: CurrentUser = Depends(require_role(*_READ_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    """Instance counts by status + a simple compliance % (met / terminal)."""
    tid = _tenant(cu)
    rows = (await db.execute(
        select(SLAInstance.status, func.count())
        .where(SLAInstance.tenant_id == tid)
        .group_by(SLAInstance.status)
    )).all()
    counts = {status: int(n) for status, n in rows}
    met = counts.get("met", 0)
    breached = counts.get("breached", 0)
    terminal = met + breached
    compliance_pct = round(met / terminal * 100, 1) if terminal else None
    return {
        "counts": counts,
        "breached": breached,
        "at_risk": counts.get("running", 0),
        "compliance_pct": compliance_pct,
    }
