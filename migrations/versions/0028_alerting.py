"""Alerting, escalation & paging (Phase 8 / S8.2).

Revision ID: 0028_alerting
Revises: 0027_oncall_schedules
Create Date: 2026-07-02

Tenant-scoped tables (escalation_policies, contact_methods, alerts,
alert_routing_rules, heartbeats) get fail-open RLS; escalation_steps /
notification_rules / pages are FK-isolated. Also wires the deferred FK
oncall_services.escalation_policy_id → escalation_policies.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0028_alerting"
down_revision = "0027_oncall_schedules"
branch_labels = None
depends_on = None

_PREDICATE = """(
    current_setting('app.tenant_id', true) IS NULL
    OR current_setting('app.tenant_id', true) = ''
    OR current_setting('app.bypass_rls', true) = 'on'
    OR tenant_id = current_setting('app.tenant_id', true)::uuid
)"""

_RLS_TABLES = ["escalation_policies", "contact_methods", "alerts", "alert_routing_rules", "heartbeats"]


def _ts():
    return (
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def upgrade() -> None:
    op.create_table(
        "escalation_policies",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.VARCHAR(255), nullable=False),
        sa.Column("repeat_count", sa.Integer, nullable=False, server_default=sa.text("0")),
        *_ts(),
    )
    op.create_index("ix_escalation_policies_tenant_id", "escalation_policies", ["tenant_id"])

    op.create_table(
        "escalation_steps",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("policy_id", sa.UUID(as_uuid=True), sa.ForeignKey("escalation_policies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("position", sa.Integer, nullable=False),
        sa.Column("target_type", sa.VARCHAR(10), nullable=False),
        sa.Column("target_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("timeout_minutes", sa.Integer, nullable=False, server_default=sa.text("15")),
        sa.Column("notify_strategy", sa.VARCHAR(20), nullable=False, server_default=sa.text("'current_oncall'")),
    )
    op.create_index("ix_escalation_steps_policy", "escalation_steps", ["policy_id"])

    op.create_table(
        "contact_methods",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("type", sa.VARCHAR(10), nullable=False),
        sa.Column("value", sa.VARCHAR(255), nullable=False),
        sa.Column("is_verified", sa.Boolean, nullable=False, server_default=sa.text("false")),
    )
    op.create_index("ix_contact_methods_tenant_id", "contact_methods", ["tenant_id"])
    op.create_index("ix_contact_methods_user", "contact_methods", ["user_id"])

    op.create_table(
        "notification_rules",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", sa.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("urgency", sa.VARCHAR(10), nullable=False, server_default=sa.text("'high'")),
        sa.Column("position", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("delay_minutes", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("contact_method_id", sa.UUID(as_uuid=True), sa.ForeignKey("contact_methods.id", ondelete="CASCADE"), nullable=False),
    )
    op.create_index("ix_notification_rules_user", "notification_rules", ["user_id"])

    op.create_table(
        "alerts",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("service_id", sa.UUID(as_uuid=True), sa.ForeignKey("oncall_services.id", ondelete="SET NULL"), nullable=True),
        sa.Column("source", sa.VARCHAR(60), nullable=False, server_default=sa.text("'manual'")),
        sa.Column("dedup_key", sa.VARCHAR(255), nullable=False),
        sa.Column("title", sa.VARCHAR(500), nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=True),
        sa.Column("severity_id", sa.UUID(as_uuid=True), sa.ForeignKey("severity_levels.id", ondelete="SET NULL"), nullable=True),
        sa.Column("status", sa.VARCHAR(15), nullable=False, server_default=sa.text("'open'")),
        sa.Column("occurrence_count", sa.Integer, nullable=False, server_default=sa.text("1")),
        sa.Column("incident_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("escalation_policy_id", sa.UUID(as_uuid=True), sa.ForeignKey("escalation_policies.id", ondelete="SET NULL"), nullable=True),
        sa.Column("escalation_step_index", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("next_escalation_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("acknowledged_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.TIMESTAMP(timezone=True), nullable=True),
        *_ts(),
    )
    op.create_index("uq_alert_open_dedup", "alerts", ["tenant_id", "dedup_key"],
                    unique=True, postgresql_where=sa.text("status = 'open'"))
    op.create_index("ix_alerts_tenant_status", "alerts", ["tenant_id", "status", "created_at"])
    op.create_index("ix_alerts_next_escalation", "alerts", ["status", "next_escalation_at"])

    op.create_table(
        "pages",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("alert_id", sa.UUID(as_uuid=True), sa.ForeignKey("alerts.id", ondelete="CASCADE"), nullable=True),
        sa.Column("incident_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("user_id", sa.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("contact_method_id", sa.UUID(as_uuid=True), sa.ForeignKey("contact_methods.id", ondelete="SET NULL"), nullable=True),
        sa.Column("status", sa.VARCHAR(15), nullable=False, server_default=sa.text("'queued'")),
        sa.Column("escalation_step_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("sent_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("acknowledged_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index("ix_pages_alert", "pages", ["alert_id"])
    op.create_index("ix_pages_user", "pages", ["user_id"])

    op.create_table(
        "alert_routing_rules",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("position", sa.Integer, nullable=False),
        sa.Column("conditions", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("service_id", sa.UUID(as_uuid=True), sa.ForeignKey("oncall_services.id", ondelete="CASCADE"), nullable=False),
        sa.Column("severity_id", sa.UUID(as_uuid=True), sa.ForeignKey("severity_levels.id", ondelete="SET NULL"), nullable=True),
    )
    op.create_index("ix_alert_routing_rules_tenant_pos", "alert_routing_rules", ["tenant_id", "position"])

    op.create_table(
        "heartbeats",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("service_id", sa.UUID(as_uuid=True), sa.ForeignKey("oncall_services.id", ondelete="SET NULL"), nullable=True),
        sa.Column("name", sa.VARCHAR(255), nullable=False),
        sa.Column("interval_sec", sa.Integer, nullable=False),
        sa.Column("last_ping_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("ping_token", sa.VARCHAR(64), nullable=False, unique=True),
    )
    op.create_index("ix_heartbeats_tenant_id", "heartbeats", ["tenant_id"])

    # Wire the deferred FK from S8.1.
    op.create_foreign_key(
        "fk_oncall_services_escalation_policy", "oncall_services", "escalation_policies",
        ["escalation_policy_id"], ["id"], ondelete="SET NULL",
    )

    for t in _RLS_TABLES:
        op.execute(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_isolation ON {t} USING {_PREDICATE} WITH CHECK {_PREDICATE}")


def downgrade() -> None:
    op.drop_constraint("fk_oncall_services_escalation_policy", "oncall_services", type_="foreignkey")
    op.drop_table("heartbeats")
    op.drop_table("alert_routing_rules")
    op.drop_table("pages")
    op.drop_table("alerts")
    op.drop_table("notification_rules")
    op.drop_table("contact_methods")
    op.drop_table("escalation_steps")
    op.drop_table("escalation_policies")
