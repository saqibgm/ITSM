"""Incident management (Phase 8 / S8.3).

Revision ID: 0029_incidents
Revises: 0028_alerting
Create Date: 2026-07-02

incidents (tenant RLS) + seeded incident_status_transitions (global) + roles/
timeline/status_updates (FK-isolated). Adds incident_number_seq (IR-YYYY-NNNNN)
and wires the deferred alerts.incident_id → incidents FK.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0029_incidents"
down_revision = "0028_alerting"
branch_labels = None
depends_on = None

_PREDICATE = """(
    current_setting('app.tenant_id', true) IS NULL
    OR current_setting('app.tenant_id', true) = ''
    OR current_setting('app.bypass_rls', true) = 'on'
    OR tenant_id = current_setting('app.tenant_id', true)::uuid
)"""

_TRANSITIONS = [
    ("declared", "investigating"), ("declared", "cancelled"),
    ("investigating", "identified"), ("investigating", "monitoring"),
    ("investigating", "resolved"), ("investigating", "cancelled"),
    ("identified", "monitoring"), ("identified", "resolved"),
    ("monitoring", "resolved"), ("monitoring", "investigating"),
    ("resolved", "postmortem"), ("resolved", "closed"), ("resolved", "investigating"),
    ("postmortem", "closed"),
]


def upgrade() -> None:
    op.execute("CREATE SEQUENCE IF NOT EXISTS incident_number_seq")

    op.create_table(
        "incidents",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("incident_number", sa.VARCHAR(30), nullable=False),
        sa.Column("title", sa.VARCHAR(500), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("severity_id", sa.UUID(as_uuid=True), sa.ForeignKey("severity_levels.id", ondelete="SET NULL"), nullable=True),
        sa.Column("status", sa.VARCHAR(20), nullable=False, server_default=sa.text("'declared'")),
        sa.Column("declared_by", sa.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("declared_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("mitigated_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("affected_service_ids", sa.ARRAY(sa.UUID(as_uuid=True)), nullable=True),
        sa.Column("source_alert_id", sa.UUID(as_uuid=True), sa.ForeignKey("alerts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("source_ticket_id", sa.UUID(as_uuid=True), sa.ForeignKey("tickets.id", ondelete="SET NULL"), nullable=True),
        sa.Column("chat_channel", sa.VARCHAR(120), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "incident_number", name="uq_incident_number"),
    )
    op.create_index("ix_incidents_tenant_status", "incidents", ["tenant_id", "status", "declared_at"])

    op.create_table(
        "incident_status_transitions",
        sa.Column("from_status", sa.VARCHAR(20), primary_key=True),
        sa.Column("to_status", sa.VARCHAR(20), primary_key=True),
    )
    for f, t in _TRANSITIONS:
        op.execute(f"INSERT INTO incident_status_transitions (from_status, to_status) VALUES ('{f}', '{t}')")

    op.create_table(
        "incident_roles",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("incident_id", sa.UUID(as_uuid=True), sa.ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.VARCHAR(10), nullable=False),
        sa.Column("user_id", sa.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("assigned_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("incident_id", "role", name="uq_incident_role"),
    )
    op.create_index("ix_incident_roles_incident", "incident_roles", ["incident_id"])

    op.create_table(
        "incident_timeline",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("incident_id", sa.UUID(as_uuid=True), sa.ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("actor_id", sa.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("event_type", sa.VARCHAR(40), nullable=False),
        sa.Column("data", postgresql.JSONB, nullable=True),
        sa.Column("pinned", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_incident_timeline_incident_at", "incident_timeline", ["incident_id", "at"])

    op.create_table(
        "incident_status_updates",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("incident_id", sa.UUID(as_uuid=True), sa.ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("author_id", sa.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("audience", sa.VARCHAR(15), nullable=False, server_default=sa.text("'internal'")),
        sa.Column("channels", sa.ARRAY(sa.VARCHAR(20)), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_incident_status_updates_incident", "incident_status_updates", ["incident_id"])

    # Wire deferred FK from S8.2.
    op.create_foreign_key("fk_alerts_incident", "alerts", "incidents", ["incident_id"], ["id"], ondelete="SET NULL")

    op.execute("ALTER TABLE incidents ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE incidents FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY tenant_isolation ON incidents USING {_PREDICATE} WITH CHECK {_PREDICATE}")


def downgrade() -> None:
    op.drop_constraint("fk_alerts_incident", "alerts", type_="foreignkey")
    op.drop_table("incident_status_updates")
    op.drop_table("incident_timeline")
    op.drop_table("incident_roles")
    op.drop_table("incident_status_transitions")
    op.drop_table("incidents")
    op.execute("DROP SEQUENCE IF EXISTS incident_number_seq")
