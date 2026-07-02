"""SRE analytics + post-incident review API (Phase 8 / S8.5). Prefixless router; full paths."""

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentUser, require_role
from app.database import get_db
from app.exceptions import AuthorizationError, ResourceNotFoundError
from app.models.incident import Incident
from app.models.retro import AIPIRDraft, IncidentRetrospective, RetroActionItem
from app.services import sre_service
from app.services.ticket_service import CreateTicketData, TicketService

router = APIRouter(tags=["sre"])

_READ = ("admin", "tenant_admin", "manager", "team_lead", "agent")
_WRITE = _READ


def _tenant(cu: CurrentUser) -> UUID:
    if cu.tenant_id is None:
        raise AuthorizationError("Tenant context required")
    return cu.tenant_id


async def _get_incident(db, tid, iid) -> Incident:
    inc = (await db.execute(select(Incident).where(Incident.id == iid, Incident.tenant_id == tid))).scalar_one_or_none()
    if inc is None:
        raise ResourceNotFoundError("Incident", "requested")
    return inc


# ── Retrospective ─────────────────────────────────────────────────────────────

class RetroIn(BaseModel):
    summary: Optional[str] = None
    contributing_factors: Optional[str] = None
    impact: Optional[str] = None
    status: Optional[str] = None


class RetroResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    incident_id: UUID
    summary: Optional[str] = None
    contributing_factors: Optional[str] = None
    impact: Optional[str] = None
    status: str


async def _get_retro(db, incident_id) -> Optional[IncidentRetrospective]:
    return (await db.execute(
        select(IncidentRetrospective).where(IncidentRetrospective.incident_id == incident_id)
    )).scalar_one_or_none()


@router.get("/incidents/{incident_id}/retrospective", response_model=Optional[RetroResponse])
async def get_retro(incident_id: UUID, cu: CurrentUser = Depends(require_role(*_READ)), db: AsyncSession = Depends(get_db)):
    await _get_incident(db, _tenant(cu), incident_id)
    return await _get_retro(db, incident_id)


@router.put("/incidents/{incident_id}/retrospective", response_model=RetroResponse)
async def upsert_retro(incident_id: UUID, body: RetroIn, cu: CurrentUser = Depends(require_role(*_WRITE)), db: AsyncSession = Depends(get_db)):
    await _get_incident(db, _tenant(cu), incident_id)
    retro = await _get_retro(db, incident_id)
    data = body.model_dump(exclude_none=True)
    if retro is None:
        retro = IncidentRetrospective(incident_id=incident_id, **data)
        db.add(retro)
    else:
        for k, v in data.items():
            setattr(retro, k, v)
    if data.get("status") == "published":
        from sqlalchemy import func
        retro.published_at = func.now()
    await db.commit(); await db.refresh(retro)
    return retro


@router.post("/incidents/{incident_id}/pir-draft")
async def generate_pir(incident_id: UUID, cu: CurrentUser = Depends(require_role(*_WRITE)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    inc = await _get_incident(db, tid, incident_id)
    draft = await sre_service.generate_pir_draft(db, tid, inc)
    await db.commit(); await db.refresh(draft)
    return {"id": str(draft.id), "summary": draft.summary, "contributing_factors": draft.contributing_factors,
            "impact": draft.impact, "action_items": draft.action_items, "model_version": draft.model_version,
            "status": draft.status}


@router.get("/incidents/{incident_id}/suggest-responder")
async def suggest_responder(incident_id: UUID, cu: CurrentUser = Depends(require_role(*_READ)), db: AsyncSession = Depends(get_db)):
    inc = await _get_incident(db, _tenant(cu), incident_id)
    return {"incident_id": str(incident_id), "suggested_user_id": await sre_service.suggest_responder(db, inc)}


# ── Action items ──────────────────────────────────────────────────────────────

class ActionItemIn(BaseModel):
    description: str = Field(min_length=1)
    owner_id: Optional[UUID] = None


@router.post("/incidents/{incident_id}/retrospective/action-items")
async def add_action_item(incident_id: UUID, body: ActionItemIn, cu: CurrentUser = Depends(require_role(*_WRITE)), db: AsyncSession = Depends(get_db)):
    await _get_incident(db, _tenant(cu), incident_id)
    retro = await _get_retro(db, incident_id)
    if retro is None:
        retro = IncidentRetrospective(incident_id=incident_id)
        db.add(retro); await db.flush()
    item = RetroActionItem(retro_id=retro.id, description=body.description, owner_id=body.owner_id)
    db.add(item); await db.commit(); await db.refresh(item)
    return {"id": str(item.id), "description": item.description, "status": item.status}


@router.get("/incidents/{incident_id}/retrospective/action-items")
async def list_action_items(incident_id: UUID, cu: CurrentUser = Depends(require_role(*_READ)), db: AsyncSession = Depends(get_db)):
    await _get_incident(db, _tenant(cu), incident_id)
    retro = await _get_retro(db, incident_id)
    if retro is None:
        return {"items": []}
    rows = (await db.execute(select(RetroActionItem).where(RetroActionItem.retro_id == retro.id))).scalars().all()
    return {"items": [{"id": str(i.id), "description": i.description, "status": i.status,
                       "ticket_id": str(i.ticket_id) if i.ticket_id else None} for i in rows]}


@router.post("/incidents/retro-action-items/{item_id}/push-to-ticket")
async def push_action_to_ticket(item_id: UUID, cu: CurrentUser = Depends(require_role(*_WRITE)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    # scope the action item to a retro of an incident in this tenant
    item = (await db.execute(
        select(RetroActionItem).join(IncidentRetrospective, IncidentRetrospective.id == RetroActionItem.retro_id)
        .join(Incident, Incident.id == IncidentRetrospective.incident_id)
        .where(RetroActionItem.id == item_id, Incident.tenant_id == tid)
    )).scalar_one_or_none()
    if item is None:
        raise ResourceNotFoundError("Action item", "requested")
    if item.ticket_id is not None:
        return {"action_item_id": str(item_id), "ticket_id": str(item.ticket_id), "already_linked": True}
    from app.models.ticket import TicketPriority, TicketType
    ticket = await TicketService().create_ticket(
        tenant_id=tid, requester_id=cu.local_user_id,
        data=CreateTicketData(title=f"Follow-up: {item.description[:120]}",
                              description="Post-incident action item.",
                              type=TicketType.service_request, priority=TicketPriority.medium),
        redis=None, db=db,
    )
    item.ticket_id = ticket.id
    await db.commit()
    return {"action_item_id": str(item_id), "ticket_id": str(ticket.id)}


# ── SRE analytics ─────────────────────────────────────────────────────────────

@router.get("/incidents/reports/mttr")
async def incident_reports(cu: CurrentUser = Depends(require_role(*_READ)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    metrics = await sre_service.incident_metrics(db, tid)
    metrics["mtta_seconds"] = await sre_service.alert_mtta(db, tid)
    return metrics


@router.get("/on-call/reports/load")
async def oncall_load_report(cu: CurrentUser = Depends(require_role(*_READ)), db: AsyncSession = Depends(get_db)):
    return {"load": await sre_service.oncall_load(db, _tenant(cu))}


@router.get("/alerts/reports/noise")
async def alert_noise_report(cu: CurrentUser = Depends(require_role(*_READ)), db: AsyncSession = Depends(get_db)):
    return await sre_service.alert_noise(db, _tenant(cu))
