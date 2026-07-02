"""Status page, maintenance windows & workflows (Phase 8 / S8.4).

Revision ID: 0030_ops
Revises: 0029_incidents
Create Date: 2026-07-02

Tenant-scoped tables get fail-open RLS; workflow_runs is FK-isolated via workflow.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0030_ops"
down_revision = "0029_incidents"
branch_labels = None
depends_on = None

_PREDICATE = """(
    current_setting('app.tenant_id', true) IS NULL
    OR current_setting('app.tenant_id', true) = ''
    OR current_setting('app.bypass_rls', true) = 'on'
    OR tenant_id = current_setting('app.tenant_id', true)::uuid
)"""

_RLS = ["status_page_config", "status_page_subscriptions", "maintenance_windows", "workflows"]


def _ts():
    return (
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def upgrade() -> None:
    op.create_table(
        "status_page_config",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("slug", sa.VARCHAR(80), nullable=False, unique=True),
        sa.Column("custom_domain", sa.VARCHAR(255), nullable=True),
        sa.Column("is_public", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("branding", postgresql.JSONB, nullable=True),
        *_ts(),
    )

    op.create_table(
        "status_page_subscriptions",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("channel", sa.VARCHAR(10), nullable=False),
        sa.Column("value", sa.VARCHAR(255), nullable=False),
        sa.Column("service_ids", sa.ARRAY(sa.UUID(as_uuid=True)), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_status_page_subs_tenant", "status_page_subscriptions", ["tenant_id"])

    op.create_table(
        "maintenance_windows",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.VARCHAR(255), nullable=False),
        sa.Column("service_ids", sa.ARRAY(sa.UUID(as_uuid=True)), nullable=True),
        sa.Column("start_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("end_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("suppress_alerts", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("created_by", sa.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        *_ts(),
    )
    op.create_index("ix_maintenance_windows_tenant_window", "maintenance_windows", ["tenant_id", "start_at", "end_at"])

    op.create_table(
        "workflows",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.VARCHAR(255), nullable=False),
        sa.Column("trigger", sa.VARCHAR(40), nullable=False),
        sa.Column("conditions", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("actions", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("version", sa.Integer, nullable=False, server_default=sa.text("1")),
        *_ts(),
    )
    op.create_index("ix_workflows_tenant_trigger", "workflows", ["tenant_id", "trigger", "is_active"])

    op.create_table(
        "workflow_runs",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("workflow_id", sa.UUID(as_uuid=True), sa.ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False),
        sa.Column("incident_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("alert_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.VARCHAR(10), nullable=False),
        sa.Column("result", postgresql.JSONB, nullable=True),
        sa.Column("at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_workflow_runs_workflow", "workflow_runs", ["workflow_id"])

    for t in _RLS:
        op.execute(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_isolation ON {t} USING {_PREDICATE} WITH CHECK {_PREDICATE}")


def downgrade() -> None:
    op.drop_table("workflow_runs")
    op.drop_table("workflows")
    op.drop_table("maintenance_windows")
    op.drop_table("status_page_subscriptions")
    op.drop_table("status_page_config")
