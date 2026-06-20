"""AI predictive maintenance predictions table

Revision ID: 0004b_ai_maintenance
Revises: 0004_assets
Create Date: 2026-06-05

Adds ai_maintenance_predictions which stores Claude's nightly risk
assessments for each active asset.  One row per asset (upserted by the
Celery beat task); old rows are replaced, not accumulated.
"""

from alembic import op
import sqlalchemy as sa

revision = "0004b_ai_maintenance"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_maintenance_predictions",
        sa.Column(
            "id",
            sa.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "asset_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("assets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("predicted_due_date", sa.Date, nullable=False),
        sa.Column("risk_score", sa.Float, nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("model_version", sa.VARCHAR(100), nullable=False),
        sa.Column(
            "acknowledged",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "acknowledged_by",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    # Composite index: look up predictions by asset, ordered by risk (highest first)
    op.create_index(
        "ix_ai_maint_pred_asset_risk",
        "ai_maintenance_predictions",
        ["asset_id", sa.text("risk_score DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_ai_maint_pred_asset_risk", table_name="ai_maintenance_predictions")
    op.drop_table("ai_maintenance_predictions")
