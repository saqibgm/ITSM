"""
SQLAlchemy ORM models for the Identity & Access domain.

All PKs are UUID v7 (sortable, time-prefixed).
No passwords are stored anywhere in this module — ITSM is a pure OIDC resource server.
"""

from datetime import datetime
from typing import Optional
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from uuid_extensions import uuid7

from app.models.base import Base, TimestampMixin

# ---------------------------------------------------------------------------
# Sentinel for "field not provided" — lets PATCH endpoints distinguish
# between "not sent" and "sent as null".
# ---------------------------------------------------------------------------
_UNSET = object()


# ---------------------------------------------------------------------------
# Tenant
# ---------------------------------------------------------------------------


class Tenant(Base, TimestampMixin):
    """Top-level isolation boundary — one row per IAM Organisation."""

    __tablename__ = "tenants"

    id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True), primary_key=True, default=uuid7
    )
    iam_org_id: Mapped[str] = mapped_column(sa.VARCHAR(255), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(sa.VARCHAR(255), nullable=False)
    slug: Mapped[str] = mapped_column(sa.VARCHAR(100), unique=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    settings: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=sa.text("'{}'::jsonb"))

    # Per-tenant monthly AI token budget cap (NULL = use platform default).
    ai_budget_monthly_tokens: Mapped[Optional[int]] = mapped_column(
        sa.BigInteger, nullable=True
    )

    # Soft-delete timestamp — NULL means the tenant is live.
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )

    # relationships
    products: Mapped[list["Product"]] = relationship(
        "Product", back_populates="tenant", cascade="all, delete-orphan"
    )
    departments: Mapped[list["Department"]] = relationship(
        "Department", back_populates="tenant", cascade="all, delete-orphan"
    )
    users: Mapped[list["User"]] = relationship(
        "User", back_populates="tenant", cascade="all, delete-orphan"
    )
    teams: Mapped[list["Team"]] = relationship(
        "Team", back_populates="tenant", cascade="all, delete-orphan"
    )
    sequences: Mapped[list["TenantSequence"]] = relationship(
        "TenantSequence", back_populates="tenant", cascade="all, delete-orphan"
    )


# ---------------------------------------------------------------------------
# Product
# ---------------------------------------------------------------------------


class Product(Base, TimestampMixin):
    """A product/module enabled for a tenant (e.g. ITSM, KB, Assets)."""

    __tablename__ = "products"
    __table_args__ = (sa.UniqueConstraint("tenant_id", "slug", name="uq_products_tenant_slug"),)

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(sa.VARCHAR(255), nullable=False)
    slug: Mapped[str] = mapped_column(sa.VARCHAR(100), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)

    tenant: Mapped["Tenant"] = relationship("Tenant", back_populates="products")


# ---------------------------------------------------------------------------
# Department
# ---------------------------------------------------------------------------


class Department(Base, TimestampMixin):
    """Mirrors an IAM Facility; supports tree hierarchy via self-referencing FK."""

    __tablename__ = "departments"

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    # NULL tenant_id = global department, shared by all tenants
    tenant_id: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    iam_facility_id: Mapped[Optional[str]] = mapped_column(sa.VARCHAR(255), nullable=True)
    name: Mapped[str] = mapped_column(sa.VARCHAR(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    parent_id: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("departments.id", ondelete="SET NULL"),
        nullable=True,
    )
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)

    tenant: Mapped["Tenant"] = relationship("Tenant", back_populates="departments")
    children: Mapped[list["Department"]] = relationship(
        "Department",
        back_populates="parent",
        cascade="all",
        foreign_keys="Department.parent_id",
    )
    parent: Mapped[Optional["Department"]] = relationship(
        "Department",
        back_populates="children",
        foreign_keys="Department.parent_id",
        remote_side="Department.id",
    )


# ---------------------------------------------------------------------------
# User  (local IAM mirror — no password field, ever)
# ---------------------------------------------------------------------------


class User(Base, TimestampMixin):
    """Local mirror of an IAM User. Populated on first login; synced by Celery."""

    __tablename__ = "users"
    __table_args__ = (
        # A user (IAM identity) can be mirrored per-tenant — platform/99T staff
        # acting on multiple tenants get one local row each. Uniqueness is on the
        # (iam_user_id, tenant_id) pair, not iam_user_id alone (see migration 0018).
        sa.UniqueConstraint("iam_user_id", "tenant_id", name="uq_users_iam_tenant"),
    )

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    iam_user_id: Mapped[str] = mapped_column(sa.VARCHAR(255), nullable=False, index=True)
    tenant_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    email: Mapped[str] = mapped_column(sa.VARCHAR(320), nullable=False)
    first_name: Mapped[str] = mapped_column(sa.VARCHAR(100), nullable=False)
    last_name: Mapped[str] = mapped_column(sa.VARCHAR(100), nullable=False)
    avatar_url: Mapped[Optional[str]] = mapped_column(sa.VARCHAR(2048), nullable=True)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    metadata_: Mapped[dict] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        server_default=sa.text("'{}'::jsonb"),
    )
    # IAM roles (realm + project-iq client roles) captured from the token on each
    # login — IAM is the source of truth; this is a synced read-only mirror.
    iam_roles: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'[]'::jsonb"), default=list,
    )
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )

    tenant: Mapped["Tenant"] = relationship("Tenant", back_populates="users")
    department_assignments: Mapped[list["UserDepartmentAssignment"]] = relationship(
        "UserDepartmentAssignment",
        back_populates="user",
        cascade="all, delete-orphan",
    )
    itsm_role_assignments: Mapped[list["UserITSMRole"]] = relationship(
        "UserITSMRole",
        back_populates="user",
        foreign_keys="UserITSMRole.user_id",
        cascade="all, delete-orphan",
    )
    team_memberships: Mapped[list["TeamMember"]] = relationship(
        "TeamMember",
        back_populates="user",
        cascade="all, delete-orphan",
    )


# ---------------------------------------------------------------------------
# UserDepartmentAssignment  (M:N, users ↔ departments)
# ---------------------------------------------------------------------------


class UserDepartmentAssignment(Base):
    """Assigns a user to one or more departments; exactly one may be primary."""

    __tablename__ = "user_department_assignments"

    user_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    department_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("departments.id", ondelete="CASCADE"),
        primary_key=True,
    )
    is_primary: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    assigned_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )

    user: Mapped["User"] = relationship("User", back_populates="department_assignments")
    department: Mapped["Department"] = relationship("Department")


# ---------------------------------------------------------------------------
# ITSMRole
# ---------------------------------------------------------------------------


class ITSMRole(Base, TimestampMixin):
    """ITSM role definition. tenant_id=NULL means a system-wide default role."""

    __tablename__ = "itsm_roles"

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    name: Mapped[str] = mapped_column(sa.VARCHAR(100), nullable=False)
    permissions: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    is_system: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)

    assignments: Mapped[list["UserITSMRole"]] = relationship(
        "UserITSMRole", back_populates="role", cascade="all, delete-orphan"
    )


# ---------------------------------------------------------------------------
# UserITSMRole  (M:N, users ↔ itsm_roles)
# ---------------------------------------------------------------------------


class UserITSMRole(Base):
    """Assigns an ITSM role to a user within a tenant.

    ``tenant_id`` is denormalized for query performance.
    ``assigned_by`` is nullable (NULL = auto-provisioned on first login).
    """

    __tablename__ = "user_itsm_roles"

    user_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    role_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("itsm_roles.id", ondelete="CASCADE"),
        primary_key=True,
    )
    tenant_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    assigned_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )
    assigned_by: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    user: Mapped["User"] = relationship(
        "User", back_populates="itsm_role_assignments", foreign_keys=[user_id]
    )
    role: Mapped["ITSMRole"] = relationship("ITSMRole", back_populates="assignments")
    assigner: Mapped[Optional["User"]] = relationship("User", foreign_keys=[assigned_by])


# ---------------------------------------------------------------------------
# Team
# ---------------------------------------------------------------------------


class Team(Base, TimestampMixin):
    """A team of agents/users within a tenant."""

    __tablename__ = "teams"

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(sa.VARCHAR(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    lead_id: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    tenant: Mapped["Tenant"] = relationship("Tenant", back_populates="teams")
    lead: Mapped[Optional["User"]] = relationship("User", foreign_keys=[lead_id])
    members: Mapped[list["TeamMember"]] = relationship(
        "TeamMember", back_populates="team", cascade="all, delete-orphan"
    )


# ---------------------------------------------------------------------------
# TeamMember  (M:N, teams ↔ users)
# ---------------------------------------------------------------------------


class TeamMember(Base):
    """Membership record linking a user to a team."""

    __tablename__ = "team_members"

    team_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("teams.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    joined_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )

    team: Mapped["Team"] = relationship("Team", back_populates="members")
    user: Mapped["User"] = relationship("User", back_populates="team_memberships")


# ---------------------------------------------------------------------------
# PlatformAuditLog  (INSERT only — no UPDATE or DELETE, ever)
# ---------------------------------------------------------------------------


class PlatformAuditLog(Base):
    """Immutable audit trail for every cross-tenant platform access.

    Retained minimum 2 years. REVOKE UPDATE, DELETE enforced in migration.
    """

    __tablename__ = "platform_audit_log"
    __table_args__ = (
        sa.Index("ix_pal_tenant_accessed_at", "tenant_id", "accessed_at"),
        sa.Index("ix_pal_actor_accessed_at", "actor_id", "accessed_at"),
    )

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    actor_id: Mapped[str] = mapped_column(sa.VARCHAR(255), nullable=False)
    actor_email: Mapped[str] = mapped_column(sa.VARCHAR(320), nullable=False)
    platform_role: Mapped[str] = mapped_column(sa.VARCHAR(50), nullable=False)
    tenant_id: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("tenants.id", ondelete="SET NULL"),
        nullable=True,
    )
    http_method: Mapped[str] = mapped_column(sa.VARCHAR(10), nullable=False)
    endpoint: Mapped[str] = mapped_column(sa.VARCHAR(1024), nullable=False)
    request_id: Mapped[str] = mapped_column(sa.VARCHAR(255), nullable=False)
    ip_address: Mapped[str] = mapped_column(sa.VARCHAR(45), nullable=False)  # supports IPv6
    accessed_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )
    # Structured action label and detail payload for platform-mutating operations.
    # NULL for read-only access log rows written by get_current_user.
    action: Mapped[Optional[str]] = mapped_column(sa.VARCHAR(255), nullable=True)
    details: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)


# ---------------------------------------------------------------------------
# TenantSequence  (per-tenant atomic counters for ticket/asset numbers)
# ---------------------------------------------------------------------------


class TenantSequence(Base):
    """Stores the last-used sequence value per (tenant, prefix).

    Format: ``{PREFIX}-{LPAD(last_value+1, 5, '0')}`` → ``INC-00001``
    """

    __tablename__ = "tenant_sequences"

    tenant_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("tenants.id", ondelete="CASCADE"),
        primary_key=True,
    )
    prefix: Mapped[str] = mapped_column(
        sa.VARCHAR(10),
        primary_key=True,
        comment="INC, REQ, PRB, CHG, AST",
    )
    last_value: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0)

    tenant: Mapped["Tenant"] = relationship("Tenant", back_populates="sequences")
