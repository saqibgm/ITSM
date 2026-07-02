"""SLI / SLO / Error-Budget reliability module (Phase 9).

Revision ID: 0033_slo
Revises: 0032_rls_nullif
Create Date: 2026-07-02

Four tables: sli_sources (where a metric comes from), slo_objectives (the target
on an oncall_service), slo_measurements (immutable time-bucketed raw material),
slo_burn_alerts (multi-window burn-rate rules that fire through Module B).

Tenant-scoped tables get fail-open RLS with the NULLIF-guarded predicate (0032).
slo_measurements is FK-isolated via its slo_objective (no own tenant_id) and is
INSERT/immutable in spirit — matching sla_events / incident_timeline.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0033_slo"
down_revision = "0032_rls_nullif"
branch_labels = None
depends_on = None

# NULLIF-guarded fail-open predicate (consistent with migration 0032).
_PREDICATE = """(
    current_setting('app.tenant_id', true) IS NULL
    OR current_setting('app.tenant_id', true) = ''
    OR current_setting('app.bypass_rls', true) = 'on'
    OR tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
)"""


def _rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY tenant_isolation ON {table} USING {_PREDICATE} WITH CHECK {_PREDICATE}")


def upgrade() -> None:
    op.create_table(
        "sli_sources",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.VARCHAR(255), nullable=False),
        sa.Column("type", sa.VARCHAR(30), nullable=False),
        sa.Column("good_query", sa.Text, nullable=True),
        sa.Column("valid_query", sa.Text, nullable=True),
        sa.Column("config", postgresql.JSONB, nullable=True),
        sa.Column("connection", postgresql.JSONB, nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_sli_sources_tenant", "sli_sources", ["tenant_id"])

    op.create_table(
        "slo_objectives",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("service_id", sa.UUID(as_uuid=True), sa.ForeignKey("oncall_services.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sli_source_id", sa.UUID(as_uuid=True), sa.ForeignKey("sli_sources.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("name", sa.VARCHAR(255), nullable=False),
        sa.Column("objective_type", sa.VARCHAR(20), nullable=False, server_default=sa.text("'availability'")),
        sa.Column("target_pct", sa.Numeric(6, 3), nullable=False),
        sa.Column("window", sa.VARCHAR(20), nullable=False, server_default=sa.text("'rolling_28d'")),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_slo_objectives_tenant", "slo_objectives", ["tenant_id"])
    op.create_index("ix_slo_objectives_service", "slo_objectives", ["service_id"])

    op.create_table(
        "slo_measurements",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("slo_id", sa.UUID(as_uuid=True), sa.ForeignKey("slo_objectives.id", ondelete="CASCADE"), nullable=False),
        sa.Column("bucket_start", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("good_count", sa.BigInteger, nullable=False, server_default=sa.text("0")),
        sa.Column("total_count", sa.BigInteger, nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("slo_id", "bucket_start", name="uq_slo_measurements_bucket"),
    )
    op.create_index("ix_slo_measurements_slo_bucket", "slo_measurements", ["slo_id", sa.text("bucket_start DESC")])

    op.create_table(
        "slo_burn_alerts",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("slo_id", sa.UUID(as_uuid=True), sa.ForeignKey("slo_objectives.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.VARCHAR(12), nullable=False),
        sa.Column("short_window_min", sa.Integer, nullable=False, server_default=sa.text("5")),
        sa.Column("long_window_min", sa.Integer, nullable=False, server_default=sa.text("60")),
        sa.Column("burn_threshold", sa.Numeric(8, 3), nullable=False, server_default=sa.text("14.4")),
        sa.Column("state", sa.VARCHAR(10), nullable=False, server_default=sa.text("'ok'")),
        sa.Column("last_fired_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("linked_alert_id", sa.UUID(as_uuid=True), sa.ForeignKey("alerts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_slo_burn_alerts_tenant", "slo_burn_alerts", ["tenant_id"])
    op.create_index("ix_slo_burn_alerts_slo", "slo_burn_alerts", ["slo_id"])

    for t in ("sli_sources", "slo_objectives", "slo_burn_alerts"):
        _rls(t)


def downgrade() -> None:
    op.drop_table("slo_burn_alerts")
    op.drop_table("slo_measurements")
    op.drop_table("slo_objectives")
    op.drop_table("sli_sources")
