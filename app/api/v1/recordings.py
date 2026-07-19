"""Support Session Recording API (specs/08, Phase 1). Prefixless router; full paths."""

from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentUser, require_role
from app.database import get_db
from app.exceptions import AuthorizationError, ResourceNotFoundError
from app.models.recording import RecordingAccessLog, SupportRecording, TicketRecordingLink
from app.repositories.ticket_repo import TicketRepository
from app.services import recording_service

router = APIRouter(tags=["recordings"])

_READ = ("agent", "team_lead", "manager", "admin")
_LINK = ("agent", "team_lead", "manager", "admin")
_ACCESS_CONTROL = ("team_lead", "manager", "admin")
_LOG_READ = ("manager", "admin")


def _tenant(cu: CurrentUser) -> UUID:
    if cu.tenant_id is None:
        raise AuthorizationError("Tenant context required")
    return cu.tenant_id


def _client_ip(request: Request) -> Optional[str]:
    return request.client.host if request.client else None


def _request_id(request: Request) -> Optional[str]:
    return getattr(request.state, "request_id", None)


async def _get_recording(db: AsyncSession, tid: UUID, recording_id: UUID) -> SupportRecording:
    rec = (await db.execute(
        select(SupportRecording).where(SupportRecording.id == recording_id, SupportRecording.tenant_id == tid)
    )).scalar_one_or_none()
    if rec is None:
        raise ResourceNotFoundError("SupportRecording", str(recording_id))
    return rec


class RecordingResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    source_type: str
    title: str
    recording_url: str
    transcript_url: Optional[str] = None
    started_at: Optional[datetime] = None
    duration_seconds: Optional[int] = None
    organizer_email: Optional[str] = None
    consent_status: str
    sensitivity: str
    access_policy: str
    status: str
    ai_summary_status: str
    ai_summary: Optional[str] = None


class LinkFromUrlIn(BaseModel):
    ticket_id: UUID
    url: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=500)
    link_type: str = "support_call"
    evidence_weight: str = "none"
    notes: Optional[str] = None
    consent_status: str = "not_required"


class TicketLinkFromUrlIn(BaseModel):
    url: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=500)
    link_type: str = "support_call"
    evidence_weight: str = "none"
    notes: Optional[str] = None
    consent_status: str = "not_required"


class RestrictIn(BaseModel):
    access_policy: str = "restricted"


@router.get("/support-recordings")
async def list_recordings(
    status: Optional[str] = None, consent_status: Optional[str] = None, sensitivity: Optional[str] = None,
    cu: CurrentUser = Depends(require_role(*_READ)), db: AsyncSession = Depends(get_db),
):
    tid = _tenant(cu)
    stmt = select(SupportRecording).where(SupportRecording.tenant_id == tid)
    if status:
        stmt = stmt.where(SupportRecording.status == status)
    if consent_status:
        stmt = stmt.where(SupportRecording.consent_status == consent_status)
    if sensitivity:
        stmt = stmt.where(SupportRecording.sensitivity == sensitivity)
    rows = (await db.execute(stmt.order_by(SupportRecording.created_at.desc()))).scalars().all()
    return {"items": [RecordingResponse.model_validate(r).model_dump(mode="json") for r in rows]}


@router.post("/support-recordings/link-from-url")
async def create_recording_from_url(
    body: LinkFromUrlIn,
    cu: CurrentUser = Depends(require_role(*_LINK)), db: AsyncSession = Depends(get_db),
):
    tid = _tenant(cu)
    await TicketRepository(db).get_or_404(body.ticket_id, tid)
    recording, _link = await recording_service.link_from_url(
        db, tid, cu.local_user_id, cu.email, body.ticket_id, body.url, title=body.title,
        link_type=body.link_type, evidence_weight=body.evidence_weight, notes=body.notes,
        consent_status=body.consent_status,
    )
    await db.commit()
    await db.refresh(recording)
    return RecordingResponse.model_validate(recording).model_dump(mode="json")


@router.get("/support-recordings/{recording_id}")
async def get_recording(
    recording_id: UUID, request: Request,
    cu: CurrentUser = Depends(require_role(*_READ)), db: AsyncSession = Depends(get_db),
):
    tid = _tenant(cu)
    rec = await _get_recording(db, tid, recording_id)
    await recording_service.log_access(db, tid, rec, cu.local_user_id, cu.email, "viewed_metadata",
                                        _client_ip(request), _request_id(request))
    await db.commit()
    return RecordingResponse.model_validate(rec).model_dump(mode="json")


@router.patch("/support-recordings/{recording_id}")
async def update_recording(
    recording_id: UUID, body: dict, request: Request,
    cu: CurrentUser = Depends(require_role(*_ACCESS_CONTROL)), db: AsyncSession = Depends(get_db),
):
    tid = _tenant(cu)
    rec = await _get_recording(db, tid, recording_id)
    for field_name in ("access_policy", "sensitivity", "consent_status"):
        if field_name in body:
            setattr(rec, field_name, body[field_name])
    await recording_service.log_access(db, tid, rec, cu.local_user_id, cu.email, "changed_permissions",
                                        _client_ip(request), _request_id(request))
    await db.commit()
    await db.refresh(rec)
    return RecordingResponse.model_validate(rec).model_dump(mode="json")


@router.delete("/support-recordings/{recording_id}")
async def delete_recording(
    recording_id: UUID, cu: CurrentUser = Depends(require_role(*_ACCESS_CONTROL)), db: AsyncSession = Depends(get_db),
):
    tid = _tenant(cu)
    rec = await _get_recording(db, tid, recording_id)
    rec.status = "deleted"
    await db.commit()
    return {"id": str(recording_id), "status": "deleted"}


@router.post("/support-recordings/{recording_id}/generate-summary")
async def generate_summary(
    recording_id: UUID, cu: CurrentUser = Depends(require_role(*_LINK)), db: AsyncSession = Depends(get_db),
):
    tid = _tenant(cu)
    rec = await _get_recording(db, tid, recording_id)
    from app.workers.tasks_recordings import generate_summary_task
    rec.ai_summary_status = "pending"
    await db.commit()
    generate_summary_task.delay(str(recording_id))
    return {"id": str(recording_id), "ai_summary_status": "pending"}


@router.get("/support-recordings/{recording_id}/summary")
async def get_summary(
    recording_id: UUID, cu: CurrentUser = Depends(require_role(*_READ)), db: AsyncSession = Depends(get_db),
):
    tid = _tenant(cu)
    rec = await _get_recording(db, tid, recording_id)
    return {"ai_summary_status": rec.ai_summary_status, "ai_summary": rec.ai_summary, "ai_action_items": rec.ai_action_items}


@router.get("/support-recordings/{recording_id}/access-log")
async def get_access_log(
    recording_id: UUID, cu: CurrentUser = Depends(require_role(*_LOG_READ)), db: AsyncSession = Depends(get_db),
):
    tid = _tenant(cu)
    await _get_recording(db, tid, recording_id)
    rows = (await db.execute(
        select(RecordingAccessLog).where(RecordingAccessLog.recording_id == recording_id)
        .order_by(RecordingAccessLog.created_at.desc())
    )).scalars().all()
    return {"items": [
        {"id": str(r.id), "actor_email": r.actor_email, "action": r.action,
         "source_ip": r.source_ip, "created_at": r.created_at.isoformat()}
        for r in rows
    ]}


@router.post("/support-recordings/{recording_id}/restrict")
async def restrict_recording(
    recording_id: UUID, body: RestrictIn, request: Request,
    cu: CurrentUser = Depends(require_role(*_ACCESS_CONTROL)), db: AsyncSession = Depends(get_db),
):
    tid = _tenant(cu)
    rec = await _get_recording(db, tid, recording_id)
    rec.access_policy = body.access_policy
    await recording_service.log_access(db, tid, rec, cu.local_user_id, cu.email, "changed_permissions",
                                        _client_ip(request), _request_id(request))
    await db.commit()
    return {"id": str(recording_id), "access_policy": rec.access_policy}


# ── Ticket-scoped recording links ──────────────────────────────────────────

@router.get("/tickets/{ticket_id}/recordings")
async def list_ticket_recordings(
    ticket_id: UUID, cu: CurrentUser = Depends(require_role(*_READ)), db: AsyncSession = Depends(get_db),
):
    tid = _tenant(cu)
    await TicketRepository(db).get_or_404(ticket_id, tid)
    rows = (await db.execute(
        select(TicketRecordingLink, SupportRecording)
        .join(SupportRecording, SupportRecording.id == TicketRecordingLink.recording_id)
        .where(TicketRecordingLink.ticket_id == ticket_id)
    )).all()
    return {"items": [
        {**RecordingResponse.model_validate(rec).model_dump(mode="json"),
         "link_type": link.link_type, "is_primary": link.is_primary,
         "visible_to_customer": link.visible_to_customer, "evidence_weight": link.evidence_weight,
         "notes": link.notes}
        for link, rec in rows
    ]}


class LinkExistingIn(BaseModel):
    recording_id: UUID
    link_type: str = "support_call"
    evidence_weight: str = "none"
    is_primary: bool = False
    notes: Optional[str] = None


@router.post("/tickets/{ticket_id}/recordings")
async def link_existing_recording(
    ticket_id: UUID, body: LinkExistingIn,
    cu: CurrentUser = Depends(require_role(*_LINK)), db: AsyncSession = Depends(get_db),
):
    tid = _tenant(cu)
    await TicketRepository(db).get_or_404(ticket_id, tid)
    rec = await _get_recording(db, tid, body.recording_id)
    link = TicketRecordingLink(
        ticket_id=ticket_id, recording_id=rec.id, link_type=body.link_type,
        linked_by=cu.local_user_id, evidence_weight=body.evidence_weight,
        is_primary=body.is_primary, notes=body.notes,
    )
    db.add(link)
    await db.commit()
    return {"ticket_id": str(ticket_id), "recording_id": str(rec.id)}


@router.post("/tickets/{ticket_id}/recordings/from-url")
async def link_recording_from_url(
    ticket_id: UUID, body: TicketLinkFromUrlIn,
    cu: CurrentUser = Depends(require_role(*_LINK)), db: AsyncSession = Depends(get_db),
):
    tid = _tenant(cu)
    await TicketRepository(db).get_or_404(ticket_id, tid)
    recording, _link = await recording_service.link_from_url(
        db, tid, cu.local_user_id, cu.email, ticket_id, body.url, title=body.title,
        link_type=body.link_type, evidence_weight=body.evidence_weight, notes=body.notes,
        consent_status=body.consent_status,
    )
    await db.commit()
    await db.refresh(recording)
    return RecordingResponse.model_validate(recording).model_dump(mode="json")


@router.delete("/tickets/{ticket_id}/recordings/{recording_id}")
async def unlink_recording(
    ticket_id: UUID, recording_id: UUID,
    cu: CurrentUser = Depends(require_role(*_LINK)), db: AsyncSession = Depends(get_db),
):
    tid = _tenant(cu)
    await TicketRepository(db).get_or_404(ticket_id, tid)
    link = (await db.execute(
        select(TicketRecordingLink).where(
            TicketRecordingLink.ticket_id == ticket_id, TicketRecordingLink.recording_id == recording_id
        )
    )).scalar_one_or_none()
    if link is None:
        raise ResourceNotFoundError("TicketRecordingLink", str(recording_id))
    await db.delete(link)
    await db.commit()
    return {"ticket_id": str(ticket_id), "recording_id": str(recording_id), "unlinked": True}


class UpdateLinkIn(BaseModel):
    link_type: Optional[str] = None
    evidence_weight: Optional[str] = None
    is_primary: Optional[bool] = None
    visible_to_customer: Optional[bool] = None
    notes: Optional[str] = None


@router.patch("/tickets/{ticket_id}/recordings/{recording_id}")
async def update_recording_link(
    ticket_id: UUID, recording_id: UUID, body: UpdateLinkIn,
    cu: CurrentUser = Depends(require_role(*_LINK)), db: AsyncSession = Depends(get_db),
):
    tid = _tenant(cu)
    await TicketRepository(db).get_or_404(ticket_id, tid)
    link = (await db.execute(
        select(TicketRecordingLink).where(
            TicketRecordingLink.ticket_id == ticket_id, TicketRecordingLink.recording_id == recording_id
        )
    )).scalar_one_or_none()
    if link is None:
        raise ResourceNotFoundError("TicketRecordingLink", str(recording_id))
    for field_name, value in body.model_dump(exclude_none=True).items():
        setattr(link, field_name, value)
    await db.commit()
    return {"ticket_id": str(ticket_id), "recording_id": str(recording_id)}


# ── Dashboards ──────────────────────────────────────────────────────────────

@router.get("/dashboards/recordings/summary")
async def recordings_dashboard_summary(cu: CurrentUser = Depends(require_role(*_READ)), db: AsyncSession = Depends(get_db)):
    return await recording_service.summary(db, _tenant(cu))


@router.get("/dashboards/recordings/missing-required")
async def recordings_dashboard_missing(cu: CurrentUser = Depends(require_role(*_READ)), db: AsyncSession = Depends(get_db)):
    return await recording_service.missing_required_recordings(db, _tenant(cu))


@router.get("/dashboards/recordings/inaccessible")
async def recordings_dashboard_inaccessible(cu: CurrentUser = Depends(require_role(*_READ)), db: AsyncSession = Depends(get_db)):
    return await recording_service.inaccessible_recordings(db, _tenant(cu))


@router.get("/dashboards/recordings/consent-issues")
async def recordings_dashboard_consent(cu: CurrentUser = Depends(require_role(*_READ)), db: AsyncSession = Depends(get_db)):
    return await recording_service.consent_issues(db, _tenant(cu))
