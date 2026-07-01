"""Per-ticket SLA runtime API (Phase 7 / S7.2).

GET  /tickets/{id}/sla           — live target clocks (status, due, remaining, %)
POST /tickets/{id}/sla/override  — apply a chosen agreement (opens fresh instances)
GET  /tickets/{id}/sla/events    — immutable event log for the ticket's instances
"""

from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentUser, require_role
from app.database import get_db
from app.exceptions import AuthorizationError, ResourceNotFoundError
from app.models.sla import SLAAgreement, SLAEvent, SLAInstance, SLATarget
from app.models.ticket import Ticket
from app.services import sla_runtime

router = APIRouter(prefix="/tickets", tags=["sla"])

_WRITE_ROLES = ("admin", "tenant_admin", "manager")
_READ_ROLES = ("admin", "tenant_admin", "manager", "team_lead", "agent")


def _tenant(cu: CurrentUser) -> UUID:
    if cu.tenant_id is None:
        raise AuthorizationError("Tenant context required")
    return cu.tenant_id


class OverrideRequest(BaseModel):
    agreement_id: UUID
    reason: str | None = None


async def _get_ticket(db: AsyncSession, tid: UUID, ticket_id: UUID) -> Ticket:
    t = (await db.execute(
        select(Ticket).where(Ticket.id == ticket_id, Ticket.tenant_id == tid)
    )).scalar_one_or_none()
    if t is None:
        raise ResourceNotFoundError("Ticket not found")
    return t


async def _sla_rows(db: AsyncSession, ticket_id: UUID) -> list[dict]:
    rows = (await db.execute(
        select(
            SLAInstance,
            SLATarget.metric,
            func.extract("epoch", SLAInstance.due_at - func.now()).label("remaining_sec"),
            func.extract("epoch", SLAInstance.due_at - SLAInstance.created_at).label("total_sec"),
        )
        .join(SLATarget, SLATarget.id == SLAInstance.target_id)
        .where(SLAInstance.ticket_id == ticket_id)
        .order_by(SLAInstance.created_at)
    )).all()
    out = []
    for inst, metric, remaining_sec, total_sec in rows:
        remaining = float(remaining_sec) if remaining_sec is not None else None
        total = float(total_sec) if total_sec else 0.0
        consumed_pct = None
        if total > 0 and remaining is not None:
            consumed_pct = max(0.0, min(100.0, round((total - remaining) / total * 100, 1)))
        out.append({
            "id": str(inst.id),
            "target_id": str(inst.target_id),
            "metric": metric,
            "status": inst.status,
            "due_at": inst.due_at.isoformat() if inst.due_at else None,
            "remaining_seconds": None if inst.status in ("met", "stopped", "cancelled") else remaining,
            "consumed_pct": consumed_pct,
            "agreement_version": inst.agreement_version,
            "breached_at": inst.breached_at.isoformat() if inst.breached_at else None,
            "attributed_party": inst.attributed_party,
            "paused": inst.status == "paused",
        })
    return out


@router.get("/{ticket_id}/sla")
async def get_ticket_sla(
    ticket_id: UUID,
    cu: CurrentUser = Depends(require_role(*_READ_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    tid = _tenant(cu)
    await _get_ticket(db, tid, ticket_id)
    return {"ticket_id": str(ticket_id), "instances": await _sla_rows(db, ticket_id)}


@router.post("/{ticket_id}/sla/override")
async def override_ticket_sla(
    ticket_id: UUID,
    body: OverrideRequest,
    cu: CurrentUser = Depends(require_role(*_WRITE_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    tid = _tenant(cu)
    ticket = await _get_ticket(db, tid, ticket_id)
    ag = (await db.execute(
        select(SLAAgreement).where(
            SLAAgreement.id == body.agreement_id,
            SLAAgreement.tenant_id == tid,
            SLAAgreement.deleted_at.is_(None),
        )
    )).scalar_one_or_none()
    if ag is None:
        raise ResourceNotFoundError("Agreement not found")
    await sla_runtime.open_instances(db, ticket, ag)
    await db.commit()
    return {"ticket_id": str(ticket_id), "instances": await _sla_rows(db, ticket_id)}


@router.get("/{ticket_id}/sla/events")
async def get_ticket_sla_events(
    ticket_id: UUID,
    cu: CurrentUser = Depends(require_role(*_READ_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    tid = _tenant(cu)
    await _get_ticket(db, tid, ticket_id)
    rows = (await db.execute(
        select(SLAEvent, SLAInstance.target_id)
        .join(SLAInstance, SLAInstance.id == SLAEvent.instance_id)
        .where(SLAInstance.ticket_id == ticket_id)
        .order_by(SLAEvent.at)
    )).all()
    return {
        "ticket_id": str(ticket_id),
        "events": [
            {
                "instance_id": str(ev.instance_id),
                "target_id": str(target_id),
                "event": ev.event,
                "reason": ev.reason,
                "at": ev.at.isoformat() if ev.at else None,
            }
            for ev, target_id in rows
        ],
    }
