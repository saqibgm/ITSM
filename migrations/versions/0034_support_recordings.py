"""Support Session Recording module (specs/08, Phase 1).

Revision ID: 0034_recordings
Revises: 0033_slo
Create Date: 2026-07-16

support_recordings / recording_access_log are tenant-scoped with fail-open RLS
(0033's NULLIF-guarded predicate). ticket_recording_links has no own tenant_id —
it is FK-isolated via ticket_id, same posture as ticket_tag_assignments/
ticket_watchers. tenant_recording_policies is a per-tenant singleton
(PK = tenant_id), no RLS needed since every query is naturally
WHERE tenant_id = :tid via the PK lookup.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0034_recordings"
down_revision = "0033_slo"
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
    op.create_table(
        "support_recordings",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_type", sa.VARCHAR(20), nullable=False),
        sa.Column("source_recording_id", sa.VARCHAR(255), nullable=True),
        sa.Column("source_meeting_id", sa.VARCHAR(255), nullable=True),
        sa.Column("source_drive_id", sa.VARCHAR(255), nullable=True),
        sa.Column("source_item_id", sa.VARCHAR(255), nullable=True),
        sa.Column("recording_url", sa.Text, nullable=False),
        sa.Column("transcript_url", sa.Text, nullable=True),
        sa.Column("title", sa.VARCHAR(500), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("ended_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("duration_seconds", sa.Integer, nullable=True),
        sa.Column("organizer_user_id", sa.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("organizer_email", sa.VARCHAR(320), nullable=True),
        sa.Column("customer_contact_name", sa.VARCHAR(255), nullable=True),
        sa.Column("customer_contact_email", sa.VARCHAR(320), nullable=True),
        sa.Column("participants", postgresql.JSONB, nullable=True),
        sa.Column("consent_status", sa.VARCHAR(20), nullable=False, server_default=sa.text("'not_required'")),
        sa.Column("sensitivity", sa.VARCHAR(20), nullable=False, server_default=sa.text("'normal'")),
        sa.Column("access_policy", sa.VARCHAR(20), nullable=False, server_default=sa.text("'ticket_team_only'")),
        sa.Column("retention_expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("storage_mode", sa.VARCHAR(20), nullable=False, server_default=sa.text("'external_reference'")),
        sa.Column("storage_key", sa.VARCHAR(512), nullable=True),
        sa.Column("status", sa.VARCHAR(20), nullable=False, server_default=sa.text("'linked'")),
        sa.Column("ai_summary_status", sa.VARCHAR(20), nullable=False, server_default=sa.text("'not_requested'")),
        sa.Column("ai_summary", sa.Text, nullable=True),
        sa.Column("ai_action_items", postgresql.JSONB, nullable=True),
        sa.Column("created_by", sa.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_support_recordings_tenant_started", "support_recordings", ["tenant_id", sa.text("started_at DESC")])
    op.create_index("ix_support_recordings_tenant_organizer", "support_recordings", ["tenant_id", "organizer_email"])
    op.create_index("ix_support_recordings_tenant_status", "support_recordings", ["tenant_id", "status"])

    op.create_table(
        "ticket_recording_links",
        sa.Column("ticket_id", sa.UUID(as_uuid=True), sa.ForeignKey("tickets.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("recording_id", sa.UUID(as_uuid=True), sa.ForeignKey("support_recordings.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("link_type", sa.VARCHAR(20), nullable=False, server_default=sa.text("'support_call'")),
        sa.Column("linked_by", sa.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("linked_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("is_primary", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("visible_to_customer", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("evidence_weight", sa.VARCHAR(20), nullable=False, server_default=sa.text("'none'")),
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_ticket_recording_primary ON ticket_recording_links (ticket_id) WHERE is_primary"
    )

    op.create_table(
        "recording_access_log",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("recording_id", sa.UUID(as_uuid=True), sa.ForeignKey("support_recordings.id", ondelete="CASCADE"), nullable=False),
        sa.Column("actor_id", sa.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("actor_email", sa.VARCHAR(320), nullable=False),
        sa.Column("action", sa.VARCHAR(30), nullable=False),
        sa.Column("source_ip", sa.VARCHAR(45), nullable=True),
        sa.Column("request_id", sa.VARCHAR(255), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_recording_access_log_recording", "recording_access_log", ["recording_id", sa.text("created_at DESC")])
    op.create_index("ix_recording_access_log_tenant", "recording_access_log", ["tenant_id", sa.text("created_at DESC")])

    op.create_table(
        "tenant_recording_policies",
        sa.Column("tenant_id", sa.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("recording_consent_required", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("consent_source", sa.VARCHAR(20), nullable=False, server_default=sa.text("'not_required'")),
        sa.Column("block_link_if_missing_consent", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("allow_customer_visibility", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("redact_transcript_before_ai", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("allow_ai_summary", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("recording_retention_days", sa.Integer, nullable=False, server_default=sa.text("365")),
        sa.Column("allow_manual_upload", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    # ticket_recording_links has no own tenant_id (FK-isolated via ticket_id →
    # tickets.tenant_id, same posture as ticket_tag_assignments/ticket_watchers)
    # so it is not a candidate for the tenant_id-based RLS predicate.
    for t in ("support_recordings", "recording_access_log"):
        _rls(t)


def downgrade() -> None:
    op.drop_table("tenant_recording_policies")
    op.drop_table("recording_access_log")
    op.drop_table("ticket_recording_links")
    op.drop_table("support_recordings")
