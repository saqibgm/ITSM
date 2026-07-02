"""On-call resolution (Phase 8 / S8.1).

Pure rotation math (unit-testable) + a DB helper that resolves "who is on call"
for a schedule at a time T, applying overrides. Computed on the fly; a
materialised cache is a later optimisation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.oncall import ROTATION_DEFAULT_HOURS, Schedule, ScheduleLayer, ScheduleOverride


def rotation_hours(schedule: Any) -> float:
    """Effective rotation length in hours for a schedule."""
    if schedule.rotation_length_hours:
        return float(schedule.rotation_length_hours)
    return float(ROTATION_DEFAULT_HOURS.get(schedule.rotation_type, 168))


def participant_at(participants: list, anchor: datetime, hours: float, at: datetime) -> Optional[UUID]:
    """The on-call participant for a rotation at time ``at``.

    index = floor((at - anchor) / rotation_length) mod N. Before ``anchor`` (or
    empty roster) behaves gracefully (index 0 / None).
    """
    if not participants:
        return None
    if hours <= 0:
        return participants[0]
    elapsed_h = (at - anchor).total_seconds() / 3600.0
    idx = int(elapsed_h // hours)
    if idx < 0:
        idx = 0
    return participants[idx % len(participants)]


def resolve_layers(schedule: Any, layers: Iterable[Any], overrides: Iterable[Any],
                   at: datetime) -> list[dict]:
    """Resolve each layer's on-call user at ``at``. An active override replaces
    the PRIMARY (rank-1) layer's user (highest precedence)."""
    anchor = schedule.start_at or schedule.created_at
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=timezone.utc)
    hours = rotation_hours(schedule)

    override_user = None
    for ov in overrides:
        s = ov.start_at.replace(tzinfo=timezone.utc) if ov.start_at.tzinfo is None else ov.start_at
        e = ov.end_at.replace(tzinfo=timezone.utc) if ov.end_at.tzinfo is None else ov.end_at
        if s <= at < e:
            override_user = ov.user_id
            break

    out: list[dict] = []
    for layer in sorted(layers, key=lambda l: l.layer_rank):
        user = participant_at(list(layer.participants or []), anchor, hours, at)
        if layer.layer_rank == 1 and override_user is not None:
            user = override_user
        out.append({"layer_rank": layer.layer_rank, "user_id": str(user) if user else None,
                    "overridden": layer.layer_rank == 1 and override_user is not None})
    return out


async def who_is_on_call(db: AsyncSession, schedule: Schedule, at: Optional[datetime] = None) -> dict:
    """Resolve on-call for a schedule at ``at`` (default now, UTC)."""
    at = at or datetime.now(timezone.utc)
    layers = (await db.execute(
        select(ScheduleLayer).where(ScheduleLayer.schedule_id == schedule.id)
    )).scalars().all()
    overrides = (await db.execute(
        select(ScheduleOverride).where(
            ScheduleOverride.schedule_id == schedule.id,
            ScheduleOverride.start_at <= at, ScheduleOverride.end_at > at,
        )
    )).scalars().all()
    resolved = resolve_layers(schedule, layers, overrides, at)
    primary = next((r["user_id"] for r in resolved if r["layer_rank"] == 1), None)
    return {"schedule_id": str(schedule.id), "at": at.isoformat(),
            "on_call": resolved, "primary_user_id": primary}
