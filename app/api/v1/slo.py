"""SLO / SLI / error-budget API (Phase 9). Prefix /slo (+ /services/{id}/slo).

Reads: agent and up. Writes (sources, objectives, burn rules): manager/admin.
Platform users with no tenant selected see all tenants (fail-open, mirrors the
rest of the app); tenant users are scoped.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentUser, require_role
from app.database import get_db
from app.exceptions import AuthorizationError, ResourceNotFoundError, ValidationError
from app.models.slo import (
    BURN_DEFAULTS, SLISource, SLOBurnAlert, SLOMeasurement, SLOObjective,
)
from app.services import slo_service

router = APIRouter(prefix="/slo", tags=["slo"])
services_slo_router = APIRouter(prefix="/services", tags=["slo"])

_READ = ("admin", "tenant_admin", "manager", "team_lead", "agent")
_WRITE = ("admin", "tenant_admin", "manager")


def _tenant(cu: CurrentUser) -> UUID:
    if cu.tenant_id is None:
        raise AuthorizationError("Tenant context required")
    return cu.tenant_id


def _scope(cu: CurrentUser):
    if cu.tenant_id is not None:
        return cu.tenant_id
    if cu.tier == "platform":
        return None
    raise AuthorizationError("Tenant context required")


# ── schemas ───────────────────────────────────────────────────────────────
class SourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    type: str = "internal_metric"
    good_query: Optional[str] = None
    valid_query: Optional[str] = None
    config: Optional[dict] = None
    connection: Optional[dict] = None


class SourceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str
    type: str
    config: Optional[dict] = None
    is_active: bool


class ObjectiveCreate(BaseModel):
    service_id: UUID
    sli_source_id: UUID
    name: str = Field(min_length=1, max_length=255)
    objective_type: str = "availability"
    target_pct: float = Field(gt=0, le=100)
    window: str = "rolling_28d"


class ObjectiveUpdate(BaseModel):
    name: Optional[str] = None
    target_pct: Optional[float] = Field(default=None, gt=0, le=100)
    window: Optional[str] = None
    is_active: Optional[bool] = None


class ObjectiveResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    service_id: UUID
    sli_source_id: UUID
    name: str
    objective_type: str
    target_pct: float
    window: str
    is_active: bool


class MeasurementPush(BaseModel):
    good_count: int = Field(ge=0)
    total_count: int = Field(ge=0)
    bucket_start: Optional[datetime] = None


class BurnAlertCreate(BaseModel):
    kind: str = "fast_burn"
    short_window_min: Optional[int] = None
    long_window_min: Optional[int] = None
    burn_threshold: Optional[float] = None


# ── SLI sources ─────────────────────────────────────────────────────────────
@router.get("/sources", response_model=list[SourceResponse])
async def list_sources(cu: CurrentUser = Depends(require_role(*_READ)), db: AsyncSession = Depends(get_db)):
    tid = _scope(cu)
    q = select(SLISource)
    if tid is not None:
        q = q.where(SLISource.tenant_id == tid)
    return (await db.execute(q.order_by(SLISource.created_at.desc()))).scalars().all()


@router.post("/sources", response_model=SourceResponse, status_code=201)
async def create_source(body: SourceCreate, cu: CurrentUser = Depends(require_role(*_WRITE)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    # Guards against accidental duplicates (e.g. a double-submit from the UI)
    dup = (await db.execute(select(SLISource.id).where(
        SLISource.tenant_id == tid, SLISource.name == body.name))).scalar_one_or_none()
    if dup is not None:
        raise ValidationError(f"An SLI source named '{body.name}' already exists")
    src = SLISource(tenant_id=tid, **body.model_dump())
    db.add(src)
    await db.commit()
    await db.refresh(src)
    return src


@router.delete("/sources/{source_id}", status_code=204)
async def delete_source(source_id: UUID, cu: CurrentUser = Depends(require_role(*_WRITE)), db: AsyncSession = Depends(get_db)):
    src = (await db.execute(select(SLISource).where(SLISource.id == source_id, SLISource.tenant_id == _tenant(cu)))).scalar_one_or_none()
    if src is None:
        raise ResourceNotFoundError("SLI source", "requested")
    await db.delete(src)
    await db.commit()


# ── objectives ────────────────────────────────────────────────────────────
@router.get("/objectives", response_model=list[ObjectiveResponse])
async def list_objectives(cu: CurrentUser = Depends(require_role(*_READ)), db: AsyncSession = Depends(get_db)):
    tid = _scope(cu)
    q = select(SLOObjective)
    if tid is not None:
        q = q.where(SLOObjective.tenant_id == tid)
    return (await db.execute(q.order_by(SLOObjective.created_at.desc()))).scalars().all()


@router.post("/objectives", response_model=ObjectiveResponse, status_code=201)
async def create_objective(body: ObjectiveCreate, cu: CurrentUser = Depends(require_role(*_WRITE)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    dup = (await db.execute(select(SLOObjective.id).where(
        SLOObjective.tenant_id == tid, SLOObjective.name == body.name))).scalar_one_or_none()
    if dup is not None:
        raise ValidationError(f"An SLO named '{body.name}' already exists")
    slo = SLOObjective(tenant_id=tid, **body.model_dump())
    db.add(slo)
    await db.commit()
    await db.refresh(slo)
    return slo


@router.patch("/objectives/{slo_id}", response_model=ObjectiveResponse)
async def update_objective(slo_id: UUID, body: ObjectiveUpdate, cu: CurrentUser = Depends(require_role(*_WRITE)), db: AsyncSession = Depends(get_db)):
    slo = await _get_objective(db, slo_id, _tenant(cu))
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(slo, k, v)
    await db.commit()
    await db.refresh(slo)
    return slo


@router.delete("/objectives/{slo_id}", status_code=204)
async def delete_objective(slo_id: UUID, cu: CurrentUser = Depends(require_role(*_WRITE)), db: AsyncSession = Depends(get_db)):
    slo = await _get_objective(db, slo_id, _tenant(cu))
    await db.delete(slo)
    await db.commit()


@router.get("/objectives/{slo_id}/status")
async def objective_status(slo_id: UUID, cu: CurrentUser = Depends(require_role(*_READ)), db: AsyncSession = Depends(get_db)):
    slo = await _get_objective(db, slo_id, _scope(cu))
    status = await slo_service.objective_status(db, slo)
    return {"slo_id": str(slo.id), "name": slo.name, "service_id": str(slo.service_id), **status}


@router.get("/objectives/{slo_id}/history")
async def objective_history(slo_id: UUID, hours: int = Query(48, ge=1, le=2160),
                            cu: CurrentUser = Depends(require_role(*_READ)), db: AsyncSession = Depends(get_db)):
    slo = await _get_objective(db, slo_id, _scope(cu))
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = (await db.execute(
        select(SLOMeasurement).where(SLOMeasurement.slo_id == slo.id, SLOMeasurement.bucket_start >= since)
        .order_by(SLOMeasurement.bucket_start)
    )).scalars().all()
    return {"slo_id": str(slo.id), "buckets": [
        {"bucket_start": m.bucket_start.isoformat(), "good": m.good_count, "total": m.total_count,
         "sli_pct": round(m.good_count / m.total_count * 100, 3) if m.total_count else None}
        for m in rows
    ]}


@router.post("/{slo_id}/measurements", status_code=201)
async def push_measurement(slo_id: UUID, body: MeasurementPush,
                           cu: CurrentUser = Depends(require_role(*_WRITE)), db: AsyncSession = Depends(get_db)):
    """Push ingest for external systems (bearer-auth; HMAC webhook variant TBD)."""
    slo = await _get_objective(db, slo_id, _tenant(cu))
    bucket = body.bucket_start or slo_service._floor_bucket(datetime.now(timezone.utc))
    if bucket.tzinfo is None:
        bucket = bucket.replace(tzinfo=timezone.utc)
    await slo_service.record_measurement(db, slo.id, bucket, body.good_count, body.total_count)
    await slo_service.evaluate_burn_alerts(db, slo)
    await db.commit()
    return {"ok": True, "bucket_start": bucket.isoformat()}


# ── burn-alert rules ─────────────────────────────────────────────────────────
@router.get("/objectives/{slo_id}/burn-alerts")
async def list_burn_alerts(slo_id: UUID, cu: CurrentUser = Depends(require_role(*_READ)), db: AsyncSession = Depends(get_db)):
    slo = await _get_objective(db, slo_id, _scope(cu))
    rows = (await db.execute(select(SLOBurnAlert).where(SLOBurnAlert.slo_id == slo.id))).scalars().all()
    return [{"id": str(r.id), "kind": r.kind, "short_window_min": r.short_window_min,
             "long_window_min": r.long_window_min, "burn_threshold": float(r.burn_threshold),
             "state": r.state, "last_fired_at": r.last_fired_at.isoformat() if r.last_fired_at else None}
            for r in rows]


@router.post("/objectives/{slo_id}/burn-alerts", status_code=201)
async def create_burn_alert(slo_id: UUID, body: BurnAlertCreate,
                            cu: CurrentUser = Depends(require_role(*_WRITE)), db: AsyncSession = Depends(get_db)):
    slo = await _get_objective(db, slo_id, _tenant(cu))
    d = BURN_DEFAULTS.get(body.kind, BURN_DEFAULTS["fast_burn"])
    rule = SLOBurnAlert(
        tenant_id=slo.tenant_id, slo_id=slo.id, kind=body.kind,
        short_window_min=body.short_window_min or d["short_window_min"],
        long_window_min=body.long_window_min or d["long_window_min"],
        burn_threshold=body.burn_threshold or d["burn_threshold"],
    )
    db.add(rule)
    await db.commit()
    await db.refresh(rule)
    return {"id": str(rule.id), "kind": rule.kind, "state": rule.state}


@router.delete("/objectives/{slo_id}/burn-alerts/{alert_id}", status_code=204)
async def delete_burn_alert(slo_id: UUID, alert_id: UUID, cu: CurrentUser = Depends(require_role(*_WRITE)), db: AsyncSession = Depends(get_db)):
    await _get_objective(db, slo_id, _tenant(cu))
    rule = (await db.execute(select(SLOBurnAlert).where(SLOBurnAlert.id == alert_id, SLOBurnAlert.slo_id == slo_id))).scalar_one_or_none()
    if rule is None:
        raise ResourceNotFoundError("Burn alert", "requested")
    await db.delete(rule)
    await db.commit()


# ── reports + per-service ────────────────────────────────────────────────────
@router.get("/reports/error-budget")
async def error_budget_report(cu: CurrentUser = Depends(require_role(*_READ)), db: AsyncSession = Depends(get_db)):
    tid = _scope(cu)
    q = select(SLOObjective).where(SLOObjective.is_active.is_(True))
    if tid is not None:
        q = q.where(SLOObjective.tenant_id == tid)
    slos = (await db.execute(q)).scalars().all()
    out = []
    for slo in slos:
        st = await slo_service.objective_status(db, slo)
        out.append({"slo_id": str(slo.id), "name": slo.name, "service_id": str(slo.service_id),
                    "target_pct": float(slo.target_pct), "sli_pct": st["sli_pct"],
                    "budget_remaining_pct": st["budget_remaining_pct"], "burn_rate_1h": st["burn_rate_1h"],
                    "meeting": st["meeting"]})
    return {"objectives": out}


@services_slo_router.get("/{service_id}/slo")
async def service_slos(service_id: UUID, cu: CurrentUser = Depends(require_role(*_READ)), db: AsyncSession = Depends(get_db)):
    tid = _scope(cu)
    q = select(SLOObjective).where(SLOObjective.service_id == service_id)
    if tid is not None:
        q = q.where(SLOObjective.tenant_id == tid)
    slos = (await db.execute(q)).scalars().all()
    return [{"slo_id": str(s.id), "name": s.name, **(await slo_service.objective_status(db, s))} for s in slos]


# ── helpers ──────────────────────────────────────────────────────────────────
async def _get_objective(db: AsyncSession, slo_id: UUID, tid) -> SLOObjective:
    q = select(SLOObjective).where(SLOObjective.id == slo_id)
    if tid is not None:
        q = q.where(SLOObjective.tenant_id == tid)
    slo = (await db.execute(q)).scalar_one_or_none()
    if slo is None:
        raise ResourceNotFoundError("SLO objective", "requested")
    return slo
