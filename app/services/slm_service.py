"""Service Level Management (SLM) — priority-ordered SLA matching (Phase 7 / S7.1).

Pure, DB-free matching logic (unit-testable) plus a thin DB helper. The clock/
pause math stays in ``sla_service.SLAService``; this module only decides *which*
agreement applies to a ticket, and exposes the effective start/stop events for a
target (the §10 parity tweak).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any, Iterable, Optional
from uuid import UUID
from zoneinfo import ZoneInfo

from app.models.sla import METRIC_DEFAULT_EVENTS


def _parse_hhmm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


def add_working_minutes_cw(start: datetime, minutes: int, cw: Any | None) -> datetime:
    """Advance ``start`` by ``minutes`` of working time over a CoverageWindow.

    Generalises the business-hours math to a coverage window that may be 24x7,
    or carry multiple daily windows (split shifts). ``cw is None`` or ``is_247``
    → plain wall-clock addition. All inputs/outputs are timezone-aware UTC-safe;
    intermediate math is done in the window's timezone. Never calls now().
    """
    if minutes <= 0:
        return start
    if cw is None or getattr(cw, "is_247", False) or not (cw.windows or []):
        return start + timedelta(minutes=minutes)

    tz = ZoneInfo(cw.timezone or "UTC")
    current = (start.replace(tzinfo=ZoneInfo("UTC")) if start.tzinfo is None else start).astimezone(tz)

    work_days: set[int] = set(cw.work_days or [1, 2, 3, 4, 5])
    holidays: set[date] = set(cw.holidays or [])
    # Sorted (start, end) time pairs for a working day.
    slots = sorted(
        (_parse_hhmm(w["start"]), _parse_hhmm(w["end"]))
        for w in (cw.windows or [])
    )

    remaining = minutes
    guard = 0
    while remaining > 0:
        guard += 1
        if guard > 100000:  # safety: never spin forever on a misconfigured window
            return current
        if current.isoweekday() not in work_days or current.date() in holidays:
            current = (current + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            continue
        advanced = False
        for s, e in slots:
            slot_start = current.replace(hour=s.hour, minute=s.minute, second=0, microsecond=0)
            slot_end = current.replace(hour=e.hour, minute=e.minute, second=0, microsecond=0)
            if current >= slot_end:
                continue
            if current < slot_start:
                current = slot_start
            avail = int((slot_end - current).total_seconds() // 60)
            if remaining <= avail:
                current = current + timedelta(minutes=remaining)
                remaining = 0
                advanced = True
                break
            remaining -= avail
            current = slot_end
            advanced = True
        if remaining > 0 and (not advanced or current >= current.replace(
                hour=slots[-1][1].hour, minute=slots[-1][1].minute, second=0, microsecond=0)):
            # Past the last slot for today — jump to the next day.
            current = (current + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)

    return current.astimezone(ZoneInfo("UTC"))

# Condition keys an sla_rule may constrain on. A rule matches when EVERY key it
# specifies (non-null) equals the ticket's corresponding attribute. `tag` matches
# by membership when the ticket carries a list of tags.
_LIST_MEMBERSHIP_KEYS = {"tag"}


def conditions_match(conditions: dict[str, Any], ctx: dict[str, Any]) -> bool:
    """True if all non-null conditions are satisfied by the ticket context.

    An empty conditions dict matches everything (a catch-all rule). Values are
    compared as-is; for `tag`, the condition matches if it's present in the
    ticket's `tag` list (or equals a scalar `tag`).
    """
    for key, want in conditions.items():
        if want is None:
            continue
        have = ctx.get(key)
        if key in _LIST_MEMBERSHIP_KEYS and isinstance(have, (list, tuple, set)):
            if want not in have:
                return False
        elif str(have) != str(want):
            return False
    return True


def match_rule(rules: Iterable[Any], ctx: dict[str, Any]) -> Optional[Any]:
    """First active rule (lowest ``position``) whose conditions match, else None.

    ``rules`` are SLARule-like objects with ``position``, ``is_active``,
    ``conditions`` (dict) and ``agreement_id``.
    """
    for rule in sorted((r for r in rules if getattr(r, "is_active", True)),
                        key=lambda r: r.position):
        if conditions_match(rule.conditions or {}, ctx):
            return rule
    return None


def match_agreement_id(rules: Iterable[Any], ctx: dict[str, Any]) -> Optional[UUID]:
    """Convenience: the matched rule's ``agreement_id`` (or None)."""
    rule = match_rule(rules, ctx)
    return rule.agreement_id if rule is not None else None


def effective_events(target: Any) -> tuple[str, str]:
    """(start_event, stop_event) for a target — explicit override, else the
    metric's default (§10 parity tweak: configurable start/stop per target)."""
    default_start, default_stop = METRIC_DEFAULT_EVENTS.get(
        target.metric, ("ticket_created", "resolved")
    )
    return (target.start_event or default_start, target.stop_event or default_stop)


def match_explanation(rules: list[Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Machine-readable "why this SLA" trace (spec §3.3.2 / A.2.2)."""
    evaluated: list[dict[str, Any]] = []
    matched = None
    for rule in sorted((r for r in rules if getattr(r, "is_active", True)),
                       key=lambda r: r.position):
        ok = conditions_match(rule.conditions or {}, ctx)
        evaluated.append({
            "rule_id": str(rule.id),
            "position": rule.position,
            "matched": ok,
            "conditions": rule.conditions or {},
        })
        if ok and matched is None:
            matched = rule
        if ok:
            break
    return {
        "matched_by": "rule" if matched else None,
        "matched_rule_id": str(matched.id) if matched else None,
        "agreement_id": str(matched.agreement_id) if matched else None,
        "evaluated_rules": evaluated,
    }
