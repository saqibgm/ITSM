"""Row-Level Security safety net (defense-in-depth behind app-layer filtering).

Revision ID: 0014_row_level_security
Revises: 0013_schema_hardening
Create Date: 2026-06-08

Adds PostgreSQL RLS to tenant-scoped data tables. The policy is **fail-open**:
when no `app.tenant_id` GUC is set on the session it is permissive (sees all),
so anything that doesn't set the GUC — Celery workers, the IAM webhook, the
test harness, the auth bootstrap itself — is completely unaffected. RLS only
*engages* (restricts) once a request sets `app.tenant_id` (done in
get_current_user for tenant users); platform users set `app.bypass_rls=on`.

This is a second layer behind the existing per-query tenant_id filtering — a
forgotten filter on a tenant request can no longer leak another tenant's rows.

Excluded: auth-bootstrap tables (users, user_itsm_roles — queried before the
GUC is set), platform/system tables (products, itsm_roles, platform_audit_log,
system_logs, tenant_sequences), and the tenants table itself.
"""

from alembic import op
from sqlalchemy import text

revision = "0014_row_level_security"
down_revision = "0013_schema_hardening"
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

# Fail-open: permissive unless app.tenant_id is set to a concrete value.
_PREDICATE = """(
    current_setting('app.tenant_id', true) IS NULL
    OR current_setting('app.tenant_id', true) = ''
    OR current_setting('app.bypass_rls', true) = 'on'
    OR tenant_id IS NULL
    OR tenant_id = current_setting('app.tenant_id', true)::uuid
)"""


def upgrade() -> None:
    for t in _TABLES:
        op.execute(text(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY"))
        # FORCE so the table-owning app role is also subject to the policy
        # (owners bypass RLS by default).
        op.execute(text(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY"))
        op.execute(text(
            f"CREATE POLICY tenant_isolation ON {t} "
            f"USING {_PREDICATE} WITH CHECK {_PREDICATE}"
        ))


def downgrade() -> None:
    for t in _TABLES:
        op.execute(text(f"DROP POLICY IF EXISTS tenant_isolation ON {t}"))
        op.execute(text(f"ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY"))
        op.execute(text(f"ALTER TABLE {t} DISABLE ROW LEVEL SECURITY"))
