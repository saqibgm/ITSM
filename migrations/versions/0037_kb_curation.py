"""Add KB wiki-curation status value + lineage column (KB_WIKI_CURATION_RAG_PLAN Phase 1).

Revision ID: 0037_kb_curation
Revises: 0036_rca_notif_types
Create Date: 2026-08-18

kbarticlestatus is create_type=False (migrations own the DDL), so the new
label is added via ALTER TYPE ... ADD VALUE, same pattern as 0036. Postgres
does not support dropping enum values, so downgrade() only drops the new
column, matching this repo's existing convention for additive enum
migrations.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0037_kb_curation"
down_revision = "0036_rca_notif_types"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE kbarticlestatus ADD VALUE IF NOT EXISTS 'ai_curated_pending_review'")
    op.add_column("kb_articles", sa.Column("curation_source", JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column("kb_articles", "curation_source")
    # enum value stays — Postgres can't drop enum values, matches 0036's convention
