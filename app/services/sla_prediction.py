"""SLA breach prediction (Phase 7 / S7.3 tail).

A transparent heuristic scorer (``model_version = heuristic-v1``): risk = fraction
of the budget already consumed by a running clock. Stored in ai_sla_predictions
with a human reason; HITL ground truth (``actual_breached``) is set later by the
breach scan, so a real model can be trained on the gold set. Swappable — bump
model_version when a learned model replaces the heuristic.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.sla import AISLAPrediction, SLAInstance

RISK_FLAG_THRESHOLD = 0.7  # spec A.2.4: risk > 0.7 → flag / workflow


async def score_open_instances(db: AsyncSession) -> int:
    """Score every running (unpaused) instance and append a prediction row."""
    rows = (await db.execute(
        select(
            SLAInstance.id, SLAInstance.tenant_id, SLAInstance.ticket_id,
            func.extract("epoch", func.now() - SLAInstance.created_at).label("elapsed"),
            func.extract("epoch", SLAInstance.due_at - SLAInstance.created_at).label("total"),
        ).where(SLAInstance.status == "running", SLAInstance.paused_at.is_(None))
    )).all()

    n = 0
    for iid, tid, ticket_id, elapsed, total in rows:
        if not total or float(total) <= 0:
            continue
        consumed = max(0.0, float(elapsed) / float(total))
        risk = round(min(1.0, consumed), 3)
        db.add(AISLAPrediction(
            tenant_id=tid, ticket_id=ticket_id, instance_id=iid,
            breach_risk=risk, reason=f"{int(consumed * 100)}% of budget consumed",
        ))
        n += 1
    return n
