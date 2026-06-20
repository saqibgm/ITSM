"""Ticket domain tables

Revision ID: 0002
Revises: 0001b
Create Date: 2026-06-05
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, ENUM

revision = "0002"
down_revision = "0001b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # Enum types
    # ------------------------------------------------------------------
    op.execute(text(
    "DO $$ BEGIN "
    "CREATE TYPE tickettype AS ENUM ('incident','service_request','problem','change'); "
    "EXCEPTION WHEN duplicate_object THEN null; END $$"
))
    op.execute(text(
    "DO $$ BEGIN "
    "CREATE TYPE ticketstatus AS ENUM ('open','in_progress','pending','pending_approval','approved','rejected','resolved','closed','cancelled'); "
    "EXCEPTION WHEN duplicate_object THEN null; END $$"
))
    op.execute(text(
    "DO $$ BEGIN "
    "CREATE TYPE ticketpriority AS ENUM ('critical','high','medium','low'); "
    "EXCEPTION WHEN duplicate_object THEN null; END $$"
))

    # ------------------------------------------------------------------
    # business_hours_config
    # ------------------------------------------------------------------
    op.create_table(
        "business_hours_config",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("timezone", sa.VARCHAR(100), nullable=False, server_default=sa.text("'UTC'")),
        sa.Column(
            "work_days",
            ARRAY(sa.Integer),
            nullable=False,
            server_default=sa.text("ARRAY[1,2,3,4,5]"),
        ),
        sa.Column(
            "work_start_time",
            sa.Time,
            nullable=False,
            server_default=sa.text("'09:00'"),
        ),
        sa.Column(
            "work_end_time",
            sa.Time,
            nullable=False,
            server_default=sa.text("'18:00'"),
        ),
        sa.Column(
            "holidays",
            ARRAY(sa.Date),
            nullable=False,
            server_default=sa.text("ARRAY[]::date[]"),
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

    # ------------------------------------------------------------------
    # sla_policies
    # ------------------------------------------------------------------
    op.create_table(
        "sla_policies",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.VARCHAR(255), nullable=False),
        sa.Column("response_time_minutes", sa.Integer, nullable=False),
        sa.Column("resolution_time_minutes", sa.Integer, nullable=False),
        sa.Column(
            "business_hours_only",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("true"),
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
    op.create_index("ix_sla_policies_tenant_id", "sla_policies", ["tenant_id"])

    # ------------------------------------------------------------------
    # ticket_categories
    # ------------------------------------------------------------------
    op.create_table(
        "ticket_categories",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "product_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("products.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("name", sa.VARCHAR(255), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column(
            "parent_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("ticket_categories.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "default_sla_policy_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("sla_policies.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "default_team_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("teams.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
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
    op.create_index("ix_ticket_categories_tenant_id", "ticket_categories", ["tenant_id"])

    # ------------------------------------------------------------------
    # tickets
    # ------------------------------------------------------------------
    op.create_table(
        "tickets",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("idempotency_key", sa.VARCHAR(255), nullable=True),
        sa.Column("ticket_number", sa.VARCHAR(20), nullable=False),
        sa.Column(
            "tenant_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "product_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("products.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "department_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("departments.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("type", ENUM("incident", "service_request", "problem", "change", name="tickettype", create_type=False), nullable=False),
        sa.Column(
            "status",
            ENUM(
                "open", "in_progress", "pending", "pending_approval", "approved",
                "rejected", "resolved", "closed", "cancelled",
                name="ticketstatus", create_type=False,
            ),
            nullable=False,
            server_default=sa.text("'open'"),
        ),
        sa.Column("priority", ENUM("critical", "high", "medium", "low", name="ticketpriority", create_type=False), nullable=False),
        sa.Column("title", sa.VARCHAR(500), nullable=False),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column(
            "requester_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "assignee_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "team_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("teams.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "category_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("ticket_categories.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "sla_policy_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("sla_policies.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("sla_response_due", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("sla_resolve_due", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("sla_breached", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("sla_paused_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "sla_paused_duration_sec",
            sa.Integer,
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "parent_ticket_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("tickets.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("due_date", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("closed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("description_embedding", sa.Text, nullable=True),
        sa.Column(
            "metadata",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
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

    op.create_index("ix_tickets_tenant_id", "tickets", ["tenant_id"])
    op.create_index(
        "ix_tickets_tenant_status_priority",
        "tickets",
        ["tenant_id", "status", "priority"],
    )
    op.create_index("ix_tickets_tenant_assignee", "tickets", ["tenant_id", "assignee_id"])
    op.create_index("ix_tickets_tenant_requester", "tickets", ["tenant_id", "requester_id"])
    op.create_index(
        "ix_tickets_tenant_created_at",
        "tickets",
        ["tenant_id", sa.text("created_at DESC")],
    )

    # Partial unique index for idempotency
    op.execute(text(
        "CREATE UNIQUE INDEX uq_tickets_tenant_idempotency "
        "ON tickets(tenant_id, idempotency_key) "
        "WHERE idempotency_key IS NOT NULL"
    ))

    # ------------------------------------------------------------------
    # ticket_history  (IMMUTABLE)
    # ------------------------------------------------------------------
    op.create_table(
        "ticket_history",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "ticket_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("tickets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "actor_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("field_changed", sa.VARCHAR(100), nullable=False),
        sa.Column("old_value", JSONB, nullable=True),
        sa.Column("new_value", JSONB, nullable=True),
        sa.Column(
            "changed_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_ticket_history_ticket_changed",
        "ticket_history",
        ["ticket_id", "changed_at"],
    )

    # ------------------------------------------------------------------
    # ticket_escalations
    # ------------------------------------------------------------------
    op.create_table(
        "ticket_escalations",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "ticket_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("tickets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "escalated_by",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "escalated_to",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column(
            "escalated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_ticket_escalations_ticket_id", "ticket_escalations", ["ticket_id"])

    # ------------------------------------------------------------------
    # ticket_comments
    # ------------------------------------------------------------------
    op.create_table(
        "ticket_comments",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "ticket_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("tickets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "author_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("is_internal", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column(
            "parent_comment_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("ticket_comments.id", ondelete="SET NULL"),
            nullable=True,
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
        sa.Column("deleted_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index("ix_ticket_comments_ticket_id", "ticket_comments", ["ticket_id"])

    # ------------------------------------------------------------------
    # ticket_attachments
    # ------------------------------------------------------------------
    op.create_table(
        "ticket_attachments",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "ticket_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("tickets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "comment_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("ticket_comments.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "uploaded_by",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("filename", sa.VARCHAR(512), nullable=False),
        sa.Column("storage_url", sa.VARCHAR(2048), nullable=False),
        sa.Column("file_size", sa.BigInteger, nullable=False),
        sa.Column("mime_type", sa.VARCHAR(255), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_ticket_attachments_ticket_id", "ticket_attachments", ["ticket_id"])

    # ------------------------------------------------------------------
    # ticket_tags
    # ------------------------------------------------------------------
    op.create_table(
        "ticket_tags",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.VARCHAR(100), nullable=False),
        sa.Column("color", sa.VARCHAR(20), nullable=False),
        sa.UniqueConstraint("tenant_id", "name", name="uq_ticket_tags_tenant_name"),
    )
    op.create_index("ix_ticket_tags_tenant_id", "ticket_tags", ["tenant_id"])

    # ------------------------------------------------------------------
    # ticket_tag_assignments
    # ------------------------------------------------------------------
    op.create_table(
        "ticket_tag_assignments",
        sa.Column(
            "ticket_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("tickets.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "tag_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("ticket_tags.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "assigned_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    # ------------------------------------------------------------------
    # ticket_watchers
    # ------------------------------------------------------------------
    op.create_table(
        "ticket_watchers",
        sa.Column(
            "ticket_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("tickets.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "user_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "added_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    # ------------------------------------------------------------------
    # Switch description_embedding to proper vector type if pgvector is live
    # ------------------------------------------------------------------
    try:
        op.execute(text(
            "ALTER TABLE tickets "
            "ALTER COLUMN description_embedding TYPE vector(1536) "
            "USING description_embedding::vector(1536)"
        ))
    except Exception:
        pass

    # HNSW index on embedding — wrap in try/except in case pgvector not installed
    try:
        op.execute(text(
            "CREATE INDEX ix_tickets_embedding_hnsw "
            "ON tickets USING hnsw (description_embedding vector_cosine_ops)"
        ))
    except Exception:
        pass

    # ------------------------------------------------------------------
    # Security: make ticket_history immutable
    # ------------------------------------------------------------------
    op.execute(text("REVOKE UPDATE, DELETE ON ticket_history FROM itsm_user"))


def downgrade() -> None:
    op.execute(text("GRANT UPDATE, DELETE ON ticket_history TO itsm_user"))

    op.drop_table("ticket_watchers")
    op.drop_table("ticket_tag_assignments")
    op.drop_table("ticket_tags")
    op.drop_table("ticket_attachments")
    op.drop_table("ticket_comments")
    op.drop_table("ticket_escalations")
    op.drop_table("ticket_history")

    op.drop_index("uq_tickets_tenant_idempotency", table_name="tickets")
    op.drop_index("ix_tickets_tenant_created_at", table_name="tickets")
    op.drop_index("ix_tickets_tenant_requester", table_name="tickets")
    op.drop_index("ix_tickets_tenant_assignee", table_name="tickets")
    op.drop_index("ix_tickets_tenant_status_priority", table_name="tickets")
    op.drop_index("ix_tickets_tenant_id", table_name="tickets")
    op.drop_table("tickets")

    op.drop_index("ix_ticket_categories_tenant_id", table_name="ticket_categories")
    op.drop_table("ticket_categories")

    op.drop_index("ix_sla_policies_tenant_id", table_name="sla_policies")
    op.drop_table("sla_policies")

    op.drop_table("business_hours_config")

    op.execute(text("DROP TYPE IF EXISTS ticketpriority"))
    op.execute(text("DROP TYPE IF EXISTS ticketstatus"))
    op.execute(text("DROP TYPE IF EXISTS tickettype"))
