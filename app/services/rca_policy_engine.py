"""RCA policy engine (specs/08, Phase 2).

Tenant-defined `RcaPolicy` rows are evaluated against a `PolicyContext` via a
safe dispatch table — no `eval()` anywhere. First active policy match wins
(ordered by priority). If no policy is configured, a hardcoded default
trigger table (specs/08 §4.2.1) keeps a fresh tenant from being ungoverned.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.retro import RcaPolicy


@dataclass
class PolicyContext:
    ticket_type: Optional[str] = None
    priority: Optional[str] = None
    sla_breached: bool = False
    service_id: Optional[UUID] = None
    team_id: Optional[UUID] = None
    category_id: Optional[UUID] = None
    incident_duration_minutes: Optional[float] = None
    repeat_count_window: int = 0
    recording_exists: bool = False
    security_flag: bool = False
    manual_override: bool = False
    severity_rank: Optional[int] = None  # 1 = most severe (severity_levels.rank)

    def get(self, field_name: str) -> Any:
        return getattr(self, field_name, None)


@dataclass
class PolicyDecision:
    required: bool
    policy_id: Optional[UUID] = None
    owner_role: str = "agent"
    approver_role: str = "manager"
    due_days: int = 3
    required_evidence_types: list[str] = field(default_factory=list)
    required_action_item_count: int = 0
    escalation_policy: Optional[str] = None
    customer_facing_summary_required: bool = False


_OPS = {
    "eq": lambda a, b: a == b,
    "neq": lambda a, b: a != b,
    "in": lambda a, b: a in (b or []),
    "gte": lambda a, b: a is not None and a >= b,
    "lte": lambda a, b: a is not None and a <= b,
    "gt": lambda a, b: a is not None and a > b,
    "lt": lambda a, b: a is not None and a < b,
    "exists": lambda a, b: (a is not None) == bool(b),
}


def _match(conditions: list[dict], ctx: PolicyContext) -> bool:
    """AND-combined list of {field, op, value} conditions. No eval() — every
    operator is a fixed, safe lambda in `_OPS`."""
    if not conditions:
        return False
    for cond in conditions:
        field_name = cond.get("field")
        op = cond.get("op")
        value = cond.get("value")
        fn = _OPS.get(op)
        if fn is None or field_name is None:
            return False
        try:
            if not fn(ctx.get(field_name), value):
                return False
        except TypeError:
            return False
    return True


def _default_decision(ctx: PolicyContext) -> Optional[PolicyDecision]:
    """Hardcoded fallback trigger table (specs/08 §4.2.1) — used only when a
    tenant has zero active rca_policies configured."""
    if ctx.manual_override:
        return PolicyDecision(required=True, due_days=3)
    if ctx.security_flag:
        return PolicyDecision(required=True, due_days=2, approver_role="admin")
    if ctx.severity_rank == 1:  # sev1 / major incident
        return PolicyDecision(required=True, due_days=3)
    if ctx.sla_breached and ctx.priority in ("critical", "high"):
        return PolicyDecision(required=True, due_days=5)
    if ctx.repeat_count_window > 3:
        return PolicyDecision(required=True, due_days=5)
    return None


async def evaluate(db: AsyncSession, tenant_id: UUID, ctx: PolicyContext) -> Optional[PolicyDecision]:
    """Returns None when no policy (configured or default) requires an RCA."""
    policies = (await db.execute(
        select(RcaPolicy)
        .where(RcaPolicy.tenant_id == tenant_id, RcaPolicy.status == "active")
        .order_by(RcaPolicy.priority.asc())
    )).scalars().all()

    for policy in policies:
        if _match(policy.conditions or [], ctx):
            outputs = policy.outputs or {}
            if not outputs.get("required", True):
                return None
            return PolicyDecision(
                required=True,
                policy_id=policy.id,
                owner_role=outputs.get("owner_role", "agent"),
                approver_role=outputs.get("approver_role", "manager"),
                due_days=int(outputs.get("due_days", 3)),
                required_evidence_types=outputs.get("required_evidence_types", []),
                required_action_item_count=int(outputs.get("required_action_item_count", 0)),
                escalation_policy=outputs.get("escalation_policy"),
                customer_facing_summary_required=bool(outputs.get("customer_facing_summary_required", False)),
            )

    if not policies:
        return _default_decision(ctx)
    return None
