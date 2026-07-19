"""Add RCA/recording NotificationType enum values (specs/08).

Revision ID: 0036_rca_notif_types
Revises: 0035_rca_gov
Create Date: 2026-07-16

notificationtype is create_type=False (migrations own the DDL) so new labels
are added via ALTER TYPE ... ADD VALUE. Each value is only added here, never
used in the same transaction — Postgres forbids using a brand-new enum label
in the same transaction that created it, but back-to-back ADD VALUE statements
are fine. Corresponding Python enum members were added to
app/models/notification.py in the same commit as this migration.

Postgres does not support dropping enum values, so downgrade() is a no-op
(matches the project's existing convention for additive enum migrations).
"""

from alembic import op

revision = "0036_rca_notif_types"
down_revision = "0035_rca_gov"
branch_labels = None
depends_on = None

_NEW_VALUES = [
    "recording_linked",
    "recording_required_missing",
    "recording_inaccessible",
    "recording_consent_missing",
    "recording_summary_ready",
    "rca_required",
    "rca_due_soon",
    "rca_overdue",
    "rca_submitted",
    "rca_rejected",
    "rca_approved",
    "rca_action_assigned",
    "rca_action_overdue",
    "rca_completed",
]


def upgrade() -> None:
    for value in _NEW_VALUES:
        op.execute(f"ALTER TYPE notificationtype ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    # Postgres cannot drop enum values in place; a downgrade would require
    # recreating the type. Left as a no-op, consistent with how this
    # repo treats other additive-only enum migrations.
    pass
