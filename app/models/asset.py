"""
SQLAlchemy ORM models for the Asset Management domain.

All PKs are UUID v7 (sortable, time-prefixed).
AssetHistory is INSERT-only — enforced via REVOKE in migration.
"""

import enum
from datetime import date, datetime
from decimal import Decimal
from typing import Optional
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from uuid_extensions import uuid7

from app.models.base import Base, TimestampMixin


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class AssetStatus(str, enum.Enum):
    in_stock = "in_stock"
    in_use = "in_use"
    in_maintenance = "in_maintenance"
    retired = "retired"
    disposed = "disposed"
    lost = "lost"


class AssetCondition(str, enum.Enum):
    new = "new"
    good = "good"
    fair = "fair"
    poor = "poor"


class AssetHistoryAction(str, enum.Enum):
    created = "created"
    updated = "updated"
    assigned = "assigned"
    unassigned = "unassigned"
    status_changed = "status_changed"
    disposed = "disposed"
    maintenance_scheduled = "maintenance_scheduled"
    relationship_added = "relationship_added"
    relationship_removed = "relationship_removed"


class MaintenanceType(str, enum.Enum):
    scheduled = "scheduled"
    reactive = "reactive"
    preventive = "preventive"


class MaintenanceStatus(str, enum.Enum):
    scheduled = "scheduled"
    in_progress = "in_progress"
    completed = "completed"
    cancelled = "cancelled"


class RelationshipType(str, enum.Enum):
    runs_on = "runs_on"
    connected_to = "connected_to"
    depends_on = "depends_on"
    hosts = "hosts"
    part_of = "part_of"
    backs_up = "backs_up"


# ---------------------------------------------------------------------------
# AssetCategory  (reference data — no timestamps)
# ---------------------------------------------------------------------------


class AssetCategory(Base, TimestampMixin):
    """Hierarchical asset categories (e.g. Hardware > Laptop, Software > License)."""

    __tablename__ = "asset_categories"

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    # NULL tenant_id = global category, shared by all tenants (see 0012 migration)
    tenant_id: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    name: Mapped[str] = mapped_column(sa.VARCHAR(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    parent_id: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("asset_categories.id", ondelete="SET NULL"),
        nullable=True,
    )
    icon: Mapped[Optional[str]] = mapped_column(sa.VARCHAR(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)

    # relationships
    children: Mapped[list["AssetCategory"]] = relationship(
        "AssetCategory",
        back_populates="parent",
        cascade="all",
        foreign_keys="AssetCategory.parent_id",
    )
    parent: Mapped[Optional["AssetCategory"]] = relationship(
        "AssetCategory",
        back_populates="children",
        foreign_keys="AssetCategory.parent_id",
        remote_side="AssetCategory.id",
    )
    asset_types: Mapped[list["AssetType"]] = relationship(
        "AssetType", back_populates="category", cascade="all, delete-orphan"
    )


# ---------------------------------------------------------------------------
# AssetType
# ---------------------------------------------------------------------------


class AssetType(Base, TimestampMixin):
    """Defines a class of asset with an optional JSON Schema for custom fields.

    Examples: Laptop, Network Switch, Software License, Vehicle.
    """

    __tablename__ = "asset_types"

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    # NULL tenant_id = global asset type, shared by all tenants
    tenant_id: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    category_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("asset_categories.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(sa.VARCHAR(200), nullable=False)
    # JSON Schema definition for dynamic custom fields (rendered as a form in the UI)
    custom_fields_schema: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )

    category: Mapped["AssetCategory"] = relationship("AssetCategory", back_populates="asset_types")
    assets: Mapped[list["Asset"]] = relationship(
        "Asset", back_populates="asset_type", cascade="all, delete-orphan"
    )


# ---------------------------------------------------------------------------
# Vendor
# ---------------------------------------------------------------------------


class Vendor(Base, TimestampMixin):
    """External vendor/supplier of assets."""

    __tablename__ = "vendors"

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    # NULL tenant_id = global vendor, shared by all tenants
    tenant_id: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    name: Mapped[str] = mapped_column(sa.VARCHAR(255), nullable=False)
    contact_email: Mapped[Optional[str]] = mapped_column(sa.VARCHAR(320), nullable=True)
    contact_phone: Mapped[Optional[str]] = mapped_column(sa.VARCHAR(50), nullable=True)
    website: Mapped[Optional[str]] = mapped_column(sa.VARCHAR(2048), nullable=True)
    address: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)

    assets: Mapped[list["Asset"]] = relationship("Asset", back_populates="vendor")
    maintenance_records: Mapped[list["AssetMaintenance"]] = relationship(
        "AssetMaintenance", back_populates="vendor"
    )


# ---------------------------------------------------------------------------
# Asset
# ---------------------------------------------------------------------------


class Asset(Base, TimestampMixin):
    """A physical or virtual asset tracked in the CMDB.

    ``asset_tag`` is auto-generated (AST-00001) and unique per tenant.
    ``deleted_at`` is set for soft deletes — hard delete never happens via API.
    """

    __tablename__ = "assets"
    __table_args__ = (
        sa.UniqueConstraint("tenant_id", "asset_tag", name="uq_assets_tenant_asset_tag"),
        sa.Index("ix_assets_tenant_status", "tenant_id", "status"),
        sa.Index("ix_assets_tenant_assigned_to", "tenant_id", "assigned_to"),
        sa.Index("ix_assets_tenant_type_id", "tenant_id", "type_id"),
        sa.Index("ix_assets_tenant_deleted_at", "tenant_id", "deleted_at"),
    )

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    asset_tag: Mapped[str] = mapped_column(sa.VARCHAR(50), nullable=False)
    tenant_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    product_id: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("products.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    department_id: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("departments.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    type_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("asset_types.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(sa.VARCHAR(500), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    status: Mapped[AssetStatus] = mapped_column(
        sa.Enum(AssetStatus, name="assetstatus", create_type=False),
        nullable=False,
        default=AssetStatus.in_stock,
        server_default=sa.text("'in_stock'"),
    )
    condition: Mapped[AssetCondition] = mapped_column(
        sa.Enum(AssetCondition, name="assetcondition", create_type=False),
        nullable=False,
        default=AssetCondition.new,
        server_default=sa.text("'new'"),
    )
    serial_number: Mapped[Optional[str]] = mapped_column(sa.VARCHAR(255), nullable=True)
    model_number: Mapped[Optional[str]] = mapped_column(sa.VARCHAR(255), nullable=True)
    manufacturer: Mapped[Optional[str]] = mapped_column(sa.VARCHAR(255), nullable=True)
    vendor_id: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("vendors.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    purchase_date: Mapped[Optional[date]] = mapped_column(sa.Date, nullable=True)
    purchase_cost: Mapped[Optional[Decimal]] = mapped_column(
        sa.Numeric(12, 2), nullable=True
    )
    warranty_expiry_date: Mapped[Optional[date]] = mapped_column(sa.Date, nullable=True)
    assigned_to: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    assigned_at: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    location: Mapped[Optional[str]] = mapped_column(sa.VARCHAR(500), nullable=True)
    location_dept_id: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("departments.id", ondelete="SET NULL"),
        nullable=True,
    )
    custom_fields: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    metadata_: Mapped[dict] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        server_default=sa.text("'{}'::jsonb"),
    )
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )

    # relationships
    asset_type: Mapped["AssetType"] = relationship("AssetType", back_populates="assets")
    vendor: Mapped[Optional["Vendor"]] = relationship("Vendor", back_populates="assets")
    assignee: Mapped[Optional["User"]] = relationship(
        "User", foreign_keys=[assigned_to]
    )
    history: Mapped[list["AssetHistory"]] = relationship(
        "AssetHistory", back_populates="asset", cascade="all, delete-orphan"
    )
    maintenance_records: Mapped[list["AssetMaintenance"]] = relationship(
        "AssetMaintenance", back_populates="asset", cascade="all, delete-orphan"
    )
    attachments: Mapped[list["AssetAttachment"]] = relationship(
        "AssetAttachment", back_populates="asset", cascade="all, delete-orphan"
    )
    ticket_links: Mapped[list["AssetTicketLink"]] = relationship(
        "AssetTicketLink", back_populates="asset", cascade="all, delete-orphan"
    )
    relationships_as_source: Mapped[list["AssetRelationship"]] = relationship(
        "AssetRelationship",
        back_populates="source_asset",
        foreign_keys="AssetRelationship.source_asset_id",
        cascade="all, delete-orphan",
    )
    relationships_as_target: Mapped[list["AssetRelationship"]] = relationship(
        "AssetRelationship",
        back_populates="target_asset",
        foreign_keys="AssetRelationship.target_asset_id",
        cascade="all, delete-orphan",
    )


# ---------------------------------------------------------------------------
# AssetStatusTransition  (seeded, read-only in app)
# ---------------------------------------------------------------------------


class AssetStatusTransition(Base):
    """State machine definition for asset status.

    Seeded at migration time. App never inserts/updates these rows.
    """

    __tablename__ = "asset_status_transitions"

    from_status: Mapped[str] = mapped_column(sa.VARCHAR(30), primary_key=True)
    to_status: Mapped[str] = mapped_column(sa.VARCHAR(30), primary_key=True)


# ---------------------------------------------------------------------------
# AssetRelationship  (CMDB directed graph)
# ---------------------------------------------------------------------------


class AssetRelationship(Base):
    """Directed relationship between two assets (CMDB graph edge).

    Enables impact analysis: traverse source → target up to N hops.
    """

    __tablename__ = "asset_relationships"
    __table_args__ = (
        sa.UniqueConstraint(
            "source_asset_id",
            "target_asset_id",
            "relationship_type",
            name="uq_asset_relationships",
        ),
        sa.Index("ix_asset_rel_tenant_source", "tenant_id", "source_asset_id"),
        sa.Index("ix_asset_rel_tenant_target", "tenant_id", "target_asset_id"),
    )

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_asset_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("assets.id", ondelete="CASCADE"),
        nullable=False,
    )
    target_asset_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("assets.id", ondelete="CASCADE"),
        nullable=False,
    )
    relationship_type: Mapped[RelationshipType] = mapped_column(
        sa.Enum(RelationshipType, name="relationshiptype", create_type=False),
        nullable=False,
    )
    description: Mapped[Optional[str]] = mapped_column(sa.VARCHAR(500), nullable=True)
    created_by: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
    )

    source_asset: Mapped["Asset"] = relationship(
        "Asset",
        back_populates="relationships_as_source",
        foreign_keys=[source_asset_id],
    )
    target_asset: Mapped["Asset"] = relationship(
        "Asset",
        back_populates="relationships_as_target",
        foreign_keys=[target_asset_id],
    )
    creator: Mapped["User"] = relationship("User", foreign_keys=[created_by])


# ---------------------------------------------------------------------------
# AssetHistory  (INSERT only — REVOKE UPDATE, DELETE enforced in migration)
# ---------------------------------------------------------------------------


class AssetHistory(Base):
    """Immutable audit trail for every asset change.

    Written only via AssetRepository.record_history().
    REVOKE UPDATE, DELETE ON asset_history enforced in migration 0004.
    """

    __tablename__ = "asset_history"
    __table_args__ = (
        sa.Index("ix_asset_history_asset_changed_at", "asset_id", "changed_at"),
    )

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    asset_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("assets.id", ondelete="CASCADE"),
        nullable=False,
    )
    actor_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    action: Mapped[AssetHistoryAction] = mapped_column(
        sa.Enum(AssetHistoryAction, name="assethistoryaction", create_type=False),
        nullable=False,
    )
    field_changed: Mapped[Optional[str]] = mapped_column(sa.VARCHAR(100), nullable=True)
    old_value: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    new_value: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    changed_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )

    asset: Mapped["Asset"] = relationship("Asset", back_populates="history")
    actor: Mapped["User"] = relationship("User", foreign_keys=[actor_id])


# ---------------------------------------------------------------------------
# AssetMaintenance
# ---------------------------------------------------------------------------


class AssetMaintenance(Base, TimestampMixin):
    """A single maintenance event (scheduled, reactive, or preventive)."""

    __tablename__ = "asset_maintenance"

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    asset_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("assets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    type: Mapped[MaintenanceType] = mapped_column(
        sa.Enum(MaintenanceType, name="maintenancetype", create_type=False),
        nullable=False,
    )
    status: Mapped[MaintenanceStatus] = mapped_column(
        sa.Enum(MaintenanceStatus, name="maintenancestatus", create_type=False),
        nullable=False,
        default=MaintenanceStatus.scheduled,
        server_default=sa.text("'scheduled'"),
    )
    title: Mapped[str] = mapped_column(sa.VARCHAR(500), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    scheduled_date: Mapped[date] = mapped_column(sa.Date, nullable=False)
    completed_date: Mapped[Optional[date]] = mapped_column(sa.Date, nullable=True)
    cost: Mapped[Optional[Decimal]] = mapped_column(sa.Numeric(12, 2), nullable=True)
    performed_by: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    vendor_id: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("vendors.id", ondelete="SET NULL"),
        nullable=True,
    )
    notes: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)

    asset: Mapped["Asset"] = relationship("Asset", back_populates="maintenance_records")
    vendor: Mapped[Optional["Vendor"]] = relationship(
        "Vendor", back_populates="maintenance_records"
    )
    performer: Mapped[Optional["User"]] = relationship(
        "User", foreign_keys=[performed_by]
    )


# ---------------------------------------------------------------------------
# AssetAttachment
# ---------------------------------------------------------------------------


class AssetAttachment(Base):
    """File attached to an asset (invoice, photo, manual, warranty certificate)."""

    __tablename__ = "asset_attachments"

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    asset_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("assets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    uploaded_by: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    filename: Mapped[str] = mapped_column(sa.VARCHAR(500), nullable=False)
    storage_url: Mapped[str] = mapped_column(sa.VARCHAR(2048), nullable=False)
    file_size: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    mime_type: Mapped[str] = mapped_column(sa.VARCHAR(255), nullable=False)
    label: Mapped[Optional[str]] = mapped_column(
        sa.VARCHAR(100), nullable=True, comment="invoice, photo, manual, warranty"
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )

    asset: Mapped["Asset"] = relationship("Asset", back_populates="attachments")
    uploader: Mapped["User"] = relationship("User", foreign_keys=[uploaded_by])


# ---------------------------------------------------------------------------
# AssetTicketLink  (M:N, assets ↔ tickets)
# ---------------------------------------------------------------------------


class AssetTicketLink(Base):
    """Junction table linking an asset to a related support ticket."""

    __tablename__ = "asset_ticket_links"

    asset_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("assets.id", ondelete="CASCADE"),
        primary_key=True,
    )
    ticket_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("tickets.id", ondelete="CASCADE"),
        primary_key=True,
    )
    linked_by: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    linked_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )

    asset: Mapped["Asset"] = relationship("Asset", back_populates="ticket_links")
    linker: Mapped["User"] = relationship("User", foreign_keys=[linked_by])
