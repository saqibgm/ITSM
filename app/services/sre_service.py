"""SRE analytics + post-incident helpers (Phase 8 / S8.5).

Analytics (MTTM/MTTR, on-call load, alert noise) are pure SQL. The PIR draft is
templated from the incident + its timeline (model_version 'template-v1') — an
LLM generator can replace it later behind the same shape/HITL. Responder
suggestion reuses the on-call resolver. pgvector similar-incident search is a
future upgrade.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.alerting import Alert, Page
from app.models.incident import Incident, IncidentTimeline
from app.models.oncall import OnCallService, Schedule
from app.models.retro import AIPIRDraft
from app.services import oncall_service


def _secs(v) -> Optional[float]:
    return round(float(v), 1) if v is not None else None


async def incident_metrics(db: AsyncSession, tenant_id: UUID, since: Optional[datetime] = None) -> dict:
    """MTTM/MTTR (seconds) + counts, over incidents declared since ``since``."""
    since = since or (datetime.now(timezone.utc) - timedelta(days=30))
    base = select(Incident).where(Incident.tenant_id == tenant_id, Incident.declared_at >= since)
    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    mttm = (await db.execute(
        select(func.avg(func.extract("epoch", Incident.mitigated_at - Incident.declared_at)))
        .where(Incident.tenant_id == tenant_id, Incident.declared_at >= since, Incident.mitigated_at.isnot(None))
    )).scalar_one()
    mttr = (await db.execute(
        select(func.avg(func.extract("epoch", Incident.resolved_at - Incident.declared_at)))
        .where(Incident.tenant_id == tenant_id, Incident.declared_at >= since, Incident.resolved_at.isnot(None))
    )).scalar_one()
    by_status = dict((s, int(n)) for s, n in (await db.execute(
        select(Incident.status, func.count()).where(Incident.tenant_id == tenant_id, Incident.declared_at >= since)
        .group_by(Incident.status)
    )).all())
    return {"since": since.isoformat(), "incidents": int(total),
            "mttm_seconds": _secs(mttm), "mttr_seconds": _secs(mttr), "by_status": by_status}


async def alert_mtta(db: AsyncSession, tenant_id: UUID, since: Optional[datetime] = None) -> Optional[float]:
    since = since or (datetime.now(timezone.utc) - timedelta(days=30))
    v = (await db.execute(
        select(func.avg(func.extract("epoch", Alert.acknowledged_at - Alert.created_at)))
        .where(Alert.tenant_id == tenant_id, Alert.created_at >= since, Alert.acknowledged_at.isnot(None))
    )).scalar_one()
    return _secs(v)


async def oncall_load(db: AsyncSession, tenant_id: UUID, since: Optional[datetime] = None) -> list[dict]:
    """Page count per responder (proxy for on-call burden). Pages join alerts for
    tenant scoping."""
    since = since or (datetime.now(timezone.utc) - timedelta(days=30))
    rows = (await db.execute(
        select(Page.user_id, func.count())
        .join(Alert, Alert.id == Page.alert_id)
        .where(Alert.tenant_id == tenant_id, Page.sent_at >= since)
        .group_by(Page.user_id).order_by(func.count().desc())
    )).all()
    return [{"user_id": str(u), "pages": int(n)} for u, n in rows]


async def alert_noise(db: AsyncSession, tenant_id: UUID, since: Optional[datetime] = None) -> dict:
    since = since or (datetime.now(timezone.utc) - timedelta(days=30))
    total, occ = (await db.execute(
        select(func.count(), func.coalesce(func.sum(Alert.occurrence_count), 0))
        .where(Alert.tenant_id == tenant_id, Alert.created_at >= since)
    )).one()
    total, occ = int(total), int(occ)
    return {"alerts": total, "total_occurrences": occ,
            "dedup_ratio": round(occ / total, 2) if total else None}


async def generate_pir_draft(db: AsyncSession, tenant_id: UUID, incident: Incident) -> AIPIRDraft:
    """Templated post-incident-review draft from the incident + timeline."""
    events = (await db.execute(
        select(IncidentTimeline).where(IncidentTimeline.incident_id == incident.id)
        .order_by(IncidentTimeline.at)
    )).scalars().all()
    duration = None
    if incident.resolved_at and incident.declared_at:
        duration = round((incident.resolved_at - incident.declared_at).total_seconds() / 60, 1)
    summary = (f"Incident {incident.incident_number} — \"{incident.title}\". "
               f"Declared {incident.declared_at:%Y-%m-%d %H:%M} UTC"
               + (f", resolved after {duration} min. " if duration is not None else ". ")
               + f"{len(events)} timeline events recorded.")
    factors = "Contributing factors to be confirmed by the responders (auto-draft)."
    impact = "Impact scope to be confirmed (affected services: "
    impact += (", ".join(str(s) for s in (incident.affected_service_ids or [])) or "none recorded") + ")."
    draft = AIPIRDraft(tenant_id=tenant_id, incident_id=incident.id, summary=summary,
                       contributing_factors=factors, impact=impact,
                       action_items=[{"description": "Document root cause"},
                                     {"description": "Add monitoring/alerting gap follow-up"}])
    db.add(draft)
    return draft


async def suggest_responder(db: AsyncSession, incident: Incident) -> Optional[str]:
    """Suggest an IC from the first affected service's team on-call."""
    for sid in (incident.affected_service_ids or []):
        svc = (await db.execute(select(OnCallService).where(OnCallService.id == sid))).scalar_one_or_none()
        if svc is None:
            continue
        sch = (await db.execute(
            select(Schedule).where(Schedule.tenant_id == incident.tenant_id, Schedule.is_active.is_(True))
        )).scalars().first()
        if sch is not None:
            res = await oncall_service.who_is_on_call(db, sch)
            if res["primary_user_id"]:
                return res["primary_user_id"]
    return None
