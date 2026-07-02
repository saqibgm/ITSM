"""Status page, maintenance windows & workflows API (Phase 8 / S8.4).

Routers: ops_router (/status-page admin), public_router (/status — UNAUTH read +
subscribe), maint_router (/maintenance-windows), workflows_router (/workflows).
"""

from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentUser, require_role
from app.database import get_db
from app.exceptions import AuthorizationError, ResourceNotFoundError, ValidationError
from app.models.incident import Incident, IncidentStatusUpdate
from app.models.oncall import OnCallService
from app.models.ops import (
    MaintenanceWindow, StatusPageChannel, StatusPageConfig, StatusPageSubscription,
    Workflow, WorkflowRun,
)
from app.services import workflow_service

_WRITE = ("admin", "tenant_admin", "manager")
_READ = ("admin", "tenant_admin", "manager", "team_lead", "agent")
_LIVE_STATUSES = ("declared", "investigating", "identified", "monitoring")


def _tenant(cu: CurrentUser) -> UUID:
    if cu.tenant_id is None:
        raise AuthorizationError("Tenant context required")
    return cu.tenant_id


# ── Status page: admin config ─────────────────────────────────────────────────

ops_router = APIRouter(prefix="/status-page", tags=["ops"])


class StatusPageIn(BaseModel):
    slug: str = Field(min_length=2, max_length=80, pattern=r"^[a-z0-9-]+$")
    is_public: bool = False
    custom_domain: Optional[str] = None
    branding: Optional[dict] = None


class StatusPageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    slug: str
    is_public: bool
    custom_domain: Optional[str] = None
    branding: Optional[dict] = None


@ops_router.get("", response_model=Optional[StatusPageResponse])
async def get_config(cu: CurrentUser = Depends(require_role(*_READ)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    return (await db.execute(select(StatusPageConfig).where(StatusPageConfig.tenant_id == tid))).scalar_one_or_none()


@ops_router.put("", response_model=StatusPageResponse)
async def upsert_config(body: StatusPageIn, cu: CurrentUser = Depends(require_role(*_WRITE)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    cfg = (await db.execute(select(StatusPageConfig).where(StatusPageConfig.tenant_id == tid))).scalar_one_or_none()
    # slug must be globally unique
    clash = (await db.execute(select(StatusPageConfig).where(StatusPageConfig.slug == body.slug))).scalar_one_or_none()
    if clash is not None and (cfg is None or clash.id != cfg.id):
        raise ValidationError("slug already in use")
    if cfg is None:
        cfg = StatusPageConfig(tenant_id=tid, **body.model_dump())
        db.add(cfg)
    else:
        for k, v in body.model_dump().items():
            setattr(cfg, k, v)
    await db.commit(); await db.refresh(cfg)
    return cfg


@ops_router.get("/subscriptions")
async def list_subs(cu: CurrentUser = Depends(require_role(*_WRITE)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    rows = (await db.execute(select(StatusPageSubscription).where(StatusPageSubscription.tenant_id == tid))).scalars().all()
    return {"subscriptions": [{"id": str(s.id), "channel": s.channel, "value": s.value} for s in rows]}


# ── Status page: PUBLIC (unauthenticated) ─────────────────────────────────────

public_router = APIRouter(prefix="/status", tags=["ops-public"])


class SubscribeIn(BaseModel):
    channel: StatusPageChannel
    value: str = Field(min_length=3, max_length=255)
    service_ids: Optional[list[UUID]] = None


@public_router.get("/{slug}")
async def public_status(slug: str, db: AsyncSession = Depends(get_db)):
    """Unauthenticated public status page: service states + active incidents."""
    cfg = (await db.execute(
        select(StatusPageConfig).where(StatusPageConfig.slug == slug, StatusPageConfig.is_public.is_(True))
    )).scalar_one_or_none()
    if cfg is None:
        raise ResourceNotFoundError("Status page", "requested")
    tid = cfg.tenant_id
    services = (await db.execute(
        select(OnCallService).where(OnCallService.tenant_id == tid, OnCallService.is_active.is_(True))
    )).scalars().all()
    incidents = (await db.execute(
        select(Incident).where(Incident.tenant_id == tid, Incident.status.in_(_LIVE_STATUSES))
        .order_by(Incident.declared_at.desc())
    )).scalars().all()
    inc_out = []
    for inc in incidents:
        updates = (await db.execute(
            select(IncidentStatusUpdate).where(
                IncidentStatusUpdate.incident_id == inc.id, IncidentStatusUpdate.audience == "public"
            ).order_by(IncidentStatusUpdate.created_at.desc())
        )).scalars().all()
        inc_out.append({
            "incident_number": inc.incident_number, "title": inc.title, "status": inc.status,
            "declared_at": inc.declared_at.isoformat() if inc.declared_at else None,
            "updates": [{"body": u.body, "at": u.created_at.isoformat() if u.created_at else None} for u in updates],
        })
    overall = "major_outage" if any(s.current_state == "major_outage" for s in services) else (
        "degraded" if any(s.current_state not in ("operational",) for s in services) else "operational")
    return {
        "slug": slug, "branding": cfg.branding, "overall_status": overall,
        "services": [{"name": s.name, "state": s.current_state} for s in services],
        "active_incidents": inc_out,
    }


@public_router.post("/{slug}/subscribe", status_code=201)
async def subscribe(slug: str, body: SubscribeIn, db: AsyncSession = Depends(get_db)):
    cfg = (await db.execute(
        select(StatusPageConfig).where(StatusPageConfig.slug == slug, StatusPageConfig.is_public.is_(True))
    )).scalar_one_or_none()
    if cfg is None:
        raise ResourceNotFoundError("Status page", "requested")
    sub = StatusPageSubscription(tenant_id=cfg.tenant_id,
                                 channel=body.channel.value if hasattr(body.channel, "value") else body.channel,
                                 value=body.value, service_ids=body.service_ids)
    db.add(sub); await db.commit()
    return {"subscribed": True}


# ── Maintenance windows ───────────────────────────────────────────────────────

maint_router = APIRouter(prefix="/maintenance-windows", tags=["ops"])


class MaintenanceIn(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    service_ids: Optional[list[UUID]] = None
    start_at: datetime
    end_at: datetime
    suppress_alerts: bool = True


class MaintenanceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    title: str
    service_ids: Optional[list[UUID]] = None
    start_at: datetime
    end_at: datetime
    suppress_alerts: bool


@maint_router.get("", response_model=list[MaintenanceResponse])
async def list_maint(cu: CurrentUser = Depends(require_role(*_READ)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    return (await db.execute(
        select(MaintenanceWindow).where(MaintenanceWindow.tenant_id == tid).order_by(MaintenanceWindow.start_at.desc())
    )).scalars().all()


@maint_router.post("", response_model=MaintenanceResponse, status_code=201)
async def create_maint(body: MaintenanceIn, cu: CurrentUser = Depends(require_role(*_WRITE)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    if body.end_at <= body.start_at:
        raise ValidationError("end_at must be after start_at")
    w = MaintenanceWindow(tenant_id=tid, created_by=cu.local_user_id, **body.model_dump())
    db.add(w); await db.commit(); await db.refresh(w)
    return w


@maint_router.delete("/{window_id}", status_code=204)
async def delete_maint(window_id: UUID, cu: CurrentUser = Depends(require_role(*_WRITE)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    w = (await db.execute(select(MaintenanceWindow).where(MaintenanceWindow.id == window_id, MaintenanceWindow.tenant_id == tid))).scalar_one_or_none()
    if w is None:
        raise ResourceNotFoundError("Maintenance window", "requested")
    await db.delete(w); await db.commit()


# ── Workflows ─────────────────────────────────────────────────────────────────

workflows_router = APIRouter(prefix="/workflows", tags=["ops"])


class WorkflowIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    trigger: str = Field(min_length=1, max_length=40)
    conditions: dict = Field(default_factory=dict)
    actions: list = Field(default_factory=list)
    is_active: bool = True


class WorkflowResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str
    trigger: str
    conditions: dict
    actions: list
    is_active: bool
    version: int


class DryRunIn(BaseModel):
    context: dict = Field(default_factory=dict)


async def _get_wf(db, tid, wid) -> Workflow:
    wf = (await db.execute(select(Workflow).where(Workflow.id == wid, Workflow.tenant_id == tid))).scalar_one_or_none()
    if wf is None:
        raise ResourceNotFoundError("Workflow", "requested")
    return wf


@workflows_router.get("", response_model=list[WorkflowResponse])
async def list_workflows(cu: CurrentUser = Depends(require_role(*_READ)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    return (await db.execute(select(Workflow).where(Workflow.tenant_id == tid).order_by(Workflow.name))).scalars().all()


@workflows_router.post("", response_model=WorkflowResponse, status_code=201)
async def create_workflow(body: WorkflowIn, cu: CurrentUser = Depends(require_role(*_WRITE)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    wf = Workflow(tenant_id=tid, **body.model_dump())
    db.add(wf); await db.commit(); await db.refresh(wf)
    return wf


@workflows_router.patch("/{workflow_id}", response_model=WorkflowResponse)
async def update_workflow(workflow_id: UUID, body: WorkflowIn, cu: CurrentUser = Depends(require_role(*_WRITE)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    wf = await _get_wf(db, tid, workflow_id)
    for k, v in body.model_dump().items():
        setattr(wf, k, v)
    wf.version = (wf.version or 1) + 1
    await db.commit(); await db.refresh(wf)
    return wf


@workflows_router.delete("/{workflow_id}", status_code=204)
async def delete_workflow(workflow_id: UUID, cu: CurrentUser = Depends(require_role(*_WRITE)), db: AsyncSession = Depends(get_db)):
    wf = await _get_wf(db, _tenant(cu), workflow_id)
    await db.delete(wf); await db.commit()


@workflows_router.post("/{workflow_id}/dry-run")
async def dry_run(workflow_id: UUID, body: DryRunIn, cu: CurrentUser = Depends(require_role(*_READ)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    wf = await _get_wf(db, tid, workflow_id)
    res = await workflow_service.run_workflows(db, tid, wf.trigger, body.context, dry_run=True)
    # dry-run applies nothing; filter to this workflow's summary
    mine = [r for r in res if r["workflow_id"] == str(workflow_id)]
    return {"workflow_id": str(workflow_id), "matched": bool(mine), "result": mine[0] if mine else None}


@workflows_router.get("/{workflow_id}/runs")
async def workflow_runs(workflow_id: UUID, cu: CurrentUser = Depends(require_role(*_READ)), db: AsyncSession = Depends(get_db)):
    await _get_wf(db, _tenant(cu), workflow_id)
    rows = (await db.execute(
        select(WorkflowRun).where(WorkflowRun.workflow_id == workflow_id).order_by(WorkflowRun.at.desc()).limit(100)
    )).scalars().all()
    return {"runs": [{"id": str(r.id), "status": r.status, "result": r.result,
                      "at": r.at.isoformat() if r.at else None} for r in rows]}
