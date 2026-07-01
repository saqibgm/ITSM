"""Service Level Management (SLM) — priority-ordered SLA matching (Phase 7 / S7.1).

Pure, DB-free matching logic (unit-testable) plus a thin DB helper. The clock/
pause math stays in ``sla_service.SLAService``; this module only decides *which*
agreement applies to a ticket, and exposes the effective start/stop events for a
target (the §10 parity tweak).
"""

from __future__ import annotations

from typing import Any, Iterable, Optional
from uuid import UUID

from app.models.sla import METRIC_DEFAULT_EVENTS

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
