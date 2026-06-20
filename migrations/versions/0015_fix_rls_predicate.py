"""Fix RLS policy predicate — empty-string GUC must not be cast to uuid.

Revision ID: 0015_fix_rls_predicate
Revises: 0014_row_level_security
Create Date: 2026-06-08

0014's predicate cast `current_setting('app.tenant_id', true)::uuid` directly.
get_db clears the GUC to '' (empty string) at request start, and Postgres
evaluates the cast subexpression even when an earlier OR branch is true, so
`''::uuid` raised InvalidTextRepresentation (500) for the non-superuser runtime
role. Wrapping in NULLIF(..., '') makes an unset/empty GUC become NULL (→
fail-open) instead of a bad cast. Superuser test role never hit this (bypasses
RLS), which is why the suite stayed green.
"""

from alembic import op
from sqlalchemy import text

revision = "0015_fix_rls_predicate"
down_revision = "0014_row_level_security"
branch_labels = None
depends_on = None

_TABLES = [
    "tickets", "ticket_categories", "ticket_tags",
    "assets", "asset_categories", "asset_types", "asset_relationships", "vendors",
    "kb_articles", "kb_spaces", "kb_tags",
    "notifications", "notification_preferences",
    "automation_rules", "automation_logs",
    "webhook_endpoints", "webhook_deliveries",
    "sla_policies", "business_hours_config", "teams", "departments",
    "virtual_agent_sessions", "ai_usage_daily",
]

# Fail-open + NULLIF-guarded cast (no ''::uuid).
_PREDICATE = """(
    current_setting('app.bypass_rls', true) = 'on'
    OR nullif(current_setting('app.tenant_id', true), '') IS NULL
    OR tenant_id IS NULL
    OR tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid
)"""

_OLD_PREDICATE = """(
    current_setting('app.tenant_id', true) IS NULL
    OR current_setting('app.tenant_id', true) = ''
    OR current_setting('app.bypass_rls', true) = 'on'
    OR tenant_id IS NULL
    OR tenant_id = current_setting('app.tenant_id', true)::uuid
)"""


def _recreate(predicate: str) -> None:
    for t in _TABLES:
        op.execute(text(f"DROP POLICY IF EXISTS tenant_isolation ON {t}"))
        op.execute(text(
            f"CREATE POLICY tenant_isolation ON {t} "
            f"USING {predicate} WITH CHECK {predicate}"
        ))


def upgrade() -> None:
    _recreate(_PREDICATE)


def downgrade() -> None:
    _recreate(_OLD_PREDICATE)
