"""On-call & services API (Phase 8 / S8.1).

Routers:
  services_router  — /api/v1/services         (service catalog)
  router           — /api/v1/on-call/*         (severities, schedules, layers,
                                                overrides, who-is-on-call, preview)
Write = manager/admin; read = agent+.
"""

from datetime import datetime, time, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentUser, require_role
from app.database import get_db
from app.exceptions import AuthorizationError, ResourceNotFoundError, ValidationError
from app.models.oncall import (
    OnCallService, RotationType, Schedule, ScheduleLayer, ScheduleOverride, SeverityLevel,
)
from app.services import oncall_service

_WRITE = ("admin", "tenant_admin", "manager")
_READ = ("admin", "tenant_admin", "manager", "team_lead", "agent")


def _tenant(cu: CurrentUser) -> UUID:
    if cu.tenant_id is None:
        raise AuthorizationError("Tenant context required")
    return cu.tenant_id


# ── Services ────────────────────────────────────────────────────────────────

services_router = APIRouter(prefix="/services", tags=["on-call"])


class ServiceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str
    description: Optional[str] = None
    asset_id: Optional[UUID] = None
    escalation_policy_id: Optional[UUID] = None
    current_state: str
    is_active: bool


class ServiceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: Optional[str] = None
    asset_id: Optional[UUID] = None
    escalation_policy_id: Optional[UUID] = None


class ServiceUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=255)
    description: Optional[str] = None
    asset_id: Optional[UUID] = None
    escalation_policy_id: Optional[UUID] = None
    current_state: Optional[str] = None
    is_active: Optional[bool] = None


@services_router.get("", response_model=list[ServiceResponse])
async def list_services(cu: CurrentUser = Depends(require_role(*_READ)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    return (await db.execute(
        select(OnCallService).where(OnCallService.tenant_id == tid).order_by(OnCallService.name)
    )).scalars().all()


@services_router.post("", response_model=ServiceResponse, status_code=201)
async def create_service(body: ServiceCreate, cu: CurrentUser = Depends(require_role(*_WRITE)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    svc = OnCallService(tenant_id=tid, **body.model_dump())
    db.add(svc); await db.commit(); await db.refresh(svc)
    return svc


async def _get_service(db, tid, sid) -> OnCallService:
    svc = (await db.execute(select(OnCallService).where(OnCallService.id == sid, OnCallService.tenant_id == tid))).scalar_one_or_none()
    if svc is None:
        raise ResourceNotFoundError("Service", "requested")
    return svc


@services_router.get("/{service_id}", response_model=ServiceResponse)
async def get_service(service_id: UUID, cu: CurrentUser = Depends(require_role(*_READ)), db: AsyncSession = Depends(get_db)):
    return await _get_service(db, _tenant(cu), service_id)


@services_router.patch("/{service_id}", response_model=ServiceResponse)
async def update_service(service_id: UUID, body: ServiceUpdate, cu: CurrentUser = Depends(require_role(*_WRITE)), db: AsyncSession = Depends(get_db)):
    svc = await _get_service(db, _tenant(cu), service_id)
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(svc, k, v)
    await db.commit(); await db.refresh(svc)
    return svc


@services_router.delete("/{service_id}", status_code=204)
async def delete_service(service_id: UUID, cu: CurrentUser = Depends(require_role(*_WRITE)), db: AsyncSession = Depends(get_db)):
    svc = await _get_service(db, _tenant(cu), service_id)
    await db.delete(svc); await db.commit()


# ── On-call: severities, schedules, layers, overrides ─────────────────────────

router = APIRouter(prefix="/on-call", tags=["on-call"])


class SeverityResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str
    rank: int
    auto_page: bool
    required_roles: Optional[list[str]] = None
    default_agreement_id: Optional[UUID] = None


class SeverityCreate(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    rank: int = Field(ge=1)
    auto_page: bool = False
    required_roles: Optional[list[str]] = None
    default_agreement_id: Optional[UUID] = None


class LayerResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    layer_rank: int
    participants: list[UUID]


class ScheduleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str
    team_id: Optional[UUID] = None
    timezone: str
    rotation_type: str
    rotation_length_hours: Optional[int] = None
    handoff_time: Optional[time] = None
    start_at: Optional[datetime] = None
    is_active: bool
    layers: list[LayerResponse] = []


class ScheduleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    team_id: Optional[UUID] = None
    timezone: str = "UTC"
    rotation_type: RotationType = RotationType.weekly
    rotation_length_hours: Optional[int] = Field(default=None, gt=0)
    handoff_time: Optional[time] = None
    start_at: Optional[datetime] = None


class ScheduleUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=255)
    team_id: Optional[UUID] = None
    timezone: Optional[str] = None
    rotation_type: Optional[RotationType] = None
    rotation_length_hours: Optional[int] = Field(default=None, gt=0)
    handoff_time: Optional[time] = None
    start_at: Optional[datetime] = None
    is_active: Optional[bool] = None


class LayerCreate(BaseModel):
    layer_rank: int = Field(ge=1)
    participants: list[UUID] = Field(default_factory=list)


class LayerUpdate(BaseModel):
    layer_rank: Optional[int] = Field(default=None, ge=1)
    participants: Optional[list[UUID]] = None


class OverrideResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    schedule_id: UUID
    user_id: UUID
    start_at: datetime
    end_at: datetime
    origin: str


class OverrideCreate(BaseModel):
    schedule_id: UUID
    user_id: UUID
    start_at: datetime
    end_at: datetime


# severities
@router.get("/severities", response_model=list[SeverityResponse])
async def list_severities(cu: CurrentUser = Depends(require_role(*_READ)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    return (await db.execute(select(SeverityLevel).where(SeverityLevel.tenant_id == tid).order_by(SeverityLevel.rank))).scalars().all()


@router.post("/severities", response_model=SeverityResponse, status_code=201)
async def create_severity(body: SeverityCreate, cu: CurrentUser = Depends(require_role(*_WRITE)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    sev = SeverityLevel(tenant_id=tid, **body.model_dump())
    db.add(sev); await db.commit(); await db.refresh(sev)
    return sev


@router.delete("/severities/{severity_id}", status_code=204)
async def delete_severity(severity_id: UUID, cu: CurrentUser = Depends(require_role(*_WRITE)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    sev = (await db.execute(select(SeverityLevel).where(SeverityLevel.id == severity_id, SeverityLevel.tenant_id == tid))).scalar_one_or_none()
    if sev is None:
        raise ResourceNotFoundError("Severity", "requested")
    await db.delete(sev); await db.commit()


# schedules
async def _get_schedule(db, tid, sid) -> Schedule:
    sch = (await db.execute(
        select(Schedule).where(Schedule.id == sid, Schedule.tenant_id == tid)
        .options(selectinload(Schedule.layers))
    )).scalar_one_or_none()
    if sch is None:
        raise ResourceNotFoundError("Schedule", "requested")
    return sch


@router.get("/schedules", response_model=list[ScheduleResponse])
async def list_schedules(cu: CurrentUser = Depends(require_role(*_READ)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    return (await db.execute(
        select(Schedule).where(Schedule.tenant_id == tid)
        .options(selectinload(Schedule.layers)).order_by(Schedule.name)
    )).scalars().unique().all()


@router.post("/schedules", response_model=ScheduleResponse, status_code=201)
async def create_schedule(body: ScheduleCreate, cu: CurrentUser = Depends(require_role(*_WRITE)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    data = body.model_dump()
    data["rotation_type"] = data["rotation_type"].value if hasattr(data["rotation_type"], "value") else data["rotation_type"]
    sch = Schedule(tenant_id=tid, **data)
    db.add(sch); await db.commit()
    return await _get_schedule(db, tid, sch.id)


@router.get("/schedules/{schedule_id}", response_model=ScheduleResponse)
async def get_schedule(schedule_id: UUID, cu: CurrentUser = Depends(require_role(*_READ)), db: AsyncSession = Depends(get_db)):
    return await _get_schedule(db, _tenant(cu), schedule_id)


@router.patch("/schedules/{schedule_id}", response_model=ScheduleResponse)
async def update_schedule(schedule_id: UUID, body: ScheduleUpdate, cu: CurrentUser = Depends(require_role(*_WRITE)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    sch = await _get_schedule(db, tid, schedule_id)
    upd = body.model_dump(exclude_none=True)
    if "rotation_type" in upd and hasattr(upd["rotation_type"], "value"):
        upd["rotation_type"] = upd["rotation_type"].value
    for k, v in upd.items():
        setattr(sch, k, v)
    await db.commit()
    return await _get_schedule(db, tid, schedule_id)


@router.delete("/schedules/{schedule_id}", status_code=204)
async def delete_schedule(schedule_id: UUID, cu: CurrentUser = Depends(require_role(*_WRITE)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    sch = await _get_schedule(db, tid, schedule_id)
    await db.delete(sch); await db.commit()


@router.post("/schedules/{schedule_id}/layers", response_model=LayerResponse, status_code=201)
async def add_layer(schedule_id: UUID, body: LayerCreate, cu: CurrentUser = Depends(require_role(*_WRITE)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    await _get_schedule(db, tid, schedule_id)
    layer = ScheduleLayer(schedule_id=schedule_id, layer_rank=body.layer_rank, participants=body.participants)
    db.add(layer); await db.commit(); await db.refresh(layer)
    return layer


async def _get_layer(db, tid, schedule_id, layer_id) -> ScheduleLayer:
    await _get_schedule(db, tid, schedule_id)  # tenant-scoped existence
    layer = (await db.execute(
        select(ScheduleLayer).where(ScheduleLayer.id == layer_id, ScheduleLayer.schedule_id == schedule_id)
    )).scalar_one_or_none()
    if layer is None:
        raise ResourceNotFoundError("Layer", "requested")
    return layer


@router.patch("/schedules/{schedule_id}/layers/{layer_id}", response_model=LayerResponse)
async def update_layer(schedule_id: UUID, layer_id: UUID, body: LayerUpdate, cu: CurrentUser = Depends(require_role(*_WRITE)), db: AsyncSession = Depends(get_db)):
    layer = await _get_layer(db, _tenant(cu), schedule_id, layer_id)
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(layer, k, v)
    await db.commit(); await db.refresh(layer)
    return layer


@router.delete("/schedules/{schedule_id}/layers/{layer_id}", status_code=204)
async def delete_layer(schedule_id: UUID, layer_id: UUID, cu: CurrentUser = Depends(require_role(*_WRITE)), db: AsyncSession = Depends(get_db)):
    layer = await _get_layer(db, _tenant(cu), schedule_id, layer_id)
    await db.delete(layer); await db.commit()


@router.get("/schedules/{schedule_id}/preview")
async def preview_schedule(schedule_id: UUID, at: Optional[datetime] = None,
                           cu: CurrentUser = Depends(require_role(*_READ)), db: AsyncSession = Depends(get_db)):
    sch = await _get_schedule(db, _tenant(cu), schedule_id)
    return await oncall_service.who_is_on_call(db, sch, at)


@router.get("/who-is-on-call")
async def who_is_on_call(schedule_id: Optional[UUID] = None, team_id: Optional[UUID] = None,
                         at: Optional[datetime] = None,
                         cu: CurrentUser = Depends(require_role(*_READ)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    if schedule_id is None and team_id is None:
        raise ValidationError("schedule_id or team_id is required")
    q = select(Schedule).where(Schedule.tenant_id == tid, Schedule.is_active.is_(True))
    if schedule_id is not None:
        q = q.where(Schedule.id == schedule_id)
    if team_id is not None:
        q = q.where(Schedule.team_id == team_id)
    schedules = (await db.execute(q)).scalars().all()
    return {"results": [await oncall_service.who_is_on_call(db, s, at) for s in schedules]}


# overrides
@router.get("/overrides", response_model=list[OverrideResponse])
async def list_overrides(schedule_id: UUID = Query(...), cu: CurrentUser = Depends(require_role(*_READ)), db: AsyncSession = Depends(get_db)):
    await _get_schedule(db, _tenant(cu), schedule_id)
    return (await db.execute(
        select(ScheduleOverride).where(ScheduleOverride.schedule_id == schedule_id).order_by(ScheduleOverride.start_at)
    )).scalars().all()


@router.post("/overrides", response_model=OverrideResponse, status_code=201)
async def create_override(body: OverrideCreate, cu: CurrentUser = Depends(require_role(*_WRITE)), db: AsyncSession = Depends(get_db)):
    await _get_schedule(db, _tenant(cu), body.schedule_id)
    if body.end_at <= body.start_at:
        raise ValidationError("end_at must be after start_at")
    ov = ScheduleOverride(schedule_id=body.schedule_id, user_id=body.user_id,
                          start_at=body.start_at, end_at=body.end_at,
                          created_by=cu.local_user_id)
    db.add(ov); await db.commit(); await db.refresh(ov)
    return ov


@router.delete("/overrides/{override_id}", status_code=204)
async def delete_override(override_id: UUID, cu: CurrentUser = Depends(require_role(*_WRITE)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    ov = (await db.execute(
        select(ScheduleOverride).join(Schedule, Schedule.id == ScheduleOverride.schedule_id)
        .where(ScheduleOverride.id == override_id, Schedule.tenant_id == tid)
    )).scalar_one_or_none()
    if ov is None:
        raise ResourceNotFoundError("Override", "requested")
    await db.delete(ov); await db.commit()
