"""Store IAM roles on users (synced on login) + per-tenant user uniqueness.

Revision ID: 0018_user_iam_roles_sync
Revises: 0017_global_ticket_number
Create Date: 2026-06-09

  - Adds users.iam_roles (JSONB) — the user's IAM roles (realm + project-iq
    client roles), mirrored from the token on each login. IAM stays the source
    of truth; itsm-service derives permissions from it.
  - Relaxes the global UNIQUE(iam_user_id) to UNIQUE(iam_user_id, tenant_id) so
    a single IAM identity (e.g. 99T platform staff) can be mirrored in more than
    one tenant.
"""

from alembic import op
import sqlalchemy as sa

revision = "0018_user_iam_roles_sync"
down_revision = "0017_global_ticket_number"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "iam_roles",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )

    # Replace the global unique(iam_user_id) with a per-tenant composite unique.
    # The single-column unique was auto-named users_iam_user_id_key by Postgres.
    op.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS users_iam_user_id_key")
    op.create_index("ix_users_iam_user_id", "users", ["iam_user_id"])
    op.create_unique_constraint(
        "uq_users_iam_tenant", "users", ["iam_user_id", "tenant_id"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_users_iam_tenant", "users", type_="unique")
    op.drop_index("ix_users_iam_user_id", table_name="users")
    op.create_unique_constraint("users_iam_user_id_key", "users", ["iam_user_id"])
    op.drop_column("users", "iam_roles")
