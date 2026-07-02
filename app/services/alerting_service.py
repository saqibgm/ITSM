"""Alerting & escalation engine (Phase 8 / S8.2).

Ingest (dedup) → route → escalation policy → page the on-call responder(s),
advancing through timed steps until acknowledged. Page delivery is abstracted:
a Page row is the recorded attempt (status 'queued'); a provider adapter sends
it later. All time bookkeeping uses DB/UTC now.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.alerting import (
    Alert, AlertRoutingRule, ContactMethod, EscalationPolicy, EscalationStep, Page,
)
from app.models.identity import Team, TeamMember
from app.models.oncall import OnCallService, Schedule
from app.services import oncall_service, slm_service


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def match_routing(db: AsyncSession, tenant_id: UUID, ctx: dict) -> tuple[Optional[UUID], Optional[UUID]]:
    """First matching alert_routing_rule → (service_id, severity_id)."""
    rules = (await db.execute(
        select(AlertRoutingRule).where(AlertRoutingRule.tenant_id == tenant_id)
        .order_by(AlertRoutingRule.position)
    )).scalars().all()
    for r in rules:
        if slm_service.conditions_match(r.conditions or {}, ctx):
            return r.service_id, r.severity_id
    return None, None


async def _resolve_step_users(db: AsyncSession, step: EscalationStep) -> list[UUID]:
    """Users to page for a step, per target_type + notify_strategy."""
    if step.target_type == "user":
        return [step.target_id]

    if step.target_type == "schedule":
        sch = (await db.execute(select(Schedule).where(Schedule.id == step.target_id))).scalar_one_or_none()
        if sch is None:
            return []
        res = await oncall_service.who_is_on_call(db, sch)
        if step.notify_strategy == "notify_all":
            return [UUID(r["user_id"]) for r in res["on_call"] if r["user_id"]]
        return [UUID(res["primary_user_id"])] if res["primary_user_id"] else []

    if step.target_type == "team":
        # Team's active schedule on-call, else the team's members.
        sch = (await db.execute(
            select(Schedule).where(Schedule.team_id == step.target_id, Schedule.is_active.is_(True))
        )).scalars().first()
        if sch is not None:
            res = await oncall_service.who_is_on_call(db, sch)
            if res["primary_user_id"]:
                return [UUID(res["primary_user_id"])]
        members = (await db.execute(
            select(TeamMember.user_id).where(TeamMember.team_id == step.target_id)
        )).scalars().all()
        return list(members)
    return []


async def _page_users(db: AsyncSession, alert: Alert, users: list[UUID], step: EscalationStep | None) -> int:
    n = 0
    for uid in users:
        cm = (await db.execute(
            select(ContactMethod).where(ContactMethod.user_id == uid)
            .order_by(ContactMethod.is_verified.desc())
        )).scalars().first()
        db.add(Page(alert_id=alert.id, user_id=uid,
                    contact_method_id=cm.id if cm else None,
                    escalation_step_id=step.id if step else None, status="queued"))
        n += 1
    return n


async def _ordered_steps(db: AsyncSession, policy_id: UUID) -> list[EscalationStep]:
    return list((await db.execute(
        select(EscalationStep).where(EscalationStep.policy_id == policy_id)
        .order_by(EscalationStep.position)
    )).scalars().all())


async def start_escalation(db: AsyncSession, alert: Alert) -> None:
    """Page the first escalation step and arm the next-step timer."""
    if not alert.escalation_policy_id:
        return
    steps = await _ordered_steps(db, alert.escalation_policy_id)
    if not steps:
        return
    step = steps[0]
    alert.escalation_step_index = 0
    users = await _resolve_step_users(db, step)
    await _page_users(db, alert, users, step)
    alert.next_escalation_at = _now() + timedelta(minutes=step.timeout_minutes)


async def advance_escalation(db: AsyncSession, alert: Alert) -> bool:
    """Move to the next step (looping per repeat_count). Returns True if paged."""
    if alert.status != "open" or not alert.escalation_policy_id:
        alert.next_escalation_at = None
        return False
    policy = (await db.execute(
        select(EscalationPolicy).where(EscalationPolicy.id == alert.escalation_policy_id)
    )).scalar_one_or_none()
    steps = await _ordered_steps(db, alert.escalation_policy_id) if policy else []
    if not steps:
        alert.next_escalation_at = None
        return False

    nxt = alert.escalation_step_index + 1
    total_allowed = len(steps) * (1 + (policy.repeat_count or 0))
    if nxt >= total_allowed:
        alert.next_escalation_at = None  # exhausted
        return False

    step = steps[nxt % len(steps)]
    alert.escalation_step_index = nxt
    users = await _resolve_step_users(db, step)
    await _page_users(db, alert, users, step)
    alert.next_escalation_at = _now() + timedelta(minutes=step.timeout_minutes)
    return True


async def ingest_alert(db: AsyncSession, tenant_id: UUID, *, dedup_key: str, title: str,
                       source: str = "manual", payload: Optional[dict] = None,
                       service_id: Optional[UUID] = None, severity_id: Optional[UUID] = None) -> Alert:
    """Create or de-dup an alert; route it; kick off escalation. Existing OPEN
    alert with the same dedup_key → bump occurrence_count (no re-page)."""
    existing = (await db.execute(
        select(Alert).where(Alert.tenant_id == tenant_id, Alert.dedup_key == dedup_key, Alert.status == "open")
    )).scalar_one_or_none()
    if existing is not None:
        existing.occurrence_count = (existing.occurrence_count or 1) + 1
        return existing

    # Route when not explicitly targeted.
    if service_id is None:
        ctx = {"source": source, "title": title, **(payload or {})}
        service_id, routed_sev = await match_routing(db, tenant_id, ctx)
        severity_id = severity_id or routed_sev

    alert = Alert(tenant_id=tenant_id, service_id=service_id, source=source,
                  dedup_key=dedup_key, title=title, payload=payload, severity_id=severity_id,
                  status="open")
    # Escalation policy from the routed/target service.
    if service_id is not None:
        svc = (await db.execute(select(OnCallService).where(OnCallService.id == service_id))).scalar_one_or_none()
        if svc is not None and svc.escalation_policy_id:
            alert.escalation_policy_id = svc.escalation_policy_id
    db.add(alert)
    await db.flush()

    # Suppress paging during an active maintenance window for the service.
    from app.services import workflow_service
    suppressed = await workflow_service.is_service_in_maintenance(db, tenant_id, service_id)
    if suppressed:
        alert.escalation_policy_id = None  # recorded but not paged
    else:
        await start_escalation(db, alert)

    # Fire alert_created workflows (best-effort; never block ingest).
    try:
        ctx = {"source": source, "title": title, "suppressed": suppressed, **(payload or {})}
        await workflow_service.run_workflows(db, tenant_id, "alert_created", ctx, alert=alert)
    except Exception:
        pass
    return alert


async def acknowledge_alert(db: AsyncSession, alert: Alert) -> None:
    alert.status = "acknowledged"
    alert.acknowledged_at = func.now()
    alert.next_escalation_at = None
    await db.execute(update(Page).where(Page.alert_id == alert.id, Page.status != "acknowledged")
                     .values(status="acknowledged", acknowledged_at=func.now()))


async def resolve_alert(db: AsyncSession, alert: Alert) -> None:
    alert.status = "resolved"
    alert.resolved_at = func.now()
    alert.next_escalation_at = None
