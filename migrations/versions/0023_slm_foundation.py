"""SLM foundation (Phase 7 / S7.1) — agreements, targets, underpinning, rules, coverage windows.

Revision ID: 0023_slm_foundation
Revises: 0022_open_direct_resolve
Create Date: 2026-07-01

Promotes the flat ``sla_policies`` into a first-class SLA/OLA/UC model. Creates
``coverage_windows``, ``sla_agreements``, ``sla_targets``, ``sla_underpinning``,
``sla_rules`` (tenant-scoped tables get the same fail-open RLS policy as 0014;
``sla_underpinning`` is isolated via its parent-target FKs, like ticket_approvals).

Data migration (non-destructive — ``sla_policies`` and ``business_hours_config``
are kept as the back-compat read path):
- each ``business_hours_config`` → a "Default" coverage window (reusing its id);
- each ``sla_policies`` row → an ``sla_agreements`` (kind='sla', reusing its id)
  + two ``sla_targets`` (first_response, resolution), pointing at the tenant's
  Default coverage window when the policy was business-hours-only.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0023_slm_foundation"
down_revision = "0022_open_direct_resolve"
branch_labels = None
depends_on = None

# Fail-open tenant-isolation predicate — identical to migration 0014.
_PREDICATE = """(
    current_setting('app.tenant_id', true) IS NULL
    OR current_setting('app.tenant_id', true) = ''
    OR current_setting('app.bypass_rls', true) = 'on'
    OR tenant_id = current_setting('app.tenant_id', true)::uuid
)"""

_RLS_TABLES = ["coverage_windows", "sla_agreements", "sla_targets", "sla_rules"]


def _ts_cols():
    return (
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def upgrade() -> None:
    # ---- coverage_windows -------------------------------------------------
    op.create_table(
        "coverage_windows",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.VARCHAR(255), nullable=False),
        sa.Column("timezone", sa.VARCHAR(64), nullable=False, server_default=sa.text("'UTC'")),
        sa.Column("is_247", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("work_days", sa.ARRAY(sa.Integer), nullable=False, server_default=sa.text("'{1,2,3,4,5}'")),
        sa.Column("windows", postgresql.JSONB, nullable=True),
        sa.Column("holidays", sa.ARRAY(sa.Date), nullable=True),
        sa.Column("compose_of", sa.ARRAY(sa.UUID(as_uuid=True)), nullable=True),
        *_ts_cols(),
    )
    op.create_index("ix_coverage_windows_tenant_id", "coverage_windows", ["tenant_id"])

    # ---- sla_agreements ---------------------------------------------------
    op.create_table(
        "sla_agreements",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.VARCHAR(255), nullable=False),
        sa.Column("kind", sa.VARCHAR(10), nullable=False, server_default=sa.text("'sla'")),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("owner_team_id", sa.UUID(as_uuid=True), sa.ForeignKey("teams.id", ondelete="SET NULL"), nullable=True),
        sa.Column("vendor_id", sa.UUID(as_uuid=True), sa.ForeignKey("vendors.id", ondelete="SET NULL"), nullable=True),
        sa.Column("version", sa.Integer, nullable=False, server_default=sa.text("1")),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("deleted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        *_ts_cols(),
    )
    op.create_index("ix_sla_agreements_tenant_id", "sla_agreements", ["tenant_id"])

    # ---- sla_targets ------------------------------------------------------
    op.create_table(
        "sla_targets",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("agreement_id", sa.UUID(as_uuid=True), sa.ForeignKey("sla_agreements.id", ondelete="CASCADE"), nullable=False),
        sa.Column("metric", sa.VARCHAR(30), nullable=False),
        sa.Column("duration_minutes", sa.Integer, nullable=False),
        sa.Column("coverage_window_id", sa.UUID(as_uuid=True), sa.ForeignKey("coverage_windows.id", ondelete="SET NULL"), nullable=True),
        sa.Column("applies_priority", sa.VARCHAR(20), nullable=True),
        sa.Column("applies_type", sa.VARCHAR(30), nullable=True),
        sa.Column("applies_category_id", sa.UUID(as_uuid=True), sa.ForeignKey("ticket_categories.id", ondelete="SET NULL"), nullable=True),
        sa.Column("start_event", sa.VARCHAR(40), nullable=True),
        sa.Column("stop_event", sa.VARCHAR(40), nullable=True),
        sa.Column("pause_conditions", sa.ARRAY(sa.VARCHAR(40)), nullable=True),
        sa.Column("warn_thresholds_pct", sa.ARRAY(sa.Integer), nullable=False, server_default=sa.text("'{50,75,90}'")),
        *_ts_cols(),
    )
    op.create_index("ix_sla_targets_tenant_id", "sla_targets", ["tenant_id"])
    op.create_index("ix_sla_targets_agreement_id", "sla_targets", ["agreement_id"])

    # ---- sla_underpinning (no own tenant_id; isolated via parent FKs) ------
    op.create_table(
        "sla_underpinning",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("parent_target_id", sa.UUID(as_uuid=True), sa.ForeignKey("sla_targets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("support_target_id", sa.UUID(as_uuid=True), sa.ForeignKey("sla_targets.id", ondelete="CASCADE"), nullable=False),
        sa.UniqueConstraint("parent_target_id", "support_target_id", name="uq_sla_underpinning_pair"),
    )
    op.create_index("ix_sla_underpinning_parent", "sla_underpinning", ["parent_target_id"])

    # ---- sla_rules --------------------------------------------------------
    op.create_table(
        "sla_rules",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("position", sa.Integer, nullable=False),
        sa.Column("conditions", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("agreement_id", sa.UUID(as_uuid=True), sa.ForeignKey("sla_agreements.id", ondelete="CASCADE"), nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        *_ts_cols(),
    )
    op.create_index("ix_sla_rules_tenant_position", "sla_rules", ["tenant_id", "position"])

    # ---- RLS on tenant-scoped tables (fail-open, same as 0014) ------------
    for t in _RLS_TABLES:
        op.execute(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {t} "
            f"USING {_PREDICATE} WITH CHECK {_PREDICATE}"
        )

    # ---- Data migration ---------------------------------------------------
    # 1) business_hours_config → a "Default" coverage window (reuse its id).
    op.execute(
        """
        INSERT INTO coverage_windows (id, tenant_id, name, timezone, is_247, work_days, windows, holidays, created_at, updated_at)
        SELECT bhc.id, bhc.tenant_id, 'Default', bhc.timezone, false, bhc.work_days,
               jsonb_build_array(jsonb_build_object(
                   'start', to_char(bhc.work_start_time, 'HH24:MI'),
                   'end',   to_char(bhc.work_end_time,   'HH24:MI'))),
               bhc.holidays, now(), now()
        FROM business_hours_config bhc
        """
    )

    # 2) sla_policies → sla_agreements (kind='sla', reuse the policy id).
    op.execute(
        """
        INSERT INTO sla_agreements (id, tenant_id, name, kind, version, is_active, created_at, updated_at)
        SELECT p.id, p.tenant_id, p.name, 'sla', 1, true, now(), now()
        FROM sla_policies p
        """
    )

    # 3) sla_policies → two sla_targets each (first_response + resolution),
    #    on the tenant's Default coverage window when business-hours-only.
    op.execute(
        """
        INSERT INTO sla_targets (id, tenant_id, agreement_id, metric, duration_minutes,
                                 coverage_window_id, warn_thresholds_pct, created_at, updated_at)
        SELECT gen_random_uuid(), p.tenant_id, p.id, 'first_response', p.response_time_minutes,
               CASE WHEN p.business_hours_only
                    THEN (SELECT cw.id FROM coverage_windows cw
                          WHERE cw.tenant_id = p.tenant_id AND cw.name = 'Default' LIMIT 1)
                    ELSE NULL END,
               '{50,75,90}', now(), now()
        FROM sla_policies p
        """
    )
    op.execute(
        """
        INSERT INTO sla_targets (id, tenant_id, agreement_id, metric, duration_minutes,
                                 coverage_window_id, warn_thresholds_pct, created_at, updated_at)
        SELECT gen_random_uuid(), p.tenant_id, p.id, 'resolution', p.resolution_time_minutes,
               CASE WHEN p.business_hours_only
                    THEN (SELECT cw.id FROM coverage_windows cw
                          WHERE cw.tenant_id = p.tenant_id AND cw.name = 'Default' LIMIT 1)
                    ELSE NULL END,
               '{50,75,90}', now(), now()
        FROM sla_policies p
        """
    )


def downgrade() -> None:
    op.drop_table("sla_underpinning")
    op.drop_table("sla_rules")
    op.drop_table("sla_targets")
    op.drop_table("sla_agreements")
    op.drop_table("coverage_windows")
