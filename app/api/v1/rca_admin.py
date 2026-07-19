"""RCA policy builder + recording policy admin API (specs/08, Phase 2)."""

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentUser, require_role
from app.database import get_db
from app.exceptions import AuthorizationError, ResourceNotFoundError
from app.models.recording import TenantRecordingPolicy
from app.models.retro import RcaPolicy
from app.services import rca_policy_engine

router = APIRouter(tags=["rca-admin"])

_POLICY_ADMIN = ("manager", "admin")


def _tenant(cu: CurrentUser) -> UUID:
    if cu.tenant_id is None:
        raise AuthorizationError("Tenant context required")
    return cu.tenant_id


class RcaPolicyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str
    description: Optional[str] = None
    status: str
    conditions: list
    outputs: dict
    priority: int


class RcaPolicyIn(BaseModel):
    name: str
    description: Optional[str] = None
    status: str = "draft"
    conditions: list = []
    outputs: dict = {}
    priority: int = 100


async def _get_policy(db: AsyncSession, tid: UUID, policy_id: UUID) -> RcaPolicy:
    row = (await db.execute(select(RcaPolicy).where(RcaPolicy.id == policy_id, RcaPolicy.tenant_id == tid))).scalar_one_or_none()
    if row is None:
        raise ResourceNotFoundError("RcaPolicy", str(policy_id))
    return row


@router.get("/tenant/rca-policies")
async def list_policies(cu: CurrentUser = Depends(require_role(*_POLICY_ADMIN)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    rows = (await db.execute(select(RcaPolicy).where(RcaPolicy.tenant_id == tid).order_by(RcaPolicy.priority))).scalars().all()
    return {"items": [RcaPolicyResponse.model_validate(r).model_dump(mode="json") for r in rows]}


@router.post("/tenant/rca-policies")
async def create_policy(body: RcaPolicyIn, cu: CurrentUser = Depends(require_role(*_POLICY_ADMIN)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    row = RcaPolicy(tenant_id=tid, created_by=cu.local_user_id, **body.model_dump())
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return RcaPolicyResponse.model_validate(row).model_dump(mode="json")


@router.get("/tenant/rca-policies/{policy_id}")
async def get_policy(policy_id: UUID, cu: CurrentUser = Depends(require_role(*_POLICY_ADMIN)), db: AsyncSession = Depends(get_db)):
    row = await _get_policy(db, _tenant(cu), policy_id)
    return RcaPolicyResponse.model_validate(row).model_dump(mode="json")


@router.patch("/tenant/rca-policies/{policy_id}")
async def update_policy(policy_id: UUID, body: RcaPolicyIn, cu: CurrentUser = Depends(require_role(*_POLICY_ADMIN)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    row = await _get_policy(db, tid, policy_id)
    for k, v in body.model_dump().items():
        setattr(row, k, v)
    await db.commit()
    await db.refresh(row)
    return RcaPolicyResponse.model_validate(row).model_dump(mode="json")


@router.delete("/tenant/rca-policies/{policy_id}")
async def delete_policy(policy_id: UUID, cu: CurrentUser = Depends(require_role(*_POLICY_ADMIN)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    row = await _get_policy(db, tid, policy_id)
    await db.delete(row)
    await db.commit()
    return {"id": str(policy_id), "deleted": True}


class PolicyTestIn(BaseModel):
    ticket_type: Optional[str] = None
    priority: Optional[str] = None
    sla_breached: bool = False
    severity_rank: Optional[int] = None
    repeat_count_window: int = 0
    security_flag: bool = False
    manual_override: bool = False


@router.post("/tenant/rca-policies/{policy_id}/test")
async def test_policy(policy_id: UUID, body: PolicyTestIn, cu: CurrentUser = Depends(require_role(*_POLICY_ADMIN)), db: AsyncSession = Depends(get_db)):
    """Dry-run — no DB writes. Evaluates the FULL active policy set (not just
    this one policy_id) so the admin sees the real first-match-wins outcome;
    policy_id is accepted for symmetry with the PRD's route shape."""
    tid = _tenant(cu)
    await _get_policy(db, tid, policy_id)
    ctx = rca_policy_engine.PolicyContext(**body.model_dump())
    decision = await rca_policy_engine.evaluate(db, tid, ctx)
    if decision is None:
        return {"required": False}
    return {
        "required": True, "policy_id": str(decision.policy_id) if decision.policy_id else None,
        "owner_role": decision.owner_role, "approver_role": decision.approver_role,
        "due_days": decision.due_days, "required_evidence_types": decision.required_evidence_types,
    }


# ── Recording policy singleton ──────────────────────────────────────────

class RecordingPolicyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    recording_consent_required: bool
    consent_source: str
    block_link_if_missing_consent: bool
    allow_customer_visibility: bool
    redact_transcript_before_ai: bool
    allow_ai_summary: bool
    recording_retention_days: int
    allow_manual_upload: bool


class RecordingPolicyIn(BaseModel):
    recording_consent_required: Optional[bool] = None
    consent_source: Optional[str] = None
    block_link_if_missing_consent: Optional[bool] = None
    allow_customer_visibility: Optional[bool] = None
    redact_transcript_before_ai: Optional[bool] = None
    allow_ai_summary: Optional[bool] = None
    recording_retention_days: Optional[int] = None
    allow_manual_upload: Optional[bool] = None


@router.get("/tenant/recording-policies")
async def get_recording_policy(cu: CurrentUser = Depends(require_role(*_POLICY_ADMIN)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    row = (await db.execute(select(TenantRecordingPolicy).where(TenantRecordingPolicy.tenant_id == tid))).scalar_one_or_none()
    if row is None:
        row = TenantRecordingPolicy(tenant_id=tid)
        db.add(row)
        await db.commit()
        await db.refresh(row)
    return RecordingPolicyResponse.model_validate(row).model_dump(mode="json")


@router.patch("/tenant/recording-policies")
async def update_recording_policy(body: RecordingPolicyIn, cu: CurrentUser = Depends(require_role(*_POLICY_ADMIN)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    row = (await db.execute(select(TenantRecordingPolicy).where(TenantRecordingPolicy.tenant_id == tid))).scalar_one_or_none()
    if row is None:
        row = TenantRecordingPolicy(tenant_id=tid)
        db.add(row)
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(row, k, v)
    await db.commit()
    await db.refresh(row)
    return RecordingPolicyResponse.model_validate(row).model_dump(mode="json")
