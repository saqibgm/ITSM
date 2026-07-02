"""Workflow automation + maintenance suppression (Phase 8 / S8.4).

A declarative engine: on a trigger (alert_created / incident_declared /
severity_changed), find active workflows whose conditions match the event
context and run their ordered actions, logging a WorkflowRun. Kept deliberately
small + safe; dry-run applies no side effects. Lazy imports avoid cycles with
the alert/incident services.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ops import MaintenanceWindow, Workflow, WorkflowRun
from app.services import slm_service


async def is_service_in_maintenance(db: AsyncSession, tenant_id: UUID, service_id: Optional[UUID],
                                    at: Optional[datetime] = None) -> bool:
    """True if a suppressing maintenance window is active for the service now."""
    if service_id is None:
        return False
    at = at or datetime.now(timezone.utc)
    rows = (await db.execute(
        select(MaintenanceWindow).where(
            MaintenanceWindow.tenant_id == tenant_id,
            MaintenanceWindow.suppress_alerts.is_(True),
            MaintenanceWindow.start_at <= at, MaintenanceWindow.end_at > at,
        )
    )).scalars().all()
    for w in rows:
        if not w.service_ids or service_id in w.service_ids:
            return True
    return False


async def _apply_action(db: AsyncSession, action: dict, *, incident=None, dry_run: bool) -> dict:
    kind = action.get("type")
    if kind == "annotate":
        return {"type": kind, "status": "ok", "note": action.get("note")}
    if kind == "notify":
        # Delivery abstracted (S8.4) — records the intent.
        return {"type": kind, "status": "ok" if not dry_run else "dry_run", "message": action.get("message")}
    if kind == "set_incident_severity":
        if incident is None:
            return {"type": kind, "status": "skipped", "reason": "no incident"}
        if not dry_run and action.get("severity_id"):
            incident.severity_id = UUID(action["severity_id"])
        return {"type": kind, "status": "ok" if not dry_run else "dry_run"}
    if kind == "post_incident_status_update":
        if incident is None:
            return {"type": kind, "status": "skipped", "reason": "no incident"}
        if not dry_run:
            from app.services import incident_service
            await incident_service.post_status_update(
                db, incident, None, body=action.get("body", "Automated update"),
                audience=action.get("audience", "internal"))
        return {"type": kind, "status": "ok" if not dry_run else "dry_run"}
    return {"type": kind, "status": "skipped", "reason": "unknown action"}


async def run_workflows(db: AsyncSession, tenant_id: UUID, trigger: str, context: dict, *,
                        incident=None, alert=None, dry_run: bool = False) -> list[dict]:
    """Run all active workflows for a trigger whose conditions match. Returns a
    per-workflow run summary; logs a WorkflowRun row for each (unless dry_run)."""
    workflows = (await db.execute(
        select(Workflow).where(
            Workflow.tenant_id == tenant_id, Workflow.trigger == trigger, Workflow.is_active.is_(True)
        )
    )).scalars().all()

    summaries: list[dict] = []
    for wf in workflows:
        if not slm_service.conditions_match(wf.conditions or {}, context):
            continue
        action_results = []
        ok = True
        for action in (wf.actions or []):
            try:
                res = await _apply_action(db, action, incident=incident, dry_run=dry_run)
            except Exception as exc:  # never let one action break the chain
                res = {"type": action.get("type"), "status": "failed", "error": str(exc)}
            action_results.append(res)
            if res.get("status") == "failed":
                ok = False
        status = "dry_run" if dry_run else ("success" if ok else "partial")
        result = {"workflow": wf.name, "actions": action_results}
        if not dry_run:
            db.add(WorkflowRun(
                workflow_id=wf.id,
                incident_id=incident.id if incident is not None else None,
                alert_id=alert.id if alert is not None else None,
                status=status, result=result,
            ))
        summaries.append({"workflow_id": str(wf.id), "status": status, **result})
    return summaries
