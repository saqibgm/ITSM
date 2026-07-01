"""SLA runtime tracking (Phase 7 / S7.2) — sla_instances + sla_events.

Revision ID: 0024_sla_instances
Revises: 0023_slm_foundation
Create Date: 2026-07-01

Per-ticket, per-target clocks (``sla_instances``, tenant-scoped + fail-open RLS
like 0014) and their immutable event log (``sla_events``, isolated via the
instance FK — no own tenant_id/RLS, like sla_underpinning / ticket_approvals).
Additive: the existing ticket ``sla_*`` fields + breach worker are untouched.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0024_sla_instances"
down_revision = "0023_slm_foundation"
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
        "sla_instances",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ticket_id", sa.UUID(as_uuid=True), sa.ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("target_id", sa.UUID(as_uuid=True), sa.ForeignKey("sla_targets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("agreement_version", sa.Integer, nullable=False, server_default=sa.text("1")),
        sa.Column("due_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("paused_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("paused_duration_sec", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("pause_reason", sa.VARCHAR(60), nullable=True),
        sa.Column("status", sa.VARCHAR(20), nullable=False, server_default=sa.text("'running'")),
        sa.Column("breached_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("attributed_party", postgresql.JSONB, nullable=True),
        sa.Column("warned_pct", sa.ARRAY(sa.Integer), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_sla_instances_tenant_status_due", "sla_instances", ["tenant_id", "status", "due_at"])
    op.create_index("ix_sla_instances_ticket", "sla_instances", ["ticket_id"])

    op.create_table(
        "sla_events",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("instance_id", sa.UUID(as_uuid=True), sa.ForeignKey("sla_instances.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event", sa.VARCHAR(20), nullable=False),
        sa.Column("reason", sa.VARCHAR(255), nullable=True),
        sa.Column("at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_sla_events_instance_at", "sla_events", ["instance_id", "at"])

    op.execute("ALTER TABLE sla_instances ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE sla_instances FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON sla_instances "
        f"USING {_PREDICATE} WITH CHECK {_PREDICATE}"
    )


def downgrade() -> None:
    op.drop_table("sla_events")
    op.drop_table("sla_instances")
