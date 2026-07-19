"""RCA Governance module (specs/08, Phase 1+2).

Revision ID: 0035_rca_gov
Revises: 0034_recordings
Create Date: 2026-07-16

Evolves incident_retrospectives/retro_action_items in place into the governed
"RCA" object (per the decision to avoid a parallel rca_records table) rather
than replacing them. Legacy draft/in_review/published rows keep working
untouched via the new is_rca_governed=false default. New supporting tables
(rca_policies, rca_linked_entities, rca_evidence_checklist, rca_history) all
FK into incident_retrospectives.id as retro_id.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0035_rca_gov"
down_revision = "0034_recordings"
branch_labels = None
depends_on = None

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
    # ---- incident_retrospectives: backfill tenant_id before constraining ----
    op.add_column("incident_retrospectives", sa.Column("tenant_id", sa.UUID(as_uuid=True), nullable=True))
    op.execute(
        """
        UPDATE incident_retrospectives r SET tenant_id = i.tenant_id
        FROM incidents i WHERE r.incident_id = i.id
        """
    )
    op.alter_column("incident_retrospectives", "tenant_id", nullable=False)
    op.create_foreign_key(
        "fk_incident_retrospectives_tenant", "incident_retrospectives", "tenants", ["tenant_id"], ["id"], ondelete="CASCADE"
    )
    op.create_index("ix_incident_retrospectives_tenant", "incident_retrospectives", ["tenant_id"])

    # incident_id becomes optional — RCA can be sourced from a ticket/problem/change
    # directly. The existing UNIQUE(incident_id) constraint survives: Postgres
    # allows multiple NULLs in a unique column.
    op.alter_column("incident_retrospectives", "incident_id", nullable=True)
    # widen 15 -> 20 to fit 'actions_in_progress'
    op.alter_column("incident_retrospectives", "status", type_=sa.VARCHAR(20))

    op.add_column("incident_retrospectives", sa.Column("is_rca_governed", sa.Boolean, nullable=False, server_default=sa.text("false")))
    op.add_column("incident_retrospectives", sa.Column("source_type", sa.VARCHAR(20), nullable=False, server_default=sa.text("'incident'")))
    op.add_column("incident_retrospectives", sa.Column("source_ticket_id", sa.UUID(as_uuid=True), sa.ForeignKey("tickets.id", ondelete="SET NULL"), nullable=True))
    op.add_column("incident_retrospectives", sa.Column("rca_number", sa.VARCHAR(20), nullable=True))
    op.add_column("incident_retrospectives", sa.Column("severity", sa.VARCHAR(10), nullable=True))
    op.add_column("incident_retrospectives", sa.Column("owner_id", sa.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True))
    op.add_column("incident_retrospectives", sa.Column("approver_id", sa.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True))
    op.add_column("incident_retrospectives", sa.Column("team_id", sa.UUID(as_uuid=True), sa.ForeignKey("teams.id", ondelete="SET NULL"), nullable=True))
    op.add_column("incident_retrospectives", sa.Column("service_id", sa.UUID(as_uuid=True), sa.ForeignKey("oncall_services.id", ondelete="SET NULL"), nullable=True))
    op.add_column("incident_retrospectives", sa.Column("product_id", sa.UUID(as_uuid=True), sa.ForeignKey("products.id", ondelete="SET NULL"), nullable=True))
    op.add_column("incident_retrospectives", sa.Column("customer_org_id", sa.VARCHAR(255), nullable=True))
    op.add_column("incident_retrospectives", sa.Column("trigger_policy_id", sa.UUID(as_uuid=True), nullable=True))  # FK added after rca_policies exists below
    op.add_column("incident_retrospectives", sa.Column("previous_status", sa.VARCHAR(20), nullable=True))
    op.add_column("incident_retrospectives", sa.Column("due_at", sa.TIMESTAMP(timezone=True), nullable=True))
    op.add_column("incident_retrospectives", sa.Column("submitted_at", sa.TIMESTAMP(timezone=True), nullable=True))
    op.add_column("incident_retrospectives", sa.Column("approved_at", sa.TIMESTAMP(timezone=True), nullable=True))
    op.add_column("incident_retrospectives", sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True))
    op.add_column("incident_retrospectives", sa.Column("waived_at", sa.TIMESTAMP(timezone=True), nullable=True))
    op.add_column("incident_retrospectives", sa.Column("waived_by", sa.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True))
    op.add_column("incident_retrospectives", sa.Column("waiver_reason", sa.Text, nullable=True))
    op.add_column("incident_retrospectives", sa.Column("executive_summary", sa.Text, nullable=True))
    op.add_column("incident_retrospectives", sa.Column("customer_impact", sa.Text, nullable=True))
    op.add_column("incident_retrospectives", sa.Column("business_impact", sa.Text, nullable=True))
    op.add_column("incident_retrospectives", sa.Column("technical_summary", sa.Text, nullable=True))
    op.add_column("incident_retrospectives", sa.Column("root_cause_statement", sa.Text, nullable=True))
    op.add_column("incident_retrospectives", sa.Column("root_cause_category", sa.VARCHAR(30), nullable=True))
    op.add_column("incident_retrospectives", sa.Column("contributing_factor_tags", postgresql.JSONB, nullable=True))
    op.add_column("incident_retrospectives", sa.Column("detection_gap", sa.Text, nullable=True))
    op.add_column("incident_retrospectives", sa.Column("response_gap", sa.Text, nullable=True))
    op.add_column("incident_retrospectives", sa.Column("prevention_plan", sa.Text, nullable=True))
    op.add_column("incident_retrospectives", sa.Column("lessons_learned", sa.Text, nullable=True))
    op.add_column("incident_retrospectives", sa.Column("customer_facing_summary", sa.Text, nullable=True))
    op.add_column("incident_retrospectives", sa.Column("ai_draft_status", sa.VARCHAR(20), nullable=False, server_default=sa.text("'not_requested'")))
    op.add_column("incident_retrospectives", sa.Column("ai_confidence", sa.Float, nullable=True))
    op.add_column("incident_retrospectives", sa.Column("ai_model_version", sa.VARCHAR(40), nullable=True))
    op.add_column("incident_retrospectives", sa.Column("created_by", sa.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True))

    op.create_unique_constraint("uq_incident_retrospectives_tenant_rca_number", "incident_retrospectives", ["tenant_id", "rca_number"])
    op.create_index("ix_incident_retrospectives_status_due", "incident_retrospectives", ["tenant_id", "status", "due_at"])
    op.create_index("ix_incident_retrospectives_owner_status", "incident_retrospectives", ["tenant_id", "owner_id", "status"])
    op.create_index("ix_incident_retrospectives_severity", "incident_retrospectives", ["tenant_id", "severity", sa.text("created_at DESC")])

    op.execute("CREATE SEQUENCE IF NOT EXISTS rca_number_seq")

    # ---- retro_action_items: backfill tenant_id, add governance columns ----
    op.add_column("retro_action_items", sa.Column("tenant_id", sa.UUID(as_uuid=True), nullable=True))
    op.execute(
        """
        UPDATE retro_action_items a SET tenant_id = r.tenant_id
        FROM incident_retrospectives r WHERE a.retro_id = r.id
        """
    )
    op.alter_column("retro_action_items", "tenant_id", nullable=False)
    op.create_foreign_key(
        "fk_retro_action_items_tenant", "retro_action_items", "tenants", ["tenant_id"], ["id"], ondelete="CASCADE"
    )
    op.create_index("ix_retro_action_items_tenant", "retro_action_items", ["tenant_id"])

    op.add_column("retro_action_items", sa.Column("title", sa.VARCHAR(255), nullable=True))
    op.add_column("retro_action_items", sa.Column("action_type", sa.VARCHAR(30), nullable=True))
    op.add_column("retro_action_items", sa.Column("priority", sa.VARCHAR(10), nullable=False, server_default=sa.text("'medium'")))
    op.add_column("retro_action_items", sa.Column("team_id", sa.UUID(as_uuid=True), sa.ForeignKey("teams.id", ondelete="SET NULL"), nullable=True))
    op.add_column("retro_action_items", sa.Column("due_at", sa.TIMESTAMP(timezone=True), nullable=True))
    op.add_column("retro_action_items", sa.Column("verification_method", sa.Text, nullable=True))
    op.add_column("retro_action_items", sa.Column("verified_by", sa.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True))
    op.add_column("retro_action_items", sa.Column("verified_at", sa.TIMESTAMP(timezone=True), nullable=True))
    op.add_column("retro_action_items", sa.Column("linked_change_id", sa.UUID(as_uuid=True), sa.ForeignKey("tickets.id", ondelete="SET NULL"), nullable=True))
    op.add_column("retro_action_items", sa.Column("accepted_risk_reason", sa.Text, nullable=True))
    op.add_column("retro_action_items", sa.Column("accepted_risk_by", sa.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True))
    op.add_column("retro_action_items", sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()))
    op.add_column("retro_action_items", sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_index("ix_retro_action_items_owner_status", "retro_action_items", ["tenant_id", "owner_id", "status", "due_at"])
    op.create_index("ix_retro_action_items_retro_status", "retro_action_items", ["retro_id", "status"])

    # ---- ai_pir_drafts: allow attaching to non-incident-sourced governed RCAs ----
    op.alter_column("ai_pir_drafts", "incident_id", nullable=True)
    op.add_column("ai_pir_drafts", sa.Column("retro_id", sa.UUID(as_uuid=True), sa.ForeignKey("incident_retrospectives.id", ondelete="CASCADE"), nullable=True))
    op.create_index("ix_ai_pir_drafts_retro", "ai_pir_drafts", ["retro_id"])

    # ---- new tables ----
    op.create_table(
        "rca_policies",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.VARCHAR(255), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("status", sa.VARCHAR(15), nullable=False, server_default=sa.text("'draft'")),
        sa.Column("conditions", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'")),
        sa.Column("outputs", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("priority", sa.Integer, nullable=False, server_default=sa.text("100")),
        sa.Column("created_by", sa.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_rca_policies_tenant", "rca_policies", ["tenant_id"])

    op.create_foreign_key(
        "fk_incident_retrospectives_trigger_policy", "incident_retrospectives", "rca_policies", ["trigger_policy_id"], ["id"], ondelete="SET NULL"
    )

    op.create_table(
        "rca_linked_entities",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("retro_id", sa.UUID(as_uuid=True), sa.ForeignKey("incident_retrospectives.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tenant_id", sa.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("entity_type", sa.VARCHAR(30), nullable=False),
        sa.Column("entity_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("entity_ref", sa.VARCHAR(255), nullable=True),
        sa.Column("link_role", sa.VARCHAR(30), nullable=False, server_default=sa.text("'related'")),
        sa.Column("required", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("linked_by", sa.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("linked_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("entity_id IS NOT NULL OR entity_ref IS NOT NULL", name="ck_rca_linked_entities_ref"),
    )
    op.create_index("ix_rca_linked_entities_retro", "rca_linked_entities", ["retro_id"])
    op.create_index("ix_rca_linked_entities_entity", "rca_linked_entities", ["entity_type", "entity_id"])
    op.create_index("ix_rca_linked_entities_tenant_entity", "rca_linked_entities", ["tenant_id", "entity_type"])

    op.create_table(
        "rca_evidence_checklist",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("retro_id", sa.UUID(as_uuid=True), sa.ForeignKey("incident_retrospectives.id", ondelete="CASCADE"), nullable=False),
        sa.Column("evidence_type", sa.VARCHAR(40), nullable=False),
        sa.Column("required", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("status", sa.VARCHAR(15), nullable=False, server_default=sa.text("'missing'")),
        sa.Column("provided_entity_type", sa.VARCHAR(30), nullable=True),
        sa.Column("provided_entity_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("waived_by", sa.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("waiver_reason", sa.Text, nullable=True),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("retro_id", "evidence_type", name="uq_rca_evidence_checklist_retro_type"),
    )
    op.create_index("ix_rca_evidence_checklist_retro", "rca_evidence_checklist", ["retro_id"])

    op.create_table(
        "rca_history",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("retro_id", sa.UUID(as_uuid=True), sa.ForeignKey("incident_retrospectives.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tenant_id", sa.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("actor_id", sa.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("event_type", sa.VARCHAR(30), nullable=False),
        sa.Column("field_changed", sa.VARCHAR(60), nullable=True),
        sa.Column("old_value", postgresql.JSONB, nullable=True),
        sa.Column("new_value", postgresql.JSONB, nullable=True),
        sa.Column("changed_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_rca_history_retro", "rca_history", ["retro_id", "changed_at"])

    # incident_retrospectives now has a real tenant_id column (backfilled above)
    # so it graduates from FK-only isolation to full RLS, matching every other
    # tenant-scoped table added in this migration.
    for t in ("incident_retrospectives", "retro_action_items", "rca_policies", "rca_linked_entities"):
        _rls(t)
    # rca_evidence_checklist/rca_history are FK-isolated via retro_id →
    # incident_retrospectives.tenant_id (same posture as ticket_recording_links
    # in 0034) — no own tenant_id column, so not RLS candidates directly.


def downgrade() -> None:
    op.drop_table("rca_history")
    op.drop_table("rca_evidence_checklist")
    op.drop_table("rca_linked_entities")
    op.drop_constraint("fk_incident_retrospectives_trigger_policy", "incident_retrospectives", type_="foreignkey")
    op.drop_table("rca_policies")
    op.execute("DROP SEQUENCE IF EXISTS rca_number_seq")

    op.drop_index("ix_ai_pir_drafts_retro", table_name="ai_pir_drafts")
    op.drop_column("ai_pir_drafts", "retro_id")
    op.alter_column("ai_pir_drafts", "incident_id", nullable=False)

    op.drop_index("ix_retro_action_items_retro_status", table_name="retro_action_items")
    op.drop_index("ix_retro_action_items_owner_status", table_name="retro_action_items")
    for col in (
        "updated_at", "created_at", "accepted_risk_by", "accepted_risk_reason", "linked_change_id",
        "verified_at", "verified_by", "verification_method", "due_at", "team_id", "priority",
        "action_type", "title",
    ):
        op.drop_column("retro_action_items", col)
    op.drop_index("ix_retro_action_items_tenant", table_name="retro_action_items")
    op.drop_constraint("fk_retro_action_items_tenant", "retro_action_items", type_="foreignkey")
    op.drop_column("retro_action_items", "tenant_id")

    op.drop_index("ix_incident_retrospectives_severity", table_name="incident_retrospectives")
    op.drop_index("ix_incident_retrospectives_owner_status", table_name="incident_retrospectives")
    op.drop_index("ix_incident_retrospectives_status_due", table_name="incident_retrospectives")
    op.drop_constraint("uq_incident_retrospectives_tenant_rca_number", "incident_retrospectives", type_="unique")
    for col in (
        "created_by", "ai_model_version", "ai_confidence", "ai_draft_status", "customer_facing_summary",
        "lessons_learned", "prevention_plan", "response_gap", "detection_gap", "contributing_factor_tags",
        "root_cause_category", "root_cause_statement", "technical_summary", "business_impact", "customer_impact",
        "executive_summary", "waiver_reason", "waived_by", "waived_at", "completed_at", "approved_at",
        "submitted_at", "due_at", "previous_status", "trigger_policy_id", "customer_org_id", "product_id",
        "service_id", "team_id", "approver_id", "owner_id", "severity", "rca_number", "source_ticket_id",
        "source_type", "is_rca_governed",
    ):
        op.drop_column("incident_retrospectives", col)
    op.alter_column("incident_retrospectives", "status", type_=sa.VARCHAR(15))
    op.alter_column("incident_retrospectives", "incident_id", nullable=False)
    op.drop_index("ix_incident_retrospectives_tenant", table_name="incident_retrospectives")
    op.drop_constraint("fk_incident_retrospectives_tenant", "incident_retrospectives", type_="foreignkey")
    op.drop_column("incident_retrospectives", "tenant_id")
