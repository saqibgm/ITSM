"""Pydantic v2 request/response schemas for the Asset Management domain."""

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.asset import (
    AssetCondition,
    AssetHistoryAction,
    AssetStatus,
    MaintenanceStatus,
    MaintenanceType,
    RelationshipType,
)

# Re-export for callers that import from this module
__all__ = [
    "CreateAssetRequest",
    "UpdateAssetRequest",
    "AssetResponse",
    "AssignAssetRequest",
    "CreateMaintenanceRequest",
    "UpdateMaintenanceRequest",
    "MaintenanceResponse",
    "AssetHistoryResponse",
    "AttachmentPresignResponse",
    "AssetAttachmentResponse",
    "CreateAssetAttachmentRequest",
    "CreateRelationshipRequest",
    "AssetRelationshipResponse",
    "CreateAssetTypeRequest",
    "UpdateAssetTypeRequest",
    "AssetTypeResponse",
    "CreateVendorRequest",
    "UpdateVendorRequest",
    "VendorResponse",
    "CreateAssetCategoryRequest",
    "UpdateAssetCategoryRequest",
    "AssetCategoryResponse",
    "LinkTicketRequest",
    "LinkedTicketResponse",
    "LinkAssetRequest",
    "LinkedAssetResponse",
    "TicketImpactResponse",
    "ImpactAnalysisResponse",
    "AIPredictiveMaintenanceResponse",
]


# ---------------------------------------------------------------------------
# Asset schemas
# ---------------------------------------------------------------------------


class CreateAssetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=500)
    type_id: UUID
    description: str | None = None
    condition: AssetCondition = AssetCondition.new
    serial_number: str | None = None
    model_number: str | None = None
    manufacturer: str | None = None
    vendor_id: UUID | None = None
    purchase_date: date | None = None
    purchase_cost: Decimal | None = None
    warranty_expiry_date: date | None = None
    department_id: UUID | None = None
    product_id: UUID | None = None
    location: str | None = None
    custom_fields: dict = Field(default_factory=dict)


class UpdateAssetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=500)
    status: AssetStatus | None = None
    condition: AssetCondition | None = None
    description: str | None = None
    location: str | None = None
    department_id: UUID | None = None
    vendor_id: UUID | None = None
    warranty_expiry_date: date | None = None
    custom_fields: dict | None = None


class AssetResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    asset_tag: str
    name: str
    type_id: UUID
    status: AssetStatus
    condition: AssetCondition
    description: str | None = None
    serial_number: str | None = None
    model_number: str | None = None
    manufacturer: str | None = None
    vendor_id: UUID | None = None
    purchase_date: date | None = None
    purchase_cost: Decimal | None = None
    warranty_expiry_date: date | None = None
    department_id: UUID | None = None
    product_id: UUID | None = None
    location: str | None = None
    assigned_to: UUID | None = None
    assigned_at: datetime | None = None
    custom_fields: dict
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None


class AssignAssetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: UUID | None  # None = unassign


# ---------------------------------------------------------------------------
# Maintenance schemas
# ---------------------------------------------------------------------------


class CreateMaintenanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: MaintenanceType
    title: str = Field(min_length=1, max_length=500)
    description: str | None = None
    scheduled_date: date
    performed_by: UUID | None = None
    vendor_id: UUID | None = None
    cost: Decimal | None = None
    notes: str | None = None


class UpdateMaintenanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: MaintenanceStatus | None = None
    completed_date: date | None = None
    cost: Decimal | None = None
    notes: str | None = None
    performed_by: UUID | None = None


class MaintenanceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    asset_id: UUID
    type: MaintenanceType
    status: MaintenanceStatus
    title: str
    description: str | None = None
    scheduled_date: date
    completed_date: date | None = None
    cost: Decimal | None = None
    performed_by: UUID | None = None
    vendor_id: UUID | None = None
    notes: str | None = None
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# History schemas
# ---------------------------------------------------------------------------


class AssetHistoryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    asset_id: UUID
    actor_id: UUID
    action: AssetHistoryAction
    field_changed: str | None = None
    old_value: dict | None = None
    new_value: dict | None = None
    notes: str | None = None
    changed_at: datetime


# ---------------------------------------------------------------------------
# Attachment schemas
# ---------------------------------------------------------------------------


class AttachmentPresignResponse(BaseModel):
    upload_url: str
    attachment_id: UUID
    expires_in: int  # seconds


class AssetAttachmentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    asset_id: UUID
    uploaded_by: UUID
    filename: str
    storage_url: str
    file_size: int
    mime_type: str
    label: str | None = None
    created_at: datetime


class CreateAssetAttachmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: str = Field(min_length=1, max_length=500)
    file_size: int = Field(gt=0)
    mime_type: str = Field(min_length=1, max_length=255)
    label: str | None = Field(default=None, max_length=100)


# ---------------------------------------------------------------------------
# Relationship schemas (CMDB)
# ---------------------------------------------------------------------------


class CreateRelationshipRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_asset_id: UUID
    relationship_type: RelationshipType
    description: str | None = Field(default=None, max_length=500)


class AssetRelationshipResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    source_asset_id: UUID
    target_asset_id: UUID
    relationship_type: RelationshipType
    description: str | None = None
    created_by: UUID
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Asset Type schemas
# ---------------------------------------------------------------------------


class CreateAssetTypeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    category_id: UUID
    custom_fields_schema: dict = Field(default_factory=dict)


class UpdateAssetTypeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    category_id: UUID | None = None
    custom_fields_schema: dict | None = None


class AssetTypeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID | None = None  # None = global asset type
    name: str
    category_id: UUID
    custom_fields_schema: dict
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Vendor schemas
# ---------------------------------------------------------------------------


class CreateVendorRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    contact_email: str | None = Field(default=None, max_length=320)
    contact_phone: str | None = Field(default=None, max_length=50)
    website: str | None = Field(default=None, max_length=2048)
    address: dict | None = None


class UpdateVendorRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    contact_email: str | None = None
    contact_phone: str | None = None
    website: str | None = None
    address: dict | None = None
    is_active: bool | None = None


class VendorResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID | None = None  # None = global vendor
    name: str
    contact_email: str | None = None
    contact_phone: str | None = None
    website: str | None = None
    address: dict | None = None
    is_active: bool
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Asset Category schemas
# ---------------------------------------------------------------------------


class CreateAssetCategoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    parent_id: UUID | None = None
    icon: str | None = Field(default=None, max_length=255)


class UpdateAssetCategoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    parent_id: UUID | None = None
    icon: str | None = Field(default=None, max_length=255)
    is_active: bool | None = None


class AssetCategoryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID | None = None  # None = global category
    name: str
    description: str | None = None
    parent_id: UUID | None = None
    icon: str | None = None
    is_active: bool


# ---------------------------------------------------------------------------
# Asset-Ticket link schemas
# ---------------------------------------------------------------------------


class LinkTicketRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticket_id: UUID


class LinkedTicketResponse(BaseModel):
    """Minimal ticket projection returned when listing tickets linked to an asset."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    ticket_number: str
    title: str
    status: str
    priority: str


class LinkAssetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: UUID


class LinkedAssetResponse(BaseModel):
    """Minimal asset projection returned when listing assets linked to a ticket."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    asset_tag: str
    name: str
    status: str


# ---------------------------------------------------------------------------
# Impact Analysis schemas (CMDB)
# ---------------------------------------------------------------------------


class TicketImpactResponse(BaseModel):
    """Compact ticket summary surfaced inside an ImpactAnalysisResponse."""

    id: UUID
    ticket_number: str
    title: str
    status: str
    priority: str


class ImpactAnalysisResponse(BaseModel):
    asset_id: UUID
    affected_assets: list[AssetResponse]
    affected_tickets: list[TicketImpactResponse]
    total_affected_assets: int
    total_affected_tickets: int


# ---------------------------------------------------------------------------
# AI Predictive Maintenance schema
# ---------------------------------------------------------------------------


class AIPredictiveMaintenanceResponse(BaseModel):
    """Response shape for GET/POST ai-maintenance-prediction endpoints."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    asset_id: UUID
    predicted_due_date: date
    risk_score: float
    reason: str
    model_version: str
    acknowledged: bool
    acknowledged_by: UUID | None
    created_at: datetime
