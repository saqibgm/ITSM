"""Automation Rules Engine — automation_rules and automation_logs tables.

Revision ID: 0007_automation
Revises: 0006_virtual_agent
Create Date: 2026-06-05

Creates:
  automation_rules  — tenant-scoped rules with trigger / conditions / actions
  automation_logs   — immutable execution records

Indexes:
  idx_automation_rules_tenant_trigger  on (tenant_id, trigger, is_active)
  idx_automation_logs_rule_created     on (rule_id, created_at)
  idx_automation_logs_tenant_entity    on (tenant_id, entity_type, entity_id)

Full downgrade() drops both tables in reverse FK order.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, ENUM

revision = "0007_automation"
down_revision = "0006_virtual_agent"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # automation_rules
    # ------------------------------------------------------------------
    op.create_table(
        "automation_rules",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.VARCHAR(255), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("trigger", sa.VARCHAR(50), nullable=False),
        sa.Column(
            "conditions",
            JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "actions",
            JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "is_active",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "run_order",
            sa.Integer,
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("last_triggered_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "trigger_count",
            sa.Integer,
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "idx_automation_rules_tenant_id",
        "automation_rules",
        ["tenant_id"],
    )
    op.create_index(
        "idx_automation_rules_tenant_trigger",
        "automation_rules",
        ["tenant_id", "trigger", "is_active"],
    )

    # ------------------------------------------------------------------
    # automation_logs
    # ------------------------------------------------------------------
    op.create_table(
        "automation_logs",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "rule_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("automation_rules.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("entity_type", sa.VARCHAR(50), nullable=False),
        sa.Column("entity_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("actions_executed", JSONB, nullable=False),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "idx_automation_logs_rule_id",
        "automation_logs",
        ["rule_id"],
    )
    op.create_index(
        "idx_automation_logs_rule_created",
        "automation_logs",
        ["rule_id", "created_at"],
    )
    op.create_index(
        "idx_automation_logs_tenant_entity",
        "automation_logs",
        ["tenant_id", "entity_type", "entity_id"],
    )


def downgrade() -> None:
    # Drop logs first (FK dependency on rules)
    op.drop_index("idx_automation_logs_tenant_entity", table_name="automation_logs")
    op.drop_index("idx_automation_logs_rule_created", table_name="automation_logs")
    op.drop_index("idx_automation_logs_rule_id", table_name="automation_logs")
    op.drop_table("automation_logs")

    op.drop_index("idx_automation_rules_tenant_trigger", table_name="automation_rules")
    op.drop_index("idx_automation_rules_tenant_id", table_name="automation_rules")
    op.drop_table("automation_rules")
