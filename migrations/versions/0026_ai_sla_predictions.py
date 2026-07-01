"""AI SLA breach predictions (Phase 7 / S7.3 tail) — ai_sla_predictions.

Revision ID: 0026_ai_sla_predictions
Revises: 0025_sla_metrics_daily
Create Date: 2026-07-01

Breach-risk scores for open instances + HITL ground truth. Tenant-scoped + RLS.
"""

from alembic import op
import sqlalchemy as sa

revision = "0026_ai_sla_predictions"
down_revision = "0025_sla_metrics_daily"
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
        "ai_sla_predictions",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ticket_id", sa.UUID(as_uuid=True), sa.ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("instance_id", sa.UUID(as_uuid=True), sa.ForeignKey("sla_instances.id", ondelete="CASCADE"), nullable=False),
        sa.Column("breach_risk", sa.Float, nullable=False),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column("model_version", sa.VARCHAR(40), nullable=False, server_default=sa.text("'heuristic-v1'")),
        sa.Column("actual_breached", sa.Boolean, nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_ai_sla_predictions_instance", "ai_sla_predictions", ["instance_id"])
    op.create_index("ix_ai_sla_predictions_tenant_created", "ai_sla_predictions", ["tenant_id", "created_at"])

    op.execute("ALTER TABLE ai_sla_predictions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE ai_sla_predictions FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON ai_sla_predictions "
        f"USING {_PREDICATE} WITH CHECK {_PREDICATE}"
    )


def downgrade() -> None:
    op.drop_table("ai_sla_predictions")
