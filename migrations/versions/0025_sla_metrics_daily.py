"""SLA daily rollup (Phase 7 / S7.3) — sla_metrics_daily.

Revision ID: 0025_sla_metrics_daily
Revises: 0024_sla_instances
Create Date: 2026-07-01

Nightly per-tenant aggregate for fast dashboards. Tenant-scoped + fail-open RLS.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0025_sla_metrics_daily"
down_revision = "0024_sla_instances"
branch_labels = None
depends_on = None

_PREDICATE = """(
    current_setting('app.tenant_id', true) IS NULL
    OR current_setting('app.tenant_id', true) = ''
    OR current_setting('app.bypass_rls', true) = 'on'
    OR tenant_id = current_setting('app.tenant_id', true)::uuid
)"""


def upgrade() -> None:
    op.create_table(
        "sla_metrics_daily",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("date", sa.Date, nullable=False),
        sa.Column("dimension", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("opened_count", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("met_count", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("breached_count", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("total_count", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("p50_resolve_min", sa.Integer, nullable=True),
        sa.Column("p90_resolve_min", sa.Integer, nullable=True),
        sa.Column("p95_resolve_min", sa.Integer, nullable=True),
        sa.Column("p99_resolve_min", sa.Integer, nullable=True),
        sa.UniqueConstraint("tenant_id", "date", "dimension", name="uq_sla_metrics_daily"),
    )
    op.create_index("ix_sla_metrics_daily_tenant_date", "sla_metrics_daily", ["tenant_id", "date"])

    op.execute("ALTER TABLE sla_metrics_daily ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE sla_metrics_daily FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON sla_metrics_daily "
        f"USING {_PREDICATE} WITH CHECK {_PREDICATE}"
    )


def downgrade() -> None:
    op.drop_table("sla_metrics_daily")
