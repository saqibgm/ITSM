"""RCA Governance API (specs/08, Phase 1+2). Mounted at /rca."""

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
from app.models.retro import (
    IncidentRetrospective,
    RcaEvidenceChecklist,
    RcaHistory,
    RcaLinkedEntity,
    RetroActionItem,
)
from app.redis_client import get_redis
from app.services import rca_service

router = APIRouter(tags=["rca"])

_READ = ("agent", "team_lead", "manager", "admin")
_CREATE_EDIT = ("agent", "team_lead", "manager", "admin")
_APPROVE_WAIVE_ADMIN = ("manager", "admin")


def _tenant(cu: CurrentUser) -> UUID:
    if cu.tenant_id is None:
        raise AuthorizationError("Tenant context required")
    return cu.tenant_id


async def _get_rca(db: AsyncSession, tid: UUID, rca_id: UUID) -> IncidentRetrospective:
    retro = (await db.execute(
        select(IncidentRetrospective).where(
            IncidentRetrospective.id == rca_id, IncidentRetrospective.tenant_id == tid,
            IncidentRetrospective.is_rca_governed.is_(True),
        )
    )).scalar_one_or_none()
    if retro is None:
        raise ResourceNotFoundError("RCA", str(rca_id))
    return retro


class RcaResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    rca_number: Optional[str] = None
    status: str
    severity: Optional[str] = None
    incident_id: Optional[UUID] = None
    source_ticket_id: Optional[UUID] = None
    owner_id: Optional[UUID] = None
    approver_id: Optional[UUID] = None
    due_at: Optional[datetime] = None
    executive_summary: Optional[str] = None
    customer_impact: Optional[str] = None
    business_impact: Optional[str] = None
    technical_summary: Optional[str] = None
    root_cause_statement: Optional[str] = None
    root_cause_category: Optional[str] = None
    detection_gap: Optional[str] = None
    response_gap: Optional[str] = None
    prevention_plan: Optional[str] = None
    lessons_learned: Optional[str] = None
    customer_facing_summary: Optional[str] = None
    waiver_reason: Optional[str] = None


class CreateRcaIn(BaseModel):
    title: str = Field(min_length=1)
    incident_id: Optional[UUID] = None
    source_ticket_id: Optional[UUID] = None
    severity: Optional[str] = None
    owner_id: Optional[UUID] = None
    team_id: Optional[UUID] = None
    due_at: Optional[datetime] = None


class UpdateRcaIn(BaseModel):
    executive_summary: Optional[str] = None
    customer_impact: Optional[str] = None
    business_impact: Optional[str] = None
    technical_summary: Optional[str] = None
    root_cause_statement: Optional[str] = None
    root_cause_category: Optional[str] = None
    detection_gap: Optional[str] = None
    response_gap: Optional[str] = None
    prevention_plan: Optional[str] = None
    lessons_learned: Optional[str] = None
    customer_facing_summary: Optional[str] = None
    owner_id: Optional[UUID] = None
    approver_id: Optional[UUID] = None
    severity: Optional[str] = None
    due_at: Optional[datetime] = None


class ReasonIn(BaseModel):
    reason: Optional[str] = None


@router.get("")
async def list_rcas(
    status: Optional[str] = None, severity: Optional[str] = None, owner_id: Optional[UUID] = None,
    team_id: Optional[UUID] = None, incident_id: Optional[UUID] = None, source_ticket_id: Optional[UUID] = None,
    cu: CurrentUser = Depends(require_role(*_READ)), db: AsyncSession = Depends(get_db),
):
    tid = _tenant(cu)
    stmt = select(IncidentRetrospective).where(
        IncidentRetrospective.tenant_id == tid, IncidentRetrospective.is_rca_governed.is_(True)
    )
    if status:
        stmt = stmt.where(IncidentRetrospective.status == status)
    if severity:
        stmt = stmt.where(IncidentRetrospective.severity == severity)
    if owner_id:
        stmt = stmt.where(IncidentRetrospective.owner_id == owner_id)
    if team_id:
        stmt = stmt.where(IncidentRetrospective.team_id == team_id)
    if incident_id:
        stmt = stmt.where(IncidentRetrospective.incident_id == incident_id)
    if source_ticket_id:
        stmt = stmt.where(IncidentRetrospective.source_ticket_id == source_ticket_id)
    rows = (await db.execute(stmt.order_by(IncidentRetrospective.created_at.desc()))).scalars().all()
    return {"items": [RcaResponse.model_validate(r).model_dump(mode="json") for r in rows]}


@router.post("")
async def create_rca(
    body: CreateRcaIn, cu: CurrentUser = Depends(require_role(*_CREATE_EDIT)), db: AsyncSession = Depends(get_db),
):
    tid = _tenant(cu)
    retro = await rca_service.create_rca(
        db, tid, cu.local_user_id, source_type=("incident" if body.incident_id else "ticket"),
        title=body.title, incident_id=body.incident_id, source_ticket_id=body.source_ticket_id,
        severity=body.severity, owner_id=body.owner_id, team_id=body.team_id, due_at=body.due_at,
        manual=True,
    )
    await db.commit()
    await db.refresh(retro)
    return RcaResponse.model_validate(retro).model_dump(mode="json")


@router.get("/{rca_id}")
async def get_rca(rca_id: UUID, cu: CurrentUser = Depends(require_role(*_READ)), db: AsyncSession = Depends(get_db)):
    retro = await _get_rca(db, _tenant(cu), rca_id)
    return RcaResponse.model_validate(retro).model_dump(mode="json")


@router.patch("/{rca_id}")
async def update_rca(
    rca_id: UUID, body: UpdateRcaIn, cu: CurrentUser = Depends(require_role(*_CREATE_EDIT)), db: AsyncSession = Depends(get_db),
):
    tid = _tenant(cu)
    retro = await _get_rca(db, tid, rca_id)
    for field_name, value in body.model_dump(exclude_none=True).items():
        setattr(retro, field_name, value)
    await db.commit()
    await db.refresh(retro)
    return RcaResponse.model_validate(retro).model_dump(mode="json")


@router.delete("/{rca_id}")
async def delete_rca(rca_id: UUID, cu: CurrentUser = Depends(require_role(*_APPROVE_WAIVE_ADMIN)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    retro = await _get_rca(db, tid, rca_id)
    retro.status = "waived"
    retro.waiver_reason = retro.waiver_reason or "Deleted by admin/manager"
    await db.commit()
    return {"id": str(rca_id), "status": "waived"}


@router.post("/{rca_id}/submit")
async def submit_rca(rca_id: UUID, cu: CurrentUser = Depends(require_role(*_CREATE_EDIT)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    retro = await _get_rca(db, tid, rca_id)
    await rca_service.transition(db, retro, cu.local_user_id, "under_review")
    await db.commit()
    return RcaResponse.model_validate(retro).model_dump(mode="json")


@router.post("/{rca_id}/approve")
async def approve_rca(rca_id: UUID, cu: CurrentUser = Depends(require_role(*_APPROVE_WAIVE_ADMIN)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    retro = await _get_rca(db, tid, rca_id)
    retro.approver_id = retro.approver_id or cu.local_user_id
    await rca_service.transition(db, retro, cu.local_user_id, "approved")
    await db.commit()
    return RcaResponse.model_validate(retro).model_dump(mode="json")


@router.post("/{rca_id}/reject")
async def reject_rca(rca_id: UUID, body: ReasonIn, cu: CurrentUser = Depends(require_role(*_APPROVE_WAIVE_ADMIN)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    retro = await _get_rca(db, tid, rca_id)
    await rca_service.transition(db, retro, cu.local_user_id, "rejected", reason=body.reason)
    await db.commit()
    return RcaResponse.model_validate(retro).model_dump(mode="json")


@router.post("/{rca_id}/waive")
async def waive_rca(rca_id: UUID, body: ReasonIn, cu: CurrentUser = Depends(require_role(*_APPROVE_WAIVE_ADMIN)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    retro = await _get_rca(db, tid, rca_id)
    await rca_service.transition(db, retro, cu.local_user_id, "waived", reason=body.reason)
    await db.commit()
    return RcaResponse.model_validate(retro).model_dump(mode="json")


@router.post("/{rca_id}/complete")
async def complete_rca(rca_id: UUID, cu: CurrentUser = Depends(require_role(*_CREATE_EDIT)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    retro = await _get_rca(db, tid, rca_id)
    await rca_service.transition(db, retro, cu.local_user_id, "completed")
    await db.commit()
    return RcaResponse.model_validate(retro).model_dump(mode="json")


@router.post("/{rca_id}/reopen")
async def reopen_rca(rca_id: UUID, cu: CurrentUser = Depends(require_role(*_APPROVE_WAIVE_ADMIN)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    retro = await _get_rca(db, tid, rca_id)
    await rca_service.reopen(db, retro, cu.local_user_id)
    await db.commit()
    return RcaResponse.model_validate(retro).model_dump(mode="json")


@router.post("/{rca_id}/generate-ai-draft")
async def generate_ai_draft(rca_id: UUID, cu: CurrentUser = Depends(require_role(*_CREATE_EDIT)), db: AsyncSession = Depends(get_db), redis=Depends(get_redis)):
    tid = _tenant(cu)
    retro = await _get_rca(db, tid, rca_id)
    draft = await rca_service.generate_ai_draft(db, tid, retro, redis)
    await db.commit()
    await db.refresh(draft)
    return {"id": str(draft.id), "summary": draft.summary, "contributing_factors": draft.contributing_factors,
            "impact": draft.impact, "action_items": draft.action_items, "model_version": draft.model_version,
            "status": draft.status}


async def _get_draft(db, tid, rca_id, draft_id):
    from app.models.retro import AIPIRDraft
    draft = (await db.execute(
        select(AIPIRDraft).where(AIPIRDraft.id == draft_id, AIPIRDraft.retro_id == rca_id, AIPIRDraft.tenant_id == tid)
    )).scalar_one_or_none()
    if draft is None:
        raise ResourceNotFoundError("AIPIRDraft", str(draft_id))
    return draft


@router.post("/{rca_id}/ai-draft/{draft_id}/accept")
async def accept_ai_draft(rca_id: UUID, draft_id: UUID, cu: CurrentUser = Depends(require_role(*_CREATE_EDIT)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    retro = await _get_rca(db, tid, rca_id)
    draft = await _get_draft(db, tid, rca_id, draft_id)
    await rca_service.accept_ai_draft(db, retro, draft, cu.local_user_id)
    await db.commit()
    return RcaResponse.model_validate(retro).model_dump(mode="json")


@router.post("/{rca_id}/ai-draft/{draft_id}/reject")
async def reject_ai_draft(rca_id: UUID, draft_id: UUID, body: ReasonIn, cu: CurrentUser = Depends(require_role(*_CREATE_EDIT)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    draft = await _get_draft(db, tid, rca_id, draft_id)
    await rca_service.reject_ai_draft(db, draft, cu.local_user_id, body.reason)
    await db.commit()
    return {"id": str(draft_id), "status": "rejected"}


@router.get("/{rca_id}/history")
async def get_history(rca_id: UUID, cu: CurrentUser = Depends(require_role(*_READ)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    await _get_rca(db, tid, rca_id)
    rows = (await db.execute(
        select(RcaHistory).where(RcaHistory.retro_id == rca_id).order_by(RcaHistory.changed_at)
    )).scalars().all()
    return {"items": [
        {"id": str(r.id), "event_type": r.event_type, "field_changed": r.field_changed,
         "old_value": r.old_value, "new_value": r.new_value, "changed_at": r.changed_at.isoformat()}
        for r in rows
    ]}


# ── Evidence ─────────────────────────────────────────────────────────────

class LinkEvidenceIn(BaseModel):
    entity_type: str
    entity_id: Optional[UUID] = None
    entity_ref: Optional[str] = None
    link_role: str = "evidence"
    required: bool = False


@router.get("/{rca_id}/evidence")
async def list_evidence(rca_id: UUID, cu: CurrentUser = Depends(require_role(*_READ)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    await _get_rca(db, tid, rca_id)
    rows = (await db.execute(select(RcaLinkedEntity).where(RcaLinkedEntity.retro_id == rca_id))).scalars().all()
    return {"items": [
        {"id": str(r.id), "entity_type": r.entity_type, "entity_id": str(r.entity_id) if r.entity_id else None,
         "entity_ref": r.entity_ref, "link_role": r.link_role, "required": r.required}
        for r in rows
    ]}


@router.post("/{rca_id}/evidence")
async def add_evidence(rca_id: UUID, body: LinkEvidenceIn, cu: CurrentUser = Depends(require_role(*_CREATE_EDIT)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    await _get_rca(db, tid, rca_id)
    if body.entity_id is None and body.entity_ref is None:
        raise ValidationError("entity_id or entity_ref is required")
    row = RcaLinkedEntity(
        retro_id=rca_id, tenant_id=tid, entity_type=body.entity_type, entity_id=body.entity_id,
        entity_ref=body.entity_ref, link_role=body.link_role, required=body.required, linked_by=cu.local_user_id,
    )
    db.add(row)
    await db.commit()
    return {"id": str(row.id)}


@router.delete("/{rca_id}/evidence/{evidence_id}")
async def remove_evidence(rca_id: UUID, evidence_id: UUID, cu: CurrentUser = Depends(require_role(*_CREATE_EDIT)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    await _get_rca(db, tid, rca_id)
    row = (await db.execute(
        select(RcaLinkedEntity).where(RcaLinkedEntity.id == evidence_id, RcaLinkedEntity.retro_id == rca_id)
    )).scalar_one_or_none()
    if row is None:
        raise ResourceNotFoundError("RcaLinkedEntity", str(evidence_id))
    await db.delete(row)
    await db.commit()
    return {"id": str(evidence_id), "removed": True}


@router.get("/{rca_id}/evidence-checklist")
async def get_evidence_checklist(rca_id: UUID, cu: CurrentUser = Depends(require_role(*_READ)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    await _get_rca(db, tid, rca_id)
    rows = (await db.execute(select(RcaEvidenceChecklist).where(RcaEvidenceChecklist.retro_id == rca_id))).scalars().all()
    return {"items": [
        {"id": str(r.id), "evidence_type": r.evidence_type, "required": r.required, "status": r.status,
         "provided_entity_type": r.provided_entity_type,
         "provided_entity_id": str(r.provided_entity_id) if r.provided_entity_id else None,
         "waiver_reason": r.waiver_reason}
        for r in rows
    ]}


class UpdateChecklistIn(BaseModel):
    status: str
    provided_entity_type: Optional[str] = None
    provided_entity_id: Optional[UUID] = None


async def _get_checklist_item(db, rca_id, check_id) -> RcaEvidenceChecklist:
    row = (await db.execute(
        select(RcaEvidenceChecklist).where(RcaEvidenceChecklist.id == check_id, RcaEvidenceChecklist.retro_id == rca_id)
    )).scalar_one_or_none()
    if row is None:
        raise ResourceNotFoundError("RcaEvidenceChecklist", str(check_id))
    return row


@router.patch("/{rca_id}/evidence-checklist/{check_id}")
async def update_checklist_item(
    rca_id: UUID, check_id: UUID, body: UpdateChecklistIn,
    cu: CurrentUser = Depends(require_role(*_CREATE_EDIT)), db: AsyncSession = Depends(get_db),
):
    tid = _tenant(cu)
    await _get_rca(db, tid, rca_id)
    row = await _get_checklist_item(db, rca_id, check_id)
    row.status = body.status
    row.provided_entity_type = body.provided_entity_type
    row.provided_entity_id = body.provided_entity_id
    await db.commit()
    return {"id": str(check_id), "status": row.status}


@router.post("/{rca_id}/evidence-checklist/{check_id}/waive")
async def waive_checklist_item(
    rca_id: UUID, check_id: UUID, body: ReasonIn,
    cu: CurrentUser = Depends(require_role(*_APPROVE_WAIVE_ADMIN)), db: AsyncSession = Depends(get_db),
):
    if not body.reason:
        raise ValidationError("Waiving an evidence item requires a reason")
    tid = _tenant(cu)
    await _get_rca(db, tid, rca_id)
    row = await _get_checklist_item(db, rca_id, check_id)
    row.status = "waived"
    row.waived_by = cu.local_user_id
    row.waiver_reason = body.reason
    await db.commit()
    return {"id": str(check_id), "status": "waived"}


# ── Action items ─────────────────────────────────────────────────────────

class CreateActionIn(BaseModel):
    description: str = Field(min_length=1)
    title: Optional[str] = None
    owner_id: Optional[UUID] = None
    priority: str = "medium"
    action_type: Optional[str] = None
    due_at: Optional[datetime] = None


class UpdateActionIn(BaseModel):
    description: Optional[str] = None
    title: Optional[str] = None
    owner_id: Optional[UUID] = None
    priority: Optional[str] = None
    status: Optional[str] = None
    due_at: Optional[datetime] = None


async def _get_action_item(db, rca_id, action_id) -> RetroActionItem:
    row = (await db.execute(
        select(RetroActionItem).where(RetroActionItem.id == action_id, RetroActionItem.retro_id == rca_id)
    )).scalar_one_or_none()
    if row is None:
        raise ResourceNotFoundError("RetroActionItem", str(action_id))
    return row


@router.get("/{rca_id}/actions")
async def list_actions(rca_id: UUID, cu: CurrentUser = Depends(require_role(*_READ)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    await _get_rca(db, tid, rca_id)
    rows = (await db.execute(select(RetroActionItem).where(RetroActionItem.retro_id == rca_id))).scalars().all()
    return {"items": [
        {"id": str(r.id), "title": r.title, "description": r.description, "owner_id": str(r.owner_id) if r.owner_id else None,
         "priority": r.priority, "status": r.status, "due_at": r.due_at.isoformat() if r.due_at else None,
         "verified_by": str(r.verified_by) if r.verified_by else None}
        for r in rows
    ]}


@router.post("/{rca_id}/actions")
async def create_action(rca_id: UUID, body: CreateActionIn, cu: CurrentUser = Depends(require_role(*_CREATE_EDIT)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    retro = await _get_rca(db, tid, rca_id)
    item = await rca_service.create_action_item(
        db, retro, cu.local_user_id, description=body.description, title=body.title,
        owner_id=body.owner_id, priority=body.priority, action_type=body.action_type, due_at=body.due_at,
    )
    await db.commit()
    return {"id": str(item.id), "status": item.status}


@router.patch("/{rca_id}/actions/{action_id}")
async def update_action(rca_id: UUID, action_id: UUID, body: UpdateActionIn, cu: CurrentUser = Depends(require_role(*_CREATE_EDIT)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    await _get_rca(db, tid, rca_id)
    item = await _get_action_item(db, rca_id, action_id)
    for field_name, value in body.model_dump(exclude_none=True).items():
        setattr(item, field_name, value)
    await db.commit()
    return {"id": str(action_id), "status": item.status}


@router.post("/{rca_id}/actions/{action_id}/verify")
async def verify_action(rca_id: UUID, action_id: UUID, cu: CurrentUser = Depends(require_role(*_CREATE_EDIT)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    await _get_rca(db, tid, rca_id)
    item = await _get_action_item(db, rca_id, action_id)
    await rca_service.verify_action_item(db, item, cu.local_user_id)
    await db.commit()
    return {"id": str(action_id), "verified_by": str(cu.local_user_id)}


class AcceptRiskIn(BaseModel):
    reason: str = Field(min_length=1)


@router.post("/{rca_id}/actions/{action_id}/accept-risk")
async def accept_risk(rca_id: UUID, action_id: UUID, body: AcceptRiskIn, cu: CurrentUser = Depends(require_role(*_APPROVE_WAIVE_ADMIN)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    await _get_rca(db, tid, rca_id)
    item = await _get_action_item(db, rca_id, action_id)
    await rca_service.accept_risk_action_item(db, item, cu.local_user_id, reason=body.reason)
    await db.commit()
    return {"id": str(action_id), "status": "accepted_risk"}
