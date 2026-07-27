"""Incident API (Phase 8 / S8.3). Prefix /incidents. Write=agent+, admin ops as noted."""

from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentUser, require_role
from app.database import get_db
from app.exceptions import AuthorizationError, ResourceNotFoundError
from app.models.alerting import Alert
from app.models.incident import (
    Audience, Incident, IncidentRole, IncidentRoleType, IncidentStatus,
    IncidentStatusUpdate, IncidentTimeline,
)
from app.services import incident_service

router = APIRouter(prefix="/incidents", tags=["incidents"])

_READ = ("admin", "tenant_admin", "manager", "team_lead", "agent")
_WRITE = _READ  # responders (agent+) can declare/drive incidents


def _tenant(cu: CurrentUser) -> UUID:
    if cu.tenant_id is None:
        raise AuthorizationError("Tenant context required")
    return cu.tenant_id


def _scope(cu: CurrentUser):
    """tenant_id, or None for a platform user with no tenant selected (show all)."""
    if cu.tenant_id is not None:
        return cu.tenant_id
    if cu.tier == "platform":
        return None
    raise AuthorizationError("Tenant context required")


class IncidentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    tenant_id: UUID
    incident_number: str
    title: str
    description: Optional[str] = None
    severity_id: Optional[UUID] = None
    status: str
    declared_by: Optional[UUID] = None
    declared_at: datetime
    mitigated_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None
    affected_service_ids: Optional[list[UUID]] = None
    source_alert_id: Optional[UUID] = None
    source_ticket_id: Optional[UUID] = None


class DeclareRequest(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    description: Optional[str] = None
    severity_id: Optional[UUID] = None
    affected_service_ids: Optional[list[UUID]] = None
    source_alert_id: Optional[UUID] = None
    source_ticket_id: Optional[UUID] = None


class ChangeStatusRequest(BaseModel):
    status: IncidentStatus


class AssignRoleRequest(BaseModel):
    role: IncidentRoleType
    user_id: UUID


class StatusUpdateRequest(BaseModel):
    body: str = Field(min_length=1)
    audience: Audience = Audience.internal
    channels: Optional[list[str]] = None


async def _get(db, tid, iid) -> Incident:
    """tid=None (platform user, no tenant selected) fetches across all tenants —
    matches list_incidents' cross-tenant behavior via _scope()."""
    q = select(Incident).where(Incident.id == iid)
    if tid is not None:
        q = q.where(Incident.tenant_id == tid)
    inc = (await db.execute(q)).scalar_one_or_none()
    if inc is None:
        raise ResourceNotFoundError("Incident", "requested")
    return inc


@router.get("", response_model=list[IncidentResponse])
async def list_incidents(status: Optional[str] = None, cu: CurrentUser = Depends(require_role(*_READ)), db: AsyncSession = Depends(get_db)):
    tid = _scope(cu)
    q = select(Incident)
    if tid is not None:
        q = q.where(Incident.tenant_id == tid)
    if status:
        q = q.where(Incident.status == status)
    return (await db.execute(q.order_by(Incident.declared_at.desc()).limit(200))).scalars().all()


@router.post("", response_model=IncidentResponse, status_code=201)
@router.post("/declare", response_model=IncidentResponse, status_code=201, include_in_schema=False)
async def declare(body: DeclareRequest, cu: CurrentUser = Depends(require_role(*_WRITE)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    inc = await incident_service.declare_incident(
        db, tid, cu.local_user_id, title=body.title, description=body.description,
        severity_id=body.severity_id, affected_service_ids=body.affected_service_ids,
        source_alert_id=body.source_alert_id, source_ticket_id=body.source_ticket_id,
    )
    await db.commit(); await db.refresh(inc)
    return inc


@router.get("/{incident_id}", response_model=IncidentResponse)
async def get_incident(incident_id: UUID, cu: CurrentUser = Depends(require_role(*_READ)), db: AsyncSession = Depends(get_db)):
    return await _get(db, _scope(cu), incident_id)


@router.post("/{incident_id}/change-status", response_model=IncidentResponse)
async def change_status(incident_id: UUID, body: ChangeStatusRequest, cu: CurrentUser = Depends(require_role(*_WRITE)), db: AsyncSession = Depends(get_db)):
    inc = await _get(db, _tenant(cu), incident_id)
    await incident_service.change_status(db, inc, body.status.value, cu.local_user_id)
    await db.commit(); await db.refresh(inc)
    return inc


@router.post("/{incident_id}/assign-role", response_model=IncidentResponse)
async def assign_role(incident_id: UUID, body: AssignRoleRequest, cu: CurrentUser = Depends(require_role(*_WRITE)), db: AsyncSession = Depends(get_db)):
    inc = await _get(db, _tenant(cu), incident_id)
    await incident_service.assign_role(db, inc, body.role.value, body.user_id, cu.local_user_id)
    await db.commit(); await db.refresh(inc)
    return inc


@router.get("/{incident_id}/roles")
async def list_roles(incident_id: UUID, cu: CurrentUser = Depends(require_role(*_READ)), db: AsyncSession = Depends(get_db)):
    await _get(db, _scope(cu), incident_id)
    rows = (await db.execute(select(IncidentRole).where(IncidentRole.incident_id == incident_id))).scalars().all()
    return {"roles": [{"role": r.role, "user_id": str(r.user_id)} for r in rows]}


@router.get("/{incident_id}/timeline")
async def timeline(incident_id: UUID, cu: CurrentUser = Depends(require_role(*_READ)), db: AsyncSession = Depends(get_db)):
    await _get(db, _scope(cu), incident_id)
    rows = (await db.execute(
        select(IncidentTimeline).where(IncidentTimeline.incident_id == incident_id).order_by(IncidentTimeline.at)
    )).scalars().all()
    return {"events": [
        {"event_type": e.event_type, "actor_id": str(e.actor_id) if e.actor_id else None,
         "data": e.data, "pinned": e.pinned, "at": e.at.isoformat() if e.at else None}
        for e in rows
    ]}


@router.post("/{incident_id}/status-updates")
async def post_status_update(incident_id: UUID, body: StatusUpdateRequest, cu: CurrentUser = Depends(require_role(*_WRITE)), db: AsyncSession = Depends(get_db)):
    inc = await _get(db, _tenant(cu), incident_id)
    upd = await incident_service.post_status_update(
        db, inc, cu.local_user_id, body=body.body, audience=body.audience.value, channels=body.channels)
    await db.commit(); await db.refresh(upd)
    return {"id": str(upd.id), "audience": upd.audience, "body": upd.body}


@router.get("/{incident_id}/status-updates")
async def list_status_updates(incident_id: UUID, cu: CurrentUser = Depends(require_role(*_READ)), db: AsyncSession = Depends(get_db)):
    await _get(db, _scope(cu), incident_id)
    rows = (await db.execute(
        select(IncidentStatusUpdate).where(IncidentStatusUpdate.incident_id == incident_id).order_by(IncidentStatusUpdate.created_at)
    )).scalars().all()
    return {"updates": [{"id": str(u.id), "body": u.body, "audience": u.audience,
                         "created_at": u.created_at.isoformat() if u.created_at else None} for u in rows]}


@router.post("/{incident_id}/link-alert", response_model=IncidentResponse)
async def link_alert(incident_id: UUID, alert_id: UUID, cu: CurrentUser = Depends(require_role(*_WRITE)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    inc = await _get(db, tid, incident_id)
    alert = (await db.execute(select(Alert).where(Alert.id == alert_id, Alert.tenant_id == tid))).scalar_one_or_none()
    if alert is None:
        raise ResourceNotFoundError("Alert", "requested")
    alert.incident_id = inc.id
    if inc.source_alert_id is None:
        inc.source_alert_id = alert.id
    await incident_service._timeline(db, inc.id, cu.local_user_id, "alert_linked", {"alert_id": str(alert_id)})
    await db.commit(); await db.refresh(inc)
    return inc


@router.get("/{incident_id}/blast-radius")
async def blast_radius(incident_id: UUID, cu: CurrentUser = Depends(require_role(*_READ)), db: AsyncSession = Depends(get_db)):
    inc = await _get(db, _scope(cu), incident_id)
    return await incident_service.blast_radius(db, inc)
