"""Unit tests for app.services.rca_policy_engine — pure logic, no DB/HTTP."""
import pytest

from app.services.rca_policy_engine import PolicyContext, _match


def _ctx(**over) -> PolicyContext:
    return PolicyContext(**over)


@pytest.mark.parametrize("op,ctx_val,cond_val,expected", [
    ("eq", "critical", "critical", True),
    ("eq", "critical", "high", False),
    ("neq", "critical", "high", True),
    ("neq", "critical", "critical", False),
    ("in", "critical", ["critical", "high"], True),
    ("in", "low", ["critical", "high"], False),
    ("gte", 5, 3, True),
    ("gte", 2, 3, False),
    ("lte", 2, 3, True),
    ("lte", 5, 3, False),
    ("gt", 5, 3, True),
    ("gt", 3, 3, False),
    ("lt", 2, 3, True),
    ("lt", 3, 3, False),
])
def test_single_condition_operators(op, ctx_val, cond_val, expected):
    ctx = _ctx(priority=ctx_val if isinstance(ctx_val, str) else None,
               repeat_count_window=ctx_val if isinstance(ctx_val, int) else 0)
    field_name = "priority" if isinstance(ctx_val, str) else "repeat_count_window"
    conditions = [{"field": field_name, "op": op, "value": cond_val}]
    assert _match(conditions, ctx) is expected


def test_exists_operator():
    ctx = _ctx(service_id=None)
    assert _match([{"field": "service_id", "op": "exists", "value": False}], ctx) is True
    assert _match([{"field": "service_id", "op": "exists", "value": True}], ctx) is False


def test_and_combined_conditions_all_must_match():
    ctx = _ctx(ticket_type="incident", priority="critical", sla_breached=True)
    conditions = [
        {"field": "ticket_type", "op": "eq", "value": "incident"},
        {"field": "priority", "op": "in", "value": ["critical", "high"]},
        {"field": "sla_breached", "op": "eq", "value": True},
    ]
    assert _match(conditions, ctx) is True

    conditions_with_miss = conditions + [{"field": "priority", "op": "eq", "value": "low"}]
    assert _match(conditions_with_miss, ctx) is False


def test_empty_conditions_never_match():
    assert _match([], _ctx()) is False


def test_unknown_operator_never_matches():
    ctx = _ctx(priority="critical")
    assert _match([{"field": "priority", "op": "nonexistent_op", "value": "critical"}], ctx) is False


def test_no_eval_used_conditions_cannot_execute_arbitrary_code():
    """A condition dict with an attempted code-injection payload as the value
    must be treated as inert data, never executed."""
    ctx = _ctx(priority="critical")
    malicious = [{"field": "priority", "op": "eq", "value": "__import__('os').system('echo pwned')"}]
    # value is just a string compared with ==, not evaluated — no match, no side effect
    assert _match(malicious, ctx) is False


@pytest.mark.asyncio
async def test_default_trigger_table_sev1():
    from app.services import rca_policy_engine
    decision = rca_policy_engine._default_decision(_ctx(severity_rank=1))
    assert decision is not None and decision.required is True


@pytest.mark.asyncio
async def test_default_trigger_table_sla_breach_critical():
    from app.services import rca_policy_engine
    decision = rca_policy_engine._default_decision(_ctx(sla_breached=True, priority="critical"))
    assert decision is not None and decision.required is True


@pytest.mark.asyncio
async def test_default_trigger_table_no_match_returns_none():
    from app.services import rca_policy_engine
    decision = rca_policy_engine._default_decision(_ctx(sla_breached=False, priority="low", severity_rank=4))
    assert decision is None


@pytest.mark.asyncio
async def test_default_trigger_table_security_flag():
    from app.services import rca_policy_engine
    decision = rca_policy_engine._default_decision(_ctx(security_flag=True))
    assert decision is not None and decision.approver_role == "admin"


@pytest.mark.asyncio
async def test_default_trigger_table_manual_override():
    from app.services import rca_policy_engine
    decision = rca_policy_engine._default_decision(_ctx(manual_override=True))
    assert decision is not None and decision.required is True


@pytest.mark.asyncio
async def test_default_trigger_table_repeat_count():
    from app.services import rca_policy_engine
    decision = rca_policy_engine._default_decision(_ctx(repeat_count_window=4))
    assert decision is not None and decision.required is True
    assert rca_policy_engine._default_decision(_ctx(repeat_count_window=3)) is None
