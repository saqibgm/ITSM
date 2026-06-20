"""Notifications and notification_preferences tables

Revision ID: 0002b
Revises: 0002
Create Date: 2026-06-05

Depends on 0002_tickets which creates the tenants and users tables.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import ENUM

revision = "0002b"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # Enum type for notification types
    # ------------------------------------------------------------------
    op.execute(text(
    "DO $$ BEGIN "
    "CREATE TYPE notificationtype AS ENUM ('ticket_assigned','ticket_updated','ticket_commented','ticket_sla_warning','ticket_sla_breached','asset_maintenance_due','kb_article_published','mention','system'); "
    "EXCEPTION WHEN duplicate_object THEN null; END $$"
))

    # ------------------------------------------------------------------
    # notifications
    # ------------------------------------------------------------------
    op.create_table(
        "notifications",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "type",
            ENUM(
                "ticket_assigned",
                "ticket_updated",
                "ticket_commented",
                "ticket_sla_warning",
                "ticket_sla_breached",
                "asset_maintenance_due",
                "kb_article_published",
                "mention",
                "system",
                name="notificationtype",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("title", sa.VARCHAR(500), nullable=False),
        sa.Column("body", sa.Text, nullable=True),
        sa.Column("entity_type", sa.VARCHAR(50), nullable=True),
        sa.Column("entity_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("is_read", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("read_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    # Index for "unread count" badge query and inbox listing
    op.create_index(
        "ix_notifications_user_unread",
        "notifications",
        ["user_id", "is_read", sa.text("created_at DESC")],
    )
    # Index for tenant-level admin queries
    op.create_index(
        "ix_notifications_tenant",
        "notifications",
        ["tenant_id", sa.text("created_at DESC")],
    )

    # ------------------------------------------------------------------
    # notification_preferences
    # ------------------------------------------------------------------
    op.create_table(
        "notification_preferences",
        sa.Column(
            "user_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "tenant_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "type",
            ENUM(
                "ticket_assigned",
                "ticket_updated",
                "ticket_commented",
                "ticket_sla_warning",
                "ticket_sla_breached",
                "asset_maintenance_due",
                "kb_article_published",
                "mention",
                "system",
                name="notificationtype",
                create_type=False,
            ),
            primary_key=True,
        ),
        sa.Column(
            "email_enabled",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "in_app_enabled",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("true"),
        ),
    )
    op.create_index(
        "ix_notification_preferences_tenant_id",
        "notification_preferences",
        ["tenant_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_notification_preferences_tenant_id", table_name="notification_preferences")
    op.drop_table("notification_preferences")

    op.drop_index("ix_notifications_tenant", table_name="notifications")
    op.drop_index("ix_notifications_user_unread", table_name="notifications")
    op.drop_table("notifications")

    op.execute(text("DROP TYPE IF EXISTS notificationtype"))
