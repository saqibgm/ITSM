"""Post-incident review (Phase 8 / S8.5) — retrospectives, action items, PIR drafts.

Revision ID: 0031_retro
Revises: 0030_ops
Create Date: 2026-07-02

incident_retrospectives / retro_action_items are FK-isolated via the incident;
ai_pir_drafts is tenant-scoped with fail-open RLS.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0031_retro"
down_revision = "0030_ops"
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
        "incident_retrospectives",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("incident_id", sa.UUID(as_uuid=True), sa.ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("summary", sa.Text, nullable=True),
        sa.Column("contributing_factors", sa.Text, nullable=True),
        sa.Column("impact", sa.Text, nullable=True),
        sa.Column("status", sa.VARCHAR(15), nullable=False, server_default=sa.text("'draft'")),
        sa.Column("published_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "retro_action_items",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("retro_id", sa.UUID(as_uuid=True), sa.ForeignKey("incident_retrospectives.id", ondelete="CASCADE"), nullable=False),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("owner_id", sa.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("ticket_id", sa.UUID(as_uuid=True), sa.ForeignKey("tickets.id", ondelete="SET NULL"), nullable=True),
        sa.Column("status", sa.VARCHAR(15), nullable=False, server_default=sa.text("'open'")),
    )
    op.create_index("ix_retro_action_items_retro", "retro_action_items", ["retro_id"])

    op.create_table(
        "ai_pir_drafts",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("incident_id", sa.UUID(as_uuid=True), sa.ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("summary", sa.Text, nullable=True),
        sa.Column("contributing_factors", sa.Text, nullable=True),
        sa.Column("impact", sa.Text, nullable=True),
        sa.Column("action_items", postgresql.JSONB, nullable=True),
        sa.Column("status", sa.VARCHAR(20), nullable=False, server_default=sa.text("'pending_review'")),
        sa.Column("model_version", sa.VARCHAR(40), nullable=False, server_default=sa.text("'template-v1'")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_ai_pir_drafts_tenant", "ai_pir_drafts", ["tenant_id"])
    op.create_index("ix_ai_pir_drafts_incident", "ai_pir_drafts", ["incident_id"])

    op.execute("ALTER TABLE ai_pir_drafts ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE ai_pir_drafts FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY tenant_isolation ON ai_pir_drafts USING {_PREDICATE} WITH CHECK {_PREDICATE}")


def downgrade() -> None:
    op.drop_table("ai_pir_drafts")
    op.drop_table("retro_action_items")
    op.drop_table("incident_retrospectives")
