"""SLO / error-budget engine (Phase 9).

Pure math (compute_status, burn_rate) is separated from DB access so it's unit
testable. Budget/SLI/burn are always *derived* from slo_measurements, never
stored. Burn alerts fire through Module B (alerting_service.ingest_alert).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.slo import (
    BURN_DEFAULTS, SLOBurnAlert, SLOMeasurement, SLOObjective, SLISource, WINDOW_DAYS,
)


# ---------------------------------------------------------------------------
# Pure math
# ---------------------------------------------------------------------------


def compute_status(target_pct: float, window_days: int, good: int, total: int) -> dict:
    """Derive SLI %, error budget and consumption from window totals.

    burn/ETA come from :func:`burn_rate` over short windows, not here.
    """
    target = float(target_pct)
    allowed_error = max(0.0, 1.0 - target / 100.0)          # e.g. 0.001 for 99.9%
    if total <= 0:
        return {
            "sli_pct": None, "target_pct": round(target, 3), "window_days": window_days,
            "total_events": 0, "good_events": 0, "bad_events": 0,
            "error_budget_events": 0, "budget_consumed_pct": None,
            "budget_remaining_pct": None, "meeting": None,
        }
    bad = total - good
    sli_pct = good / total * 100.0
    budget_events = total * allowed_error
    consumed_pct = (bad / budget_events * 100.0) if budget_events > 0 else (0.0 if bad == 0 else 100.0)
    remaining_pct = 100.0 - consumed_pct
    return {
        "sli_pct": round(sli_pct, 4),
        "target_pct": round(target, 3),
        "window_days": window_days,
        "total_events": int(total),
        "good_events": int(good),
        "bad_events": int(bad),
        "error_budget_events": round(budget_events, 2),
        "budget_consumed_pct": round(consumed_pct, 2),
        "budget_remaining_pct": round(remaining_pct, 2),
        "meeting": sli_pct >= target,
    }


def burn_rate(target_pct: float, good: int, total: int) -> Optional[float]:
    """observed_error / allowed_error. 1.0 = on pace to spend the budget exactly
    at window end; 14.4 = 28-day budget gone in ~2 days."""
    allowed_error = max(0.0, 1.0 - float(target_pct) / 100.0)
    if total <= 0 or allowed_error <= 0:
        return None
    observed_error = (total - good) / total
    return round(observed_error / allowed_error, 3)


def eta_hours_to_exhaustion(window_days: int, remaining_pct: Optional[float],
                            current_burn: Optional[float]) -> Optional[float]:
    """How long the remaining budget lasts at the current burn rate."""
    if not remaining_pct or remaining_pct <= 0 or not current_burn or current_burn <= 0:
        return None
    window_hours = window_days * 24
    return round(window_hours * (remaining_pct / 100.0) / current_burn, 1)


# ---------------------------------------------------------------------------
# DB rollups
# ---------------------------------------------------------------------------


async def window_totals(db: AsyncSession, slo_id: UUID, since: datetime) -> tuple[int, int]:
    row = (await db.execute(
        select(
            func.coalesce(func.sum(SLOMeasurement.good_count), 0),
            func.coalesce(func.sum(SLOMeasurement.total_count), 0),
        ).where(SLOMeasurement.slo_id == slo_id, SLOMeasurement.bucket_start >= since)
    )).one()
    return int(row[0]), int(row[1])


async def objective_status(db: AsyncSession, slo: SLOObjective) -> dict:
    days = WINDOW_DAYS.get(slo.window, 28)
    now = datetime.now(timezone.utc)
    good, total = await window_totals(db, slo.id, now - timedelta(days=days))
    status = compute_status(float(slo.target_pct), days, good, total)
    # short-window (1h) burn for the "how fast am I spending" headline
    g1, t1 = await window_totals(db, slo.id, now - timedelta(hours=1))
    br = burn_rate(float(slo.target_pct), g1, t1)
    status["burn_rate_1h"] = br
    status["eta_hours"] = eta_hours_to_exhaustion(days, status.get("budget_remaining_pct"), br)
    return status


# ---------------------------------------------------------------------------
# Internal SLI sampling — the "free win" adapters (no external monitor needed)
# ---------------------------------------------------------------------------


async def sample_internal_sli(db: AsyncSession, source: SLISource, service_id: UUID) -> tuple[int, int]:
    """Return (good, total) for one bucket from data we already own.

    metric config: {"metric": "ticket_sla_compliance" | "incident_free" | "uptime"}.
    Falls back to a healthy sample so a mis-config never crashes the beat.
    """
    metric = (source.config or {}).get("metric", "uptime")

    if metric == "ticket_sla_compliance":
        from app.models.sla import SLAInstance
        rows = (await db.execute(
            select(SLAInstance.status, func.count()).where(
                SLAInstance.tenant_id == source.tenant_id,
                SLAInstance.status.in_(("met", "breached")),
            ).group_by(SLAInstance.status)
        )).all()
        counts = {s: c for s, c in rows}
        good = counts.get("met", 0)
        total = good + counts.get("breached", 0)
        return (good, total) if total else (1, 1)

    if metric in ("incident_free", "uptime"):
        # good sample unless the service has an active (unresolved) incident now.
        from app.models.incident import Incident
        active = (await db.execute(
            select(func.count()).select_from(Incident).where(
                Incident.tenant_id == source.tenant_id,
                Incident.status.notin_(("resolved", "closed", "cancelled")),
                Incident.affected_service_ids.any(service_id),
            )
        )).scalar_one()
        return (0, 1) if active else (1, 1)

    return (1, 1)


async def record_measurement(db: AsyncSession, slo_id: UUID, bucket_start: datetime,
                             good: int, total: int) -> None:
    """Idempotent per (slo, bucket) — a re-run of the same bucket overwrites."""
    existing = (await db.execute(
        select(SLOMeasurement).where(
            SLOMeasurement.slo_id == slo_id, SLOMeasurement.bucket_start == bucket_start,
        )
    )).scalar_one_or_none()
    if existing:
        existing.good_count = good
        existing.total_count = total
    else:
        db.add(SLOMeasurement(slo_id=slo_id, bucket_start=bucket_start,
                              good_count=good, total_count=total))


def _floor_bucket(now: datetime, minutes: int = 5) -> datetime:
    return now.replace(second=0, microsecond=0, minute=(now.minute // minutes) * minutes)


# ---------------------------------------------------------------------------
# Burn-alert evaluation — ties into Module B paging
# ---------------------------------------------------------------------------


async def evaluate_burn_alerts(db: AsyncSession, slo: SLOObjective) -> list[str]:
    """For each burn rule on the SLO, compute burn over its short & long windows;
    a rule fires when BOTH exceed the threshold (Google-SRE multi-window). Firing
    raises an alert via Module B (fast_burn pages on-call; slow_burn opens a
    ticket-grade alert). Returns the kinds that transitioned to firing."""
    from app.services import alerting_service

    now = datetime.now(timezone.utc)
    alerts = (await db.execute(
        select(SLOBurnAlert).where(SLOBurnAlert.slo_id == slo.id)
    )).scalars().all()

    fired: list[str] = []
    for rule in alerts:
        gl, tl = await window_totals(db, slo.id, now - timedelta(minutes=rule.long_window_min))
        gs, ts = await window_totals(db, slo.id, now - timedelta(minutes=rule.short_window_min))
        br_long = burn_rate(float(slo.target_pct), gl, tl)
        br_short = burn_rate(float(slo.target_pct), gs, ts)
        threshold = float(rule.burn_threshold)
        breaching = (br_long is not None and br_short is not None
                     and br_long >= threshold and br_short >= threshold)

        if breaching and rule.state != "firing":
            alert = await alerting_service.ingest_alert(
                db, slo.tenant_id,
                dedup_key=f"slo-burn:{rule.id}",
                title=f"{rule.kind.replace('_', ' ').title()}: SLO '{slo.name}' burning at {br_long}x",
                source="slo_burn",
                service_id=slo.service_id,
                payload={"slo_id": str(slo.id), "kind": rule.kind,
                         "burn_long": br_long, "burn_short": br_short, "threshold": threshold},
            )
            rule.state = "firing"
            rule.last_fired_at = now
            rule.linked_alert_id = getattr(alert, "id", None)
            fired.append(rule.kind)
        elif not breaching and rule.state == "firing":
            # auto-clear; resolve the linked alert if still open
            rule.state = "ok"
            if rule.linked_alert_id:
                from app.models.alerting import Alert
                a = (await db.execute(select(Alert).where(Alert.id == rule.linked_alert_id))).scalar_one_or_none()
                if a and a.status != "resolved":
                    a.status = "resolved"
                    a.resolved_at = now
    return fired
