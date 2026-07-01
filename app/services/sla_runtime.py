"""SLA runtime engine (Phase 7 / S7.2).

Opens/updates per-ticket ``sla_instances`` and writes the immutable ``sla_events``
log. DB-time only for pause/resume/breach; ``due_at`` is precomputed with the
coverage-window math in ``slm_service``. Breach attribution walks the
``sla_underpinning`` chain to blame an OLA team or UC vendor.
"""

from __future__ import annotations

from typing import Optional
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.sla import (
    CoverageWindow, SLAAgreement, SLAEvent, SLAInstance, SLATarget, SLAUnderpinning,
)
from app.services import slm_service

_ACTIVE = ("running", "paused")


async def _add_event(db: AsyncSession, instance_id: UUID, event: str, reason: Optional[str] = None) -> None:
    db.add(SLAEvent(instance_id=instance_id, event=event, reason=reason))


async def open_instances(db: AsyncSession, ticket, agreement: SLAAgreement) -> list[SLAInstance]:
    """Open a running instance per active target of ``agreement`` on ``ticket``.

    Cancels any currently active instances first (so an override/re-match starts
    a clean set). ``due_at`` = ticket.created_at + target.duration over the
    target's coverage window (24x7 when none). Freezes the agreement version.
    """
    # Cancel existing active instances for this ticket.
    existing = (await db.execute(
        select(SLAInstance).where(
            SLAInstance.ticket_id == ticket.id, SLAInstance.status.in_(_ACTIVE)
        )
    )).scalars().all()
    for inst in existing:
        inst.status = "cancelled"
        await _add_event(db, inst.id, "cancelled", "superseded")

    targets = (await db.execute(
        select(SLATarget).where(SLATarget.agreement_id == agreement.id)
    )).scalars().all()

    # Preload the coverage windows the targets use.
    cw_ids = {t.coverage_window_id for t in targets if t.coverage_window_id}
    cw_map: dict[UUID, CoverageWindow] = {}
    if cw_ids:
        for cw in (await db.execute(
            select(CoverageWindow).where(CoverageWindow.id.in_(cw_ids))
        )).scalars().all():
            cw_map[cw.id] = cw

    opened: list[SLAInstance] = []
    for t in targets:
        cw = cw_map.get(t.coverage_window_id) if t.coverage_window_id else None
        due_at = slm_service.add_working_minutes_cw(ticket.created_at, t.duration_minutes, cw)
        inst = SLAInstance(
            tenant_id=ticket.tenant_id, ticket_id=ticket.id, target_id=t.id,
            agreement_version=agreement.version, due_at=due_at, status="running",
        )
        db.add(inst)
        await db.flush()  # populate inst.id for the event FK
        await _add_event(db, inst.id, "started")
        opened.append(inst)
    return opened


async def pause_ticket_instances(db: AsyncSession, ticket_id: UUID, reason: str) -> int:
    """Pause all running instances for a ticket (server-side; records event)."""
    ids = (await db.execute(
        select(SLAInstance.id).where(
            SLAInstance.ticket_id == ticket_id, SLAInstance.status == "running"
        )
    )).scalars().all()
    if not ids:
        return 0
    await db.execute(
        update(SLAInstance).where(SLAInstance.id.in_(ids)).values(
            status="paused", paused_at=func.now(), pause_reason=reason,
        )
    )
    for iid in ids:
        await _add_event(db, iid, "paused", reason)
    return len(ids)


async def resume_ticket_instances(db: AsyncSession, ticket_id: UUID) -> int:
    """Resume paused instances: accrue paused seconds + push due_at forward.

    All time math is server-side (references the columns, uses NOW()), so no
    naive/aware Python datetime ever enters the expression.
    """
    ids = (await db.execute(
        select(SLAInstance.id).where(
            SLAInstance.ticket_id == ticket_id, SLAInstance.status == "paused"
        )
    )).scalars().all()
    if not ids:
        return 0
    elapsed = func.extract("epoch", func.now() - SLAInstance.paused_at)
    await db.execute(
        update(SLAInstance).where(SLAInstance.id.in_(ids)).values(
            paused_duration_sec=func.coalesce(SLAInstance.paused_duration_sec, 0) + elapsed,
            # make_interval(years, months, weeks, days, hours, mins, secs) — positional
            due_at=SLAInstance.due_at + func.make_interval(0, 0, 0, 0, 0, 0, elapsed),
            status="running", paused_at=None, pause_reason=None,
        )
    )
    for iid in ids:
        await _add_event(db, iid, "resumed")
    return len(ids)


async def stop_instance(db: AsyncSession, instance: SLAInstance, met: bool) -> None:
    """Stop a clock permanently (met = reached its goal, else just stopped)."""
    instance.status = "met" if met else "stopped"
    await _add_event(db, instance.id, "met" if met else "stopped")


async def attribute_breach(db: AsyncSession, instance: SLAInstance) -> Optional[dict]:
    """On breach of an SLA target, blame a breached underpinning OLA/UC.

    Returns ``{"kind": "team"|"vendor", "id": "<uuid>"}`` or None. Sets it on
    the instance too. An OLA's support agreement carries ``owner_team_id``; a
    UC's carries ``vendor_id``.
    """
    support_ids = (await db.execute(
        select(SLAUnderpinning.support_target_id).where(
            SLAUnderpinning.parent_target_id == instance.target_id
        )
    )).scalars().all()
    if not support_ids:
        return None

    # Which supporting targets, on the SAME ticket, also breached?
    breached = (await db.execute(
        select(SLAInstance.target_id).where(
            SLAInstance.ticket_id == instance.ticket_id,
            SLAInstance.target_id.in_(support_ids),
            SLAInstance.status == "breached",
        )
    )).scalars().all()
    if not breached:
        return None

    # Resolve the supporting target's agreement to name the responsible party.
    ag = (await db.execute(
        select(SLAAgreement).join(SLATarget, SLATarget.agreement_id == SLAAgreement.id)
        .where(SLATarget.id.in_(breached))
    )).scalars().first()
    if ag is None:
        return None
    if ag.kind == "ola" and ag.owner_team_id:
        party = {"kind": "team", "id": str(ag.owner_team_id)}
    elif ag.kind == "uc" and ag.vendor_id:
        party = {"kind": "vendor", "id": str(ag.vendor_id)}
    else:
        return None
    instance.attributed_party = party
    return party
