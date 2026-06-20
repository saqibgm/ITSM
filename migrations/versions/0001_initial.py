"""Initial schema: identity tables + ticket state machine seed

Revision ID: 0001
Revises:
Create Date: 2026-06-05
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, ENUM

# revision identifiers, used by Alembic.
revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # PostgreSQL extensions
    # ------------------------------------------------------------------
    op.execute(sa.text('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"'))
    op.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))
    op.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))

    # ------------------------------------------------------------------
    # tenants
    # ------------------------------------------------------------------
    op.create_table(
        "tenants",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("iam_org_id", sa.VARCHAR(255), nullable=False, unique=True),
        sa.Column("name", sa.VARCHAR(255), nullable=False),
        sa.Column("slug", sa.VARCHAR(100), nullable=False, unique=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column(
            "settings",
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

    # ------------------------------------------------------------------
    # products
    # ------------------------------------------------------------------
    op.create_table(
        "products",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.VARCHAR(255), nullable=False),
        sa.Column("slug", sa.VARCHAR(100), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
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
        sa.UniqueConstraint("tenant_id", "slug", name="uq_products_tenant_slug"),
    )
    op.create_index("ix_products_tenant_id", "products", ["tenant_id"])

    # ------------------------------------------------------------------
    # departments
    # ------------------------------------------------------------------
    op.create_table(
        "departments",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("iam_facility_id", sa.VARCHAR(255), nullable=True),
        sa.Column("name", sa.VARCHAR(255), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column(
            "parent_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("departments.id", ondelete="SET NULL"),
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
    op.create_index("ix_departments_tenant_id", "departments", ["tenant_id"])

    # ------------------------------------------------------------------
    # users  (local IAM mirror — no password column, ever)
    # ------------------------------------------------------------------
    op.create_table(
        "users",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("iam_user_id", sa.VARCHAR(255), nullable=False, unique=True),
        sa.Column(
            "tenant_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("email", sa.VARCHAR(320), nullable=False),
        sa.Column("first_name", sa.VARCHAR(100), nullable=False),
        sa.Column("last_name", sa.VARCHAR(100), nullable=False),
        sa.Column("avatar_url", sa.VARCHAR(2048), nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column(
            "metadata",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("last_seen_at", sa.TIMESTAMP(timezone=True), nullable=True),
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
    op.create_index("ix_users_tenant_id", "users", ["tenant_id"])

    # ------------------------------------------------------------------
    # user_department_assignments
    # ------------------------------------------------------------------
    op.create_table(
        "user_department_assignments",
        sa.Column(
            "user_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "department_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("departments.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("is_primary", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column(
            "assigned_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    # ------------------------------------------------------------------
    # itsm_roles
    # ------------------------------------------------------------------
    op.create_table(
        "itsm_roles",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("name", sa.VARCHAR(100), nullable=False),
        sa.Column(
            "permissions",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("is_system", sa.Boolean, nullable=False, server_default=sa.text("false")),
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
    op.create_index("ix_itsm_roles_tenant_id", "itsm_roles", ["tenant_id"])

    # ------------------------------------------------------------------
    # user_itsm_roles
    # ------------------------------------------------------------------
    op.create_table(
        "user_itsm_roles",
        sa.Column(
            "user_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "role_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("itsm_roles.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "tenant_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "assigned_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "assigned_by",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_user_itsm_roles_tenant_id", "user_itsm_roles", ["tenant_id"])

    # ------------------------------------------------------------------
    # teams
    # ------------------------------------------------------------------
    op.create_table(
        "teams",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.VARCHAR(255), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column(
            "lead_id",
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
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_teams_tenant_id", "teams", ["tenant_id"])

    # ------------------------------------------------------------------
    # team_members
    # ------------------------------------------------------------------
    op.create_table(
        "team_members",
        sa.Column(
            "team_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("teams.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "user_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "joined_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    # ------------------------------------------------------------------
    # platform_audit_log  (INSERT only)
    # ------------------------------------------------------------------
    op.create_table(
        "platform_audit_log",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("actor_id", sa.VARCHAR(255), nullable=False),
        sa.Column("actor_email", sa.VARCHAR(320), nullable=False),
        sa.Column("platform_role", sa.VARCHAR(50), nullable=False),
        sa.Column(
            "tenant_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("http_method", sa.VARCHAR(10), nullable=False),
        sa.Column("endpoint", sa.VARCHAR(1024), nullable=False),
        sa.Column("request_id", sa.VARCHAR(255), nullable=False),
        sa.Column("ip_address", sa.VARCHAR(45), nullable=False),
        sa.Column(
            "accessed_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_pal_tenant_accessed_at", "platform_audit_log", ["tenant_id", "accessed_at"]
    )
    op.create_index(
        "ix_pal_actor_accessed_at", "platform_audit_log", ["actor_id", "accessed_at"]
    )

    # ------------------------------------------------------------------
    # tenant_sequences
    # ------------------------------------------------------------------
    op.create_table(
        "tenant_sequences",
        sa.Column(
            "tenant_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("prefix", sa.VARCHAR(10), primary_key=True),
        sa.Column("last_value", sa.BigInteger, nullable=False, server_default=sa.text("0")),
    )

    # ------------------------------------------------------------------
    # ticket_status_transitions  (state machine definition, seeded below)
    #
    # ticket_type = '' (empty string) means "applies to all ticket types".
    # This avoids NULL in a primary key column (PostgreSQL disallows NULL in PK).
    # Application layer queries: WHERE ticket_type = '' OR ticket_type = :current_type
    # ------------------------------------------------------------------
    op.create_table(
        "ticket_status_transitions",
        sa.Column("from_status", sa.VARCHAR(50), primary_key=True, nullable=False),
        sa.Column("to_status", sa.VARCHAR(50), primary_key=True, nullable=False),
        sa.Column(
            "ticket_type",
            sa.VARCHAR(50),
            primary_key=True,
            nullable=False,
            server_default=sa.text("''"),
            comment="'' = all types; 'change' = change tickets only",
        ),
    )

    # ------------------------------------------------------------------
    # Seed: ticket_status_transitions
    # ticket_type='' applies to incident, service_request, problem, and any
    # future type. ticket_type='change' overrides for change tickets only.
    # ------------------------------------------------------------------
    general_transitions = [
        ("open", "in_progress", ""),
        ("in_progress", "pending", ""),
        ("pending", "in_progress", ""),
        ("in_progress", "resolved", ""),
        ("resolved", "closed", ""),
        ("open", "cancelled", ""),
        ("in_progress", "cancelled", ""),
        ("pending", "cancelled", ""),
        # reopened → back to open
        ("resolved", "reopened", ""),
        ("reopened", "open", ""),
    ]

    # Change-specific transitions
    change_transitions = [
        ("open", "pending_approval", "change"),
        ("pending_approval", "approved", "change"),
        ("pending_approval", "rejected", "change"),
        ("approved", "in_progress", "change"),
        ("rejected", "cancelled", "change"),
    ]

    all_transitions = general_transitions + change_transitions

    for from_s, to_s, ttype in all_transitions:
        op.execute(
            sa.text(
                "INSERT INTO ticket_status_transitions (from_status, to_status, ticket_type) "
                "VALUES (:f, :t, :tt)"
            ).bindparams(f=from_s, t=to_s, tt=ttype)
        )

    # ------------------------------------------------------------------
    # Seed: itsm_roles system defaults (tenant_id=NULL, is_system=true)
    # Uses uuid_generate_v4() for deterministic-enough IDs in seed data;
    # application always uses uuid7 for new rows.
    # ------------------------------------------------------------------
    system_roles = [
        "end_user",
        "agent",
        "team_lead",
        "manager",
        "admin",
        "service_account",
    ]
    for role_name in system_roles:
        op.execute(
            sa.text(
                "INSERT INTO itsm_roles (id, tenant_id, name, permissions, is_system, "
                "created_at, updated_at) "
                "VALUES (uuid_generate_v4(), NULL, :name, '{}'::jsonb, true, now(), now())"
            ).bindparams(name=role_name)
        )

    # ------------------------------------------------------------------
    # Security: make platform_audit_log immutable
    # itsm_user is the application DB role (see 06-engineering-standards.md)
    # ------------------------------------------------------------------
    op.execute(
        sa.text("REVOKE UPDATE, DELETE ON platform_audit_log FROM itsm_user")
    )


def downgrade() -> None:
    # Drop in reverse FK order
    op.drop_table("ticket_status_transitions")
    op.drop_table("tenant_sequences")
    op.drop_index("ix_pal_actor_accessed_at", table_name="platform_audit_log")
    op.drop_index("ix_pal_tenant_accessed_at", table_name="platform_audit_log")
    op.drop_table("platform_audit_log")
    op.drop_table("team_members")
    op.drop_table("teams")
    op.drop_table("user_itsm_roles")
    op.drop_table("itsm_roles")
    op.drop_table("user_department_assignments")
    op.drop_table("users")
    op.drop_table("departments")
    op.drop_table("products")
    op.drop_table("tenants")
