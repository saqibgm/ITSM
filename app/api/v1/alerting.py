"""Alerting API (Phase 8 / S8.2).

Routers: router (/on-call: escalation-policies, contact-methods, heartbeats),
alerts_router (/alerts), routing_router (/routing). Write=manager/admin,
read=agent+. Alert ingest also accepts an HMAC-free generic body (S8.2 core);
signed source presets are a follow-up.
"""

import secrets
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentUser, require_role
from app.database import get_db
from app.exceptions import AuthorizationError, ResourceNotFoundError, ValidationError
from app.models.alerting import (
    Alert, AlertRoutingRule, ContactMethod, EscalationPolicy, EscalationStep,
    Heartbeat, TargetType, NotifyStrategy, ContactType,
)
from app.services import alerting_service

_WRITE = ("admin", "tenant_admin", "manager")
_READ = ("admin", "tenant_admin", "manager", "team_lead", "agent")


def _tenant(cu: CurrentUser) -> UUID:
    if cu.tenant_id is None:
        raise AuthorizationError("Tenant context required")
    return cu.tenant_id


router = APIRouter(prefix="/on-call", tags=["alerting"])
alerts_router = APIRouter(prefix="/alerts", tags=["alerting"])
routing_router = APIRouter(prefix="/routing", tags=["alerting"])


# ── Escalation policies + steps ───────────────────────────────────────────────

class StepIn(BaseModel):
    position: int = Field(ge=1)
    target_type: TargetType
    target_id: UUID
    timeout_minutes: int = Field(default=15, gt=0)
    notify_strategy: NotifyStrategy = NotifyStrategy.current_oncall


class StepResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    position: int
    target_type: str
    target_id: UUID
    timeout_minutes: int
    notify_strategy: str


class PolicyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str
    repeat_count: int
    steps: list[StepResponse] = []


class PolicyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    repeat_count: int = Field(default=0, ge=0)


async def _get_policy(db, tid, pid) -> EscalationPolicy:
    p = (await db.execute(
        select(EscalationPolicy).where(EscalationPolicy.id == pid, EscalationPolicy.tenant_id == tid)
        .options(selectinload(EscalationPolicy.steps))
    )).scalar_one_or_none()
    if p is None:
        raise ResourceNotFoundError("Escalation policy", "requested")
    return p


@router.get("/escalation-policies", response_model=list[PolicyResponse])
async def list_policies(cu: CurrentUser = Depends(require_role(*_READ)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    return (await db.execute(
        select(EscalationPolicy).where(EscalationPolicy.tenant_id == tid)
        .options(selectinload(EscalationPolicy.steps)).order_by(EscalationPolicy.name)
    )).scalars().unique().all()


@router.post("/escalation-policies", response_model=PolicyResponse, status_code=201)
async def create_policy(body: PolicyCreate, cu: CurrentUser = Depends(require_role(*_WRITE)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    p = EscalationPolicy(tenant_id=tid, **body.model_dump())
    db.add(p); await db.commit()
    return await _get_policy(db, tid, p.id)


@router.post("/escalation-policies/{policy_id}/steps", response_model=StepResponse, status_code=201)
async def add_step(policy_id: UUID, body: StepIn, cu: CurrentUser = Depends(require_role(*_WRITE)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    await _get_policy(db, tid, policy_id)
    data = body.model_dump()
    data["target_type"] = data["target_type"].value if hasattr(data["target_type"], "value") else data["target_type"]
    data["notify_strategy"] = data["notify_strategy"].value if hasattr(data["notify_strategy"], "value") else data["notify_strategy"]
    step = EscalationStep(policy_id=policy_id, **data)
    db.add(step); await db.commit(); await db.refresh(step)
    return step


@router.delete("/escalation-policies/{policy_id}", status_code=204)
async def delete_policy(policy_id: UUID, cu: CurrentUser = Depends(require_role(*_WRITE)), db: AsyncSession = Depends(get_db)):
    p = await _get_policy(db, _tenant(cu), policy_id)
    await db.delete(p); await db.commit()


@router.post("/escalation-policies/{policy_id}/test")
async def test_policy(policy_id: UUID, cu: CurrentUser = Depends(require_role(*_READ)), db: AsyncSession = Depends(get_db)):
    """Dry-run: resolve who each step would page right now."""
    p = await _get_policy(db, _tenant(cu), policy_id)
    steps = sorted(p.steps, key=lambda s: s.position)
    out = []
    for s in steps:
        users = await alerting_service._resolve_step_users(db, s)
        out.append({"position": s.position, "target_type": s.target_type,
                    "would_page": [str(u) for u in users]})
    return {"policy_id": str(policy_id), "steps": out}


# ── Contact methods (self-service; admins can manage anyone via user_id) ──────

class ContactCreate(BaseModel):
    type: ContactType
    value: str = Field(min_length=1, max_length=255)
    user_id: Optional[UUID] = None  # defaults to caller


class ContactResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    user_id: UUID
    type: str
    value: str
    is_verified: bool


@router.get("/contact-methods", response_model=list[ContactResponse])
async def list_contacts(cu: CurrentUser = Depends(require_role(*_READ)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    q = select(ContactMethod).where(ContactMethod.tenant_id == tid)
    if cu.local_user_id is not None:
        q = q.where(ContactMethod.user_id == cu.local_user_id)
    return (await db.execute(q)).scalars().all()


@router.post("/contact-methods", response_model=ContactResponse, status_code=201)
async def create_contact(body: ContactCreate, cu: CurrentUser = Depends(require_role(*_READ)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    uid = body.user_id or cu.local_user_id
    if uid is None:
        raise ValidationError("user_id required")
    cm = ContactMethod(tenant_id=tid, user_id=uid,
                       type=body.type.value if hasattr(body.type, "value") else body.type,
                       value=body.value)
    db.add(cm); await db.commit(); await db.refresh(cm)
    return cm


@router.delete("/contact-methods/{cm_id}", status_code=204)
async def delete_contact(cm_id: UUID, cu: CurrentUser = Depends(require_role(*_READ)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    cm = (await db.execute(select(ContactMethod).where(ContactMethod.id == cm_id, ContactMethod.tenant_id == tid))).scalar_one_or_none()
    if cm is None:
        raise ResourceNotFoundError("Contact method", "requested")
    await db.delete(cm); await db.commit()


# ── Heartbeats ────────────────────────────────────────────────────────────────

class HeartbeatCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    interval_sec: int = Field(gt=0)
    service_id: Optional[UUID] = None


class HeartbeatResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str
    interval_sec: int
    service_id: Optional[UUID] = None
    last_ping_at: Optional[datetime] = None
    ping_token: str


@router.get("/heartbeats", response_model=list[HeartbeatResponse])
async def list_heartbeats(cu: CurrentUser = Depends(require_role(*_READ)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    return (await db.execute(select(Heartbeat).where(Heartbeat.tenant_id == tid))).scalars().all()


@router.post("/heartbeats", response_model=HeartbeatResponse, status_code=201)
async def create_heartbeat(body: HeartbeatCreate, cu: CurrentUser = Depends(require_role(*_WRITE)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    hb = Heartbeat(tenant_id=tid, name=body.name, interval_sec=body.interval_sec,
                   service_id=body.service_id, ping_token=secrets.token_urlsafe(24))
    db.add(hb); await db.commit(); await db.refresh(hb)
    return hb


@router.post("/heartbeats/ping/{token}", status_code=204)
async def ping_heartbeat(token: str, db: AsyncSession = Depends(get_db)):
    """Unauthenticated liveness ping (secret token). Records last_ping_at."""
    hb = (await db.execute(select(Heartbeat).where(Heartbeat.ping_token == token))).scalar_one_or_none()
    if hb is None:
        raise ResourceNotFoundError("Heartbeat", "token")
    hb.last_ping_at = func.now()
    await db.commit()


# ── Alert routing rules ───────────────────────────────────────────────────────

class RoutingIn(BaseModel):
    service_id: UUID
    conditions: dict = Field(default_factory=dict)
    severity_id: Optional[UUID] = None
    position: Optional[int] = None


class RoutingResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    position: int
    conditions: dict
    service_id: UUID
    severity_id: Optional[UUID] = None


@routing_router.get("/rules", response_model=list[RoutingResponse])
async def list_routing(cu: CurrentUser = Depends(require_role(*_READ)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    return (await db.execute(
        select(AlertRoutingRule).where(AlertRoutingRule.tenant_id == tid).order_by(AlertRoutingRule.position)
    )).scalars().all()


@routing_router.post("/rules", response_model=RoutingResponse, status_code=201)
async def create_routing(body: RoutingIn, cu: CurrentUser = Depends(require_role(*_WRITE)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    pos = body.position
    if pos is None:
        pos = int((await db.execute(
            select(func.coalesce(func.max(AlertRoutingRule.position), 0)).where(AlertRoutingRule.tenant_id == tid)
        )).scalar_one()) + 1
    r = AlertRoutingRule(tenant_id=tid, position=pos, conditions=body.conditions,
                         service_id=body.service_id, severity_id=body.severity_id)
    db.add(r); await db.commit(); await db.refresh(r)
    return r


@routing_router.delete("/rules/{rule_id}", status_code=204)
async def delete_routing(rule_id: UUID, cu: CurrentUser = Depends(require_role(*_WRITE)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    r = (await db.execute(select(AlertRoutingRule).where(AlertRoutingRule.id == rule_id, AlertRoutingRule.tenant_id == tid))).scalar_one_or_none()
    if r is None:
        raise ResourceNotFoundError("Routing rule", "requested")
    await db.delete(r); await db.commit()


# ── Alerts ────────────────────────────────────────────────────────────────────

class AlertIngest(BaseModel):
    dedup_key: str = Field(min_length=1, max_length=255)
    title: str = Field(min_length=1, max_length=500)
    source: str = "manual"
    payload: Optional[dict] = None
    service_id: Optional[UUID] = None
    severity_id: Optional[UUID] = None


class AlertResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    service_id: Optional[UUID] = None
    source: str
    dedup_key: str
    title: str
    status: str
    occurrence_count: int
    severity_id: Optional[UUID] = None
    escalation_policy_id: Optional[UUID] = None
    escalation_step_index: int
    incident_id: Optional[UUID] = None


@alerts_router.get("", response_model=list[AlertResponse])
async def list_alerts(status: Optional[str] = None, cu: CurrentUser = Depends(require_role(*_READ)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    q = select(Alert).where(Alert.tenant_id == tid)
    if status:
        q = q.where(Alert.status == status)
    return (await db.execute(q.order_by(Alert.created_at.desc()).limit(200))).scalars().all()


@alerts_router.post("", response_model=AlertResponse, status_code=201)
async def ingest(body: AlertIngest, cu: CurrentUser = Depends(require_role(*_READ)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    alert = await alerting_service.ingest_alert(
        db, tid, dedup_key=body.dedup_key, title=body.title, source=body.source,
        payload=body.payload, service_id=body.service_id, severity_id=body.severity_id,
    )
    await db.commit(); await db.refresh(alert)
    return alert


async def _get_alert(db, tid, aid) -> Alert:
    a = (await db.execute(select(Alert).where(Alert.id == aid, Alert.tenant_id == tid))).scalar_one_or_none()
    if a is None:
        raise ResourceNotFoundError("Alert", "requested")
    return a


@alerts_router.get("/{alert_id}", response_model=AlertResponse)
async def get_alert(alert_id: UUID, cu: CurrentUser = Depends(require_role(*_READ)), db: AsyncSession = Depends(get_db)):
    return await _get_alert(db, _tenant(cu), alert_id)


@alerts_router.post("/{alert_id}/acknowledge", response_model=AlertResponse)
async def ack_alert(alert_id: UUID, cu: CurrentUser = Depends(require_role(*_READ)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    a = await _get_alert(db, tid, alert_id)
    await alerting_service.acknowledge_alert(db, a)
    await db.commit(); await db.refresh(a)
    return a


@alerts_router.post("/{alert_id}/resolve", response_model=AlertResponse)
async def resolve_alert_ep(alert_id: UUID, cu: CurrentUser = Depends(require_role(*_READ)), db: AsyncSession = Depends(get_db)):
    tid = _tenant(cu)
    a = await _get_alert(db, tid, alert_id)
    await alerting_service.resolve_alert(db, a)
    await db.commit(); await db.refresh(a)
    return a
