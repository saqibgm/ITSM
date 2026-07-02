"""On-call foundation (Phase 8 / S8.1) — services, severity, schedules, layers, overrides.

Revision ID: 0027_oncall_schedules
Revises: 0026_ai_sla_predictions
Create Date: 2026-07-02

Tenant-scoped tables (oncall_services, severity_levels, schedules) get fail-open
RLS like 0014; schedule_layers / schedule_overrides are isolated via their
schedule FK (no own tenant_id / RLS), like sla_events.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0027_oncall_schedules"
down_revision = "0026_ai_sla_predictions"
branch_labels = None
depends_on = None

_PREDICATE = """(
    current_setting('app.tenant_id', true) IS NULL
    OR current_setting('app.tenant_id', true) = ''
    OR current_setting('app.bypass_rls', true) = 'on'
    OR tenant_id = current_setting('app.tenant_id', true)::uuid
)"""

_RLS_TABLES = ["oncall_services", "severity_levels", "schedules"]


def _ts():
    return (
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def upgrade() -> None:
    op.create_table(
        "oncall_services",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.VARCHAR(255), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("asset_id", sa.UUID(as_uuid=True), sa.ForeignKey("assets.id", ondelete="SET NULL"), nullable=True),
        sa.Column("escalation_policy_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("current_state", sa.VARCHAR(20), nullable=False, server_default=sa.text("'operational'")),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        *_ts(),
    )
    op.create_index("ix_oncall_services_tenant_id", "oncall_services", ["tenant_id"])

    op.create_table(
        "severity_levels",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.VARCHAR(40), nullable=False),
        sa.Column("rank", sa.Integer, nullable=False),
        sa.Column("auto_page", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("required_roles", sa.ARRAY(sa.VARCHAR(20)), nullable=True),
        sa.Column("default_agreement_id", sa.UUID(as_uuid=True), sa.ForeignKey("sla_agreements.id", ondelete="SET NULL"), nullable=True),
        sa.Column("impact_urgency_map", postgresql.JSONB, nullable=True),
        *_ts(),
    )
    op.create_index("ix_severity_levels_tenant_id", "severity_levels", ["tenant_id"])

    op.create_table(
        "schedules",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.VARCHAR(255), nullable=False),
        sa.Column("team_id", sa.UUID(as_uuid=True), sa.ForeignKey("teams.id", ondelete="SET NULL"), nullable=True),
        sa.Column("timezone", sa.VARCHAR(64), nullable=False, server_default=sa.text("'UTC'")),
        sa.Column("rotation_type", sa.VARCHAR(20), nullable=False, server_default=sa.text("'weekly'")),
        sa.Column("rotation_length_hours", sa.Integer, nullable=True),
        sa.Column("handoff_time", sa.Time, nullable=True),
        sa.Column("start_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        *_ts(),
    )
    op.create_index("ix_schedules_tenant_id", "schedules", ["tenant_id"])
    op.create_index("ix_schedules_team_id", "schedules", ["team_id"])

    op.create_table(
        "schedule_layers",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("schedule_id", sa.UUID(as_uuid=True), sa.ForeignKey("schedules.id", ondelete="CASCADE"), nullable=False),
        sa.Column("layer_rank", sa.Integer, nullable=False),
        sa.Column("participants", sa.ARRAY(sa.UUID(as_uuid=True)), nullable=False, server_default=sa.text("'{}'")),
        *_ts(),
    )
    op.create_index("ix_schedule_layers_schedule", "schedule_layers", ["schedule_id"])

    op.create_table(
        "schedule_overrides",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("schedule_id", sa.UUID(as_uuid=True), sa.ForeignKey("schedules.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("start_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("end_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("origin", sa.VARCHAR(10), nullable=False, server_default=sa.text("'manual'")),
        sa.Column("created_by", sa.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_schedule_overrides_schedule", "schedule_overrides", ["schedule_id"])

    for t in _RLS_TABLES:
        op.execute(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_isolation ON {t} USING {_PREDICATE} WITH CHECK {_PREDICATE}")


def downgrade() -> None:
    op.drop_table("schedule_overrides")
    op.drop_table("schedule_layers")
    op.drop_table("schedules")
    op.drop_table("severity_levels")
    op.drop_table("oncall_services")
