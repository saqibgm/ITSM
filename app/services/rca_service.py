"""RCA Governance service (specs/08, Phase 1+2).

`IncidentRetrospective` is the single row type for both the legacy PIR flow
(is_rca_governed=False) and the governed RCA flow (is_rca_governed=True) —
see app/models/retro.py for why. This module owns the governed lifecycle:
state machine, evidence checklist, action items, AI draft accept/reject, and
dashboard aggregations. Every transition writes an insert-only RcaHistory row
and dispatches a notification per the table in the implementation plan.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import InvalidStateTransitionError, ValidationError
from app.models.notification import NotificationType
from app.models.retro import (
    RCA_EVIDENCE_TYPES,
    AIPIRDraft,
    IncidentRetrospective,
    RcaEvidenceChecklist,
    RcaHistory,
    RcaLinkedEntity,
    RetroActionItem,
)
from app.services.notification_service import NotificationService

logger = logging.getLogger(__name__)

_RCA_DRAFT_PROMPT = (
    "You are drafting a blameless root-cause-analysis (RCA) report for an internal "
    "ITSM incident review. Never assign individual blame — focus on systemic, "
    "process, tooling, and environmental factors. Respond as strict JSON with keys: "
    "summary, root_cause_statement, contributing_factors, detection_gap, response_gap, "
    "prevention_plan, customer_facing_summary, suggested_action_items (list of short strings)."
)

# --- state machine -----------------------------------------------------

_TRANSITIONS: dict[str, set[str]] = {
    "not_required": {"required"},
    "required": {"draft", "waived"},
    "draft": {"under_review", "waived"},
    "under_review": {"approved", "rejected"},
    "rejected": {"draft"},
    "approved": {"actions_in_progress", "completed"},
    "actions_in_progress": {"completed"},
    "waived": set(),
    "completed": set(),
}

_WAIVE_ONLY_FROM = {"required", "draft"}


def _effective_status(retro: IncidentRetrospective) -> str:
    """The 'overdue' overlay never blocks a real transition — it's tracked via
    previous_status and transitions are evaluated against the underlying status."""
    if retro.status == "overdue" and retro.previous_status:
        return retro.previous_status
    return retro.status


async def _history(db: AsyncSession, retro: IncidentRetrospective, actor_id: Optional[UUID],
                    event_type: str, field_changed: Optional[str] = None,
                    old_value: Optional[dict] = None, new_value: Optional[dict] = None) -> None:
    db.add(RcaHistory(
        retro_id=retro.id, tenant_id=retro.tenant_id, actor_id=actor_id,
        event_type=event_type, field_changed=field_changed, old_value=old_value, new_value=new_value,
    ))


async def next_rca_number(db: AsyncSession) -> str:
    val = await db.scalar(sa.text("SELECT nextval('rca_number_seq')"))
    return f"RCA-{int(val):06d}"


async def create_rca(
    db: AsyncSession,
    tenant_id: UUID,
    actor_id: Optional[UUID],
    *,
    source_type: str,
    title: str,
    incident_id: Optional[UUID] = None,
    source_ticket_id: Optional[UUID] = None,
    severity: Optional[str] = None,
    owner_id: Optional[UUID] = None,
    team_id: Optional[UUID] = None,
    due_at: Optional[datetime] = None,
    trigger_policy_id: Optional[UUID] = None,
    manual: bool = False,
) -> IncidentRetrospective:
    """Create a governed RCA row. `manual=True` (a human clicked "Create RCA")
    starts in `draft`; system/policy-created RCAs start in `required`."""
    number = await next_rca_number(db)
    retro = IncidentRetrospective(
        tenant_id=tenant_id, incident_id=incident_id, is_rca_governed=True,
        source_type=source_type, source_ticket_id=source_ticket_id,
        rca_number=number, status="draft" if manual else "required",
        severity=severity, owner_id=owner_id, team_id=team_id, due_at=due_at,
        trigger_policy_id=trigger_policy_id, created_by=actor_id,
    )
    # IncidentRetrospective has no `title` column (legacy retro rows never had
    # one) — narrative content lives in `summary`/`executive_summary`. Seed the
    # caller's title into executive_summary as an editable starting point.
    retro.executive_summary = title
    db.add(retro)
    await db.flush()

    for evidence_type in RCA_EVIDENCE_TYPES:
        db.add(RcaEvidenceChecklist(retro_id=retro.id, evidence_type=evidence_type, required=True))

    source_entity_type = "incident" if incident_id else ("ticket" if source_ticket_id else source_type)
    source_entity_id = incident_id or source_ticket_id
    if source_entity_id is not None:
        db.add(RcaLinkedEntity(
            retro_id=retro.id, tenant_id=tenant_id, entity_type=source_entity_type,
            entity_id=source_entity_id, link_role="source", required=True, linked_by=actor_id,
        ))

    await _history(db, retro, actor_id, "status_change", "status", None, {"status": retro.status})

    if owner_id is not None:
        await NotificationService().send(
            db, tenant_id=tenant_id, user_id=owner_id, type=NotificationType.rca_required,
            title=f"RCA {number} required", body=title, entity_type="rca_case", entity_id=retro.id,
        )
    return retro


class _NotAllowedError(ValidationError):
    """Raised for well-formed-but-blocked transitions (missing evidence,
    open action items, etc.) — a 400, distinct from the 409 state-machine
    violation, since the *edge* is legal but its preconditions aren't met."""


async def _require_evidence_and_sections(db: AsyncSession, retro: IncidentRetrospective) -> None:
    missing_sections = [
        name for name, val in (
            ("executive_summary", retro.executive_summary),
            ("root_cause_statement", retro.root_cause_statement),
            ("customer_impact", retro.customer_impact),
        ) if not val
    ]
    if missing_sections:
        raise _NotAllowedError(f"Cannot submit — missing required sections: {', '.join(missing_sections)}")

    outstanding = (await db.execute(
        select(RcaEvidenceChecklist.evidence_type).where(
            RcaEvidenceChecklist.retro_id == retro.id,
            RcaEvidenceChecklist.required.is_(True),
            RcaEvidenceChecklist.status == "missing",
        )
    )).scalars().all()
    if outstanding:
        raise _NotAllowedError(f"Cannot submit — missing required evidence: {', '.join(outstanding)}")


async def _require_actions_closeable(db: AsyncSession, retro: IncidentRetrospective) -> None:
    items = (await db.execute(select(RetroActionItem).where(RetroActionItem.retro_id == retro.id))).scalars().all()
    open_items = [i for i in items if i.status not in ("done", "cancelled", "accepted_risk")]
    if open_items:
        raise _NotAllowedError(
            f"Cannot complete — {len(open_items)} action item(s) still open: "
            + ", ".join(i.title or i.description[:40] for i in open_items)
        )
    unverified_critical = [
        i for i in items
        if i.priority in ("critical", "high") and i.status == "done" and i.verified_by is None
    ]
    if unverified_critical:
        raise _NotAllowedError(
            "Cannot complete — critical/high action items must be verified before closure: "
            + ", ".join(i.title or i.description[:40] for i in unverified_critical)
        )


_NOTIFY_ON_TRANSITION: dict[str, tuple[NotificationType, str]] = {
    "under_review": (NotificationType.rca_submitted, "approver_id"),
    "approved": (NotificationType.rca_approved, "owner_id"),
    "rejected": (NotificationType.rca_rejected, "owner_id"),
    "waived": (NotificationType.rca_completed, "owner_id"),  # closest existing type; waived has no dedicated one
    "completed": (NotificationType.rca_completed, "owner_id"),
}


async def transition(
    db: AsyncSession,
    retro: IncidentRetrospective,
    actor_id: Optional[UUID],
    to_status: str,
    *,
    reason: Optional[str] = None,
) -> IncidentRetrospective:
    """Validate + apply a state-machine transition, writing history and
    dispatching notifications. Raises InvalidStateTransitionError (-> HTTP 409
    with valid_next) for illegal edges, ValidationError (-> HTTP 400) for
    legal-but-blocked edges (missing evidence/open action items/missing reason)."""
    current = _effective_status(retro)
    valid_next = _TRANSITIONS.get(current, set())
    if to_status not in valid_next:
        raise InvalidStateTransitionError(current, to_status, sorted(valid_next))

    if to_status == "waived" and current not in _WAIVE_ONLY_FROM:
        raise InvalidStateTransitionError(current, to_status, sorted(valid_next))
    if to_status == "waived" and not reason:
        raise _NotAllowedError("Waiving an RCA requires a waiver_reason")
    if to_status == "rejected" and not reason:
        raise _NotAllowedError("Rejecting an RCA requires a reason")

    if to_status == "under_review":
        await _require_evidence_and_sections(db, retro)
    if to_status == "completed":
        await _require_actions_closeable(db, retro)

    old_status = retro.status
    retro.status = to_status
    now = func.now()

    if to_status == "under_review":
        retro.submitted_at = now
    elif to_status == "approved":
        retro.approved_at = now
        approver_role_ok = True  # role gating enforced at the API layer
        existing_actions = (await db.execute(
            select(func.count()).select_from(RetroActionItem).where(RetroActionItem.retro_id == retro.id)
        )).scalar_one()
        if existing_actions:
            retro.status = "actions_in_progress"
    elif to_status == "rejected":
        pass
    elif to_status == "waived":
        retro.waived_at = now
        retro.waived_by = actor_id
        retro.waiver_reason = reason
    elif to_status == "completed":
        retro.completed_at = now

    await _history(db, retro, actor_id, "status_change", "status", {"status": old_status}, {"status": retro.status, "reason": reason})

    notify = _NOTIFY_ON_TRANSITION.get(retro.status)
    if notify is not None:
        ntype, owner_field = notify
        recipient = getattr(retro, owner_field, None)
        if recipient is not None:
            await NotificationService().send(
                db, tenant_id=retro.tenant_id, user_id=recipient, type=ntype,
                title=f"RCA {retro.rca_number} {retro.status.replace('_', ' ')}",
                body=reason, entity_type="rca_case", entity_id=retro.id,
            )
    return retro


async def reopen(db: AsyncSession, retro: IncidentRetrospective, actor_id: Optional[UUID]) -> IncidentRetrospective:
    """Explicit override from completed/waived back to draft — bypasses the
    normal graph (manager/admin only, enforced at the API layer)."""
    if retro.status not in ("completed", "waived"):
        raise InvalidStateTransitionError(retro.status, "draft", sorted(_TRANSITIONS.get(retro.status, set())))
    old_status = retro.status
    retro.status = "draft"
    await _history(db, retro, actor_id, "status_change", "status", {"status": old_status}, {"status": "draft", "reopen": True})
    return retro


# --- action items --------------------------------------------------------


async def create_action_item(
    db: AsyncSession, retro: IncidentRetrospective, actor_id: Optional[UUID], *,
    description: str, title: Optional[str] = None, owner_id: Optional[UUID] = None,
    priority: str = "medium", action_type: Optional[str] = None, due_at: Optional[datetime] = None,
) -> RetroActionItem:
    item = RetroActionItem(
        tenant_id=retro.tenant_id, retro_id=retro.id, description=description, title=title,
        owner_id=owner_id, priority=priority, action_type=action_type, due_at=due_at,
    )
    db.add(item)
    await db.flush()

    # "approved -> actions_in_progress" side-effect: creating the first action
    # item on an approved retro is what advances it (specs/08 §4.3.1).
    if retro.is_rca_governed and retro.status == "approved":
        retro.status = "actions_in_progress"
        await _history(db, retro, actor_id, "status_change", "status", {"status": "approved"}, {"status": "actions_in_progress"})

    if owner_id is not None:
        await NotificationService().send(
            db, tenant_id=retro.tenant_id, user_id=owner_id, type=NotificationType.rca_action_assigned,
            title=f"Action item assigned on RCA {retro.rca_number or retro.id}",
            body=description, entity_type="rca_case", entity_id=retro.id,
        )
    return item


async def verify_action_item(db: AsyncSession, item: RetroActionItem, actor_id: UUID, *, verification_method: Optional[str] = None) -> RetroActionItem:
    if item.status != "done":
        raise ValidationError("Only a 'done' action item can be verified")
    item.verified_by = actor_id
    item.verified_at = func.now()
    if verification_method:
        item.verification_method = verification_method
    return item


async def accept_risk_action_item(db: AsyncSession, item: RetroActionItem, actor_id: UUID, *, reason: str) -> RetroActionItem:
    if not reason:
        raise ValidationError("accept-risk requires a reason")
    item.status = "accepted_risk"
    item.accepted_risk_by = actor_id
    item.accepted_risk_reason = reason
    return item


# --- AI draft --------------------------------------------------------------


async def generate_ai_draft(db: AsyncSession, tenant_id: UUID, retro: IncidentRetrospective, redis) -> AIPIRDraft:
    """Graceful-degrade: any AI/budget/parse failure yields a template-only
    draft rather than raising into the route (matches sre_service.generate_pir_draft)."""
    import json as _json

    context_parts = [f"RCA {retro.rca_number or ''}: {retro.executive_summary or ''}"]
    if retro.incident_id:
        from app.models.incident import Incident, IncidentTimeline
        incident = (await db.execute(select(Incident).where(Incident.id == retro.incident_id))).scalar_one_or_none()
        if incident is not None:
            context_parts.append(f"Incident {incident.incident_number}: {incident.title}\n{incident.description or ''}")
            events = (await db.execute(
                select(IncidentTimeline).where(IncidentTimeline.incident_id == incident.id).order_by(IncidentTimeline.at)
            )).scalars().all()
            context_parts.append("Timeline:\n" + "\n".join(f"- {e.at}: {e.event_type} {e.data}" for e in events))
    context = "\n\n".join(context_parts)

    summary_text = f"Draft summary for {retro.rca_number} (template — AI unavailable)."
    root_cause = "Root cause to be confirmed by the RCA owner (auto-draft)."
    factors = "Contributing factors to be confirmed (auto-draft)."
    detection_gap = response_gap = prevention_plan = customer_facing = None
    suggested_actions: list[dict] = [{"description": "Document root cause"}, {"description": "Add detection/monitoring follow-up"}]
    model_version = "template-v1"

    try:
        from app.services.ai.ai_service import ai_service
        from app.services.pii_masker import pii_masker
        from app.config import get_settings

        masked = pii_masker.mask(context)
        raw = await ai_service.generate(
            tenant_id=str(tenant_id), redis=redis,
            messages=[{"role": "user", "content": masked}],
            system=_RCA_DRAFT_PROMPT, feature="rca_draft",
        )
        parsed = _json.loads(raw)
        summary_text = parsed.get("summary", summary_text)
        root_cause = parsed.get("root_cause_statement", root_cause)
        factors = parsed.get("contributing_factors", factors)
        detection_gap = parsed.get("detection_gap")
        response_gap = parsed.get("response_gap")
        prevention_plan = parsed.get("prevention_plan")
        customer_facing = parsed.get("customer_facing_summary")
        suggested_actions = [{"description": d} for d in parsed.get("suggested_action_items", []) if d] or suggested_actions
        model_version = get_settings().CLAUDE_MODEL
        retro.ai_draft_status = "completed"
    except Exception as exc:
        logger.warning("rca_ai_draft_failed_fallback_to_template",
                       extra={"tenant_id": str(tenant_id), "retro_id": str(retro.id), "error": str(exc)})
        retro.ai_draft_status = "failed"

    # AIPIRDraft's columns (summary/contributing_factors/impact/action_items)
    # predate RCA governance and aren't extended with dedicated root-cause/
    # prevention/customer-facing columns (Phase 1/2 scope). Rather than
    # silently drop those generated fields, they ride along as a "meta" entry
    # in the action_items JSONB list — accept_ai_draft below reads it back out.
    action_items_with_meta = [{
        "_meta": True, "root_cause_statement": root_cause,
        "detection_gap": detection_gap, "prevention_plan": prevention_plan,
        "customer_facing_summary": customer_facing,
    }] + suggested_actions

    draft = AIPIRDraft(
        tenant_id=tenant_id, retro_id=retro.id, summary=summary_text,
        contributing_factors=factors, impact=response_gap, action_items=action_items_with_meta,
        model_version=model_version,
    )
    db.add(draft)
    await db.flush()
    return draft


async def accept_ai_draft(db: AsyncSession, retro: IncidentRetrospective, draft: AIPIRDraft, actor_id: Optional[UUID]) -> IncidentRetrospective:
    """Copies the draft's fields onto the real RCA columns — the first place
    PIRDraftStatus.accepted ever becomes reachable."""
    items = draft.action_items or []
    meta = next((i for i in items if i.get("_meta")), {})

    retro.executive_summary = retro.executive_summary or draft.summary
    retro.root_cause_statement = retro.root_cause_statement or meta.get("root_cause_statement")
    retro.contributing_factors = draft.contributing_factors
    retro.detection_gap = retro.detection_gap or meta.get("detection_gap")
    retro.prevention_plan = retro.prevention_plan or meta.get("prevention_plan")
    retro.customer_facing_summary = retro.customer_facing_summary or meta.get("customer_facing_summary")
    if draft.impact:
        retro.response_gap = retro.response_gap or draft.impact
    draft.status = "accepted"
    await _history(db, retro, actor_id, "field_change", "ai_draft", None, {"draft_id": str(draft.id), "accepted": True})
    return retro


async def reject_ai_draft(db: AsyncSession, draft: AIPIRDraft, actor_id: Optional[UUID], reason: Optional[str] = None) -> AIPIRDraft:
    draft.status = "rejected"
    return draft


# --- dashboards (specs/08 §5) ---------------------------------------------


async def dashboard_summary(db: AsyncSession, tenant_id: UUID, since: Optional[datetime] = None) -> dict:
    since = since or (datetime.now(timezone.utc) - timedelta(days=30))
    base = select(IncidentRetrospective).where(
        IncidentRetrospective.tenant_id == tenant_id,
        IncidentRetrospective.is_rca_governed.is_(True),
        IncidentRetrospective.created_at >= since,
    )
    rows = (await db.execute(base)).scalars().all()
    total = len(rows)
    completed = [r for r in rows if r.status == "completed"]
    overdue = sum(1 for r in rows if r.status == "overdue")
    awaiting_approval = sum(1 for r in rows if r.status == "under_review")
    rejected = sum(1 for r in rows if r.status == "rejected")
    waived = sum(1 for r in rows if r.status == "waived")
    completed_on_time = sum(1 for r in completed if r.due_at is None or (r.completed_at and r.completed_at <= r.due_at))
    action_items_overdue = (await db.execute(
        select(func.count()).select_from(RetroActionItem)
        .where(RetroActionItem.tenant_id == tenant_id, RetroActionItem.status == "overdue")
    )).scalar_one()
    durations = [
        (r.completed_at - r.created_at).total_seconds()
        for r in completed if r.completed_at is not None
    ]
    avg_completion = round(sum(durations) / len(durations), 1) if durations else None
    return {
        "since": since.isoformat(),
        "required_count": total,
        "completed_on_time_count": completed_on_time,
        "overdue_count": overdue,
        "awaiting_approval_count": awaiting_approval,
        "rejected_count": rejected,
        "waived_count": waived,
        "action_items_overdue_count": int(action_items_overdue),
        "avg_completion_time_seconds": avg_completion,
        "compliance_pct": round(100 * len(completed) / total, 1) if total else None,
    }


async def dashboard_pipeline(db: AsyncSession, tenant_id: UUID) -> dict:
    rows = (await db.execute(
        select(IncidentRetrospective.status, func.count())
        .where(IncidentRetrospective.tenant_id == tenant_id, IncidentRetrospective.is_rca_governed.is_(True))
        .group_by(IncidentRetrospective.status)
    )).all()
    return {"pipeline": {status: int(n) for status, n in rows}}


async def dashboard_overdue(db: AsyncSession, tenant_id: UUID) -> dict:
    rows = (await db.execute(
        select(IncidentRetrospective.team_id, IncidentRetrospective.owner_id, IncidentRetrospective.service_id,
               IncidentRetrospective.root_cause_category, func.count())
        .where(IncidentRetrospective.tenant_id == tenant_id, IncidentRetrospective.status == "overdue")
        .group_by(IncidentRetrospective.team_id, IncidentRetrospective.owner_id,
                  IncidentRetrospective.service_id, IncidentRetrospective.root_cause_category)
    )).all()
    return {"overdue": [
        {"team_id": str(t) if t else None, "owner_id": str(o) if o else None,
         "service_id": str(s) if s else None, "root_cause_category": c, "count": int(n)}
        for t, o, s, c, n in rows
    ]}


async def dashboard_action_burndown(db: AsyncSession, tenant_id: UUID) -> dict:
    rows = (await db.execute(
        select(RetroActionItem.status, RetroActionItem.action_type, func.count())
        .where(RetroActionItem.tenant_id == tenant_id)
        .group_by(RetroActionItem.status, RetroActionItem.action_type)
    )).all()
    by_status: dict[str, int] = {}
    corrective = preventive = 0
    for status, action_type, n in rows:
        by_status[status] = by_status.get(status, 0) + int(n)
        if action_type == "corrective":
            corrective += int(n)
        elif action_type == "preventive":
            preventive += int(n)
    return {"by_status": by_status, "corrective_count": corrective, "preventive_count": preventive}


async def dashboard_evidence_completeness(db: AsyncSession, tenant_id: UUID) -> dict:
    rows = (await db.execute(
        select(RcaEvidenceChecklist.evidence_type, RcaEvidenceChecklist.status, func.count())
        .join(IncidentRetrospective, IncidentRetrospective.id == RcaEvidenceChecklist.retro_id)
        .where(IncidentRetrospective.tenant_id == tenant_id)
        .group_by(RcaEvidenceChecklist.evidence_type, RcaEvidenceChecklist.status)
    )).all()
    by_type: dict[str, dict[str, int]] = {}
    for evidence_type, status, n in rows:
        by_type.setdefault(evidence_type, {}).setdefault(status, 0)
        by_type[evidence_type][status] += int(n)
    return {"evidence_completeness": by_type}


async def dashboard_root_cause_trends(db: AsyncSession, tenant_id: UUID) -> dict:
    rows = (await db.execute(
        select(IncidentRetrospective.root_cause_category, func.count())
        .where(IncidentRetrospective.tenant_id == tenant_id, IncidentRetrospective.root_cause_category.isnot(None))
        .group_by(IncidentRetrospective.root_cause_category)
    )).all()
    return {"root_cause_distribution": {cat: int(n) for cat, n in rows}}
