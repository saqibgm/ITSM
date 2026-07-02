"""Incident engine (Phase 8 / S8.3).

Declare → validated lifecycle (seeded state machine) → roles + auto-timeline +
stakeholder status updates. Blast-radius surfaces the affected services and their
CMDB assets. All timestamps use DB/UTC now.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import InvalidStateTransitionError, ValidationError
from app.models.alerting import Alert
from app.models.incident import (
    Incident, IncidentRole, IncidentStatusTransition, IncidentStatusUpdate, IncidentTimeline,
)
from app.models.oncall import OnCallService

_MITIGATE_STATUS = "monitoring"
_RESOLVE_STATUS = "resolved"


async def next_incident_number(db: AsyncSession) -> str:
    val = await db.scalar(sa.text("SELECT nextval('incident_number_seq')"))
    return f"IR-{datetime.now(timezone.utc).year}-{int(val):05d}"


async def _timeline(db: AsyncSession, incident_id: UUID, actor_id: Optional[UUID],
                    event_type: str, data: Optional[dict] = None) -> None:
    db.add(IncidentTimeline(incident_id=incident_id, actor_id=actor_id,
                            event_type=event_type, data=data))


async def declare_incident(db: AsyncSession, tenant_id: UUID, actor_id: Optional[UUID], *,
                           title: str, description: Optional[str] = None,
                           severity_id: Optional[UUID] = None,
                           affected_service_ids: Optional[list[UUID]] = None,
                           source_alert_id: Optional[UUID] = None,
                           source_ticket_id: Optional[UUID] = None) -> Incident:
    number = await next_incident_number(db)
    inc = Incident(
        tenant_id=tenant_id, incident_number=number, title=title, description=description,
        severity_id=severity_id, status="declared", declared_by=actor_id,
        affected_service_ids=affected_service_ids or None,
        source_alert_id=source_alert_id, source_ticket_id=source_ticket_id,
    )
    db.add(inc)
    await db.flush()
    await _timeline(db, inc.id, actor_id, "declared", {"incident_number": number, "title": title})
    # Link the source alert (bi-directional) so the queue reflects the incident.
    if source_alert_id is not None:
        alert = (await db.execute(select(Alert).where(Alert.id == source_alert_id))).scalar_one_or_none()
        if alert is not None:
            alert.incident_id = inc.id
    return inc


async def _valid_next(db: AsyncSession, frm: str) -> list[str]:
    return list((await db.execute(
        select(IncidentStatusTransition.to_status).where(IncidentStatusTransition.from_status == frm)
    )).scalars().all())


async def change_status(db: AsyncSession, incident: Incident, new_status: str, actor_id: Optional[UUID]) -> None:
    if new_status == incident.status:
        return
    valid = await _valid_next(db, incident.status)
    if new_status not in valid:
        raise InvalidStateTransitionError(incident.status, new_status, valid)
    old = incident.status
    incident.status = new_status
    if new_status == _MITIGATE_STATUS and incident.mitigated_at is None:
        incident.mitigated_at = func.now()
    if new_status == _RESOLVE_STATUS and incident.resolved_at is None:
        incident.resolved_at = func.now()
    await _timeline(db, incident.id, actor_id, "status_change", {"from": old, "to": new_status})


async def assign_role(db: AsyncSession, incident: Incident, role: str, user_id: UUID, actor_id: Optional[UUID]) -> IncidentRole:
    existing = (await db.execute(
        select(IncidentRole).where(IncidentRole.incident_id == incident.id, IncidentRole.role == role)
    )).scalar_one_or_none()
    if existing is not None:
        existing.user_id = user_id
        existing.assigned_at = func.now()
        row = existing
    else:
        row = IncidentRole(incident_id=incident.id, role=role, user_id=user_id)
        db.add(row)
    await _timeline(db, incident.id, actor_id, "role_assigned", {"role": role, "user_id": str(user_id)})
    return row


async def post_status_update(db: AsyncSession, incident: Incident, author_id: Optional[UUID], *,
                             body: str, audience: str = "internal",
                             channels: Optional[list[str]] = None) -> IncidentStatusUpdate:
    upd = IncidentStatusUpdate(incident_id=incident.id, author_id=author_id, body=body,
                               audience=audience, channels=channels)
    db.add(upd)
    await _timeline(db, incident.id, author_id, "status_update", {"audience": audience})
    return upd


async def blast_radius(db: AsyncSession, incident: Incident) -> dict:
    """Affected services + their CMDB assets (direct). Deep dependency-graph
    traversal via the asset impact CTE is a later enhancement."""
    svc_ids = incident.affected_service_ids or []
    services = []
    if svc_ids:
        rows = (await db.execute(
            select(OnCallService).where(OnCallService.id.in_(svc_ids))
        )).scalars().all()
        services = [{"id": str(s.id), "name": s.name, "asset_id": str(s.asset_id) if s.asset_id else None,
                     "current_state": s.current_state} for s in rows]
    return {"incident_id": str(incident.id), "affected_services": services}
