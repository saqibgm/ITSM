"""Asset Management API endpoints.

Route ordering note: static-path routes (e.g. /asset-types) are registered
BEFORE parametric routes (/{asset_id}) to prevent FastAPI from capturing literal
path segments as UUID parameters.

Routers exported:
  router          → /assets
  asset_types_router → /asset-types
  vendors_router  → /vendors
"""

from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1._refscope import ref_create_tenant_id, ref_visible_filter, require_ref_write
from app.auth.dependencies import CurrentUser, get_current_user
from app.database import get_db
from app.exceptions import AuthorizationError, ResourceNotFoundError
from app.models.asset import (
    Asset,
    AssetAttachment,
    AssetCategory,
    AssetCondition,
    AssetHistory,
    AssetHistoryAction,
    AssetMaintenance,
    AssetRelationship,
    AssetStatus,
    AssetType,
    MaintenanceStatus,
    Vendor,
)
from app.repositories.asset_repo import AssetRepository
from app.schemas.asset import (
    AIPredictiveMaintenanceResponse,
    AssignAssetRequest,
    AssetAttachmentResponse,
    AssetCategoryResponse,
    AssetHistoryResponse,
    AssetRelationshipResponse,
    AssetResponse,
    AssetTypeResponse,
    AttachmentPresignResponse,
    CreateAssetAttachmentRequest,
    CreateAssetCategoryRequest,
    CreateAssetRequest,
    CreateAssetTypeRequest,
    CreateMaintenanceRequest,
    CreateRelationshipRequest,
    CreateVendorRequest,
    ImpactAnalysisResponse,
    LinkTicketRequest,
    LinkedTicketResponse,
    MaintenanceResponse,
    TicketImpactResponse,
    UpdateAssetCategoryRequest,
    UpdateAssetRequest,
    UpdateAssetTypeRequest,
    UpdateMaintenanceRequest,
    UpdateVendorRequest,
    VendorResponse,
)
from app.schemas.common import PaginatedResponse
from app.services.asset_service import AssetService, CreateAssetData
from app.services.storage_service import get_storage_service

router = APIRouter(prefix="/assets", tags=["assets"])
asset_types_router = APIRouter(prefix="/asset-types", tags=["asset-types"])
asset_categories_router = APIRouter(prefix="/asset-categories", tags=["asset-categories"])
vendors_router = APIRouter(prefix="/vendors", tags=["vendors"])

_service = AssetService()

# Role sets
_AGENT_ROLES = {"agent", "team_lead", "manager", "admin"}
_TEAM_LEAD_ROLES = {"team_lead", "manager", "admin"}
_MANAGER_ROLES = {"manager", "admin"}
_ADMIN_ROLES = {"admin"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_tenant(current_user: CurrentUser) -> None:
    if current_user.tenant_id is None or current_user.local_user_id is None:
        raise AuthorizationError("Tenant context required")


def _resolve_list_scope(current_user: CurrentUser) -> UUID | None:
    """Tenant scope for list/read endpoints — see tickets._resolve_list_scope.

    Tenant users: their own tenant_id. Platform users: their selected tenant,
    or None for the cross-tenant "all tenants" view (repo omits the filter).
    """
    if current_user.tenant_id is not None:
        return current_user.tenant_id
    if current_user.tier == "platform":
        return None
    raise AuthorizationError("Tenant context required")


def _has_role(current_user: CurrentUser, roles: set[str]) -> bool:
    return bool(set(current_user.roles) & roles)


def _require_any_role(current_user: CurrentUser, roles: set[str], action: str) -> None:
    if not _has_role(current_user, roles):
        raise AuthorizationError(
            f"One of the following roles is required to {action}: {', '.join(sorted(roles))}"
        )


# ============================================================================
# /assets  — Asset CRUD
# ============================================================================


@router.get("", response_model=PaginatedResponse[AssetResponse])
async def list_assets(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    status: list[AssetStatus] | None = Query(default=None),
    condition: list[AssetCondition] | None = Query(default=None),
    type_id: UUID | None = Query(default=None),
    category_id: UUID | None = Query(default=None),
    department_id: UUID | None = Query(default=None),
    assigned_to: UUID | None = Query(default=None),
    vendor_id: UUID | None = Query(default=None),
    search: str | None = Query(default=None, max_length=200),
    warranty_expiring_before: date | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=25, ge=1, le=100),
) -> PaginatedResponse[AssetResponse]:
    """List assets for the tenant.

    - Agents and above see all assets.
    - End users see only assets assigned to themselves.
    - Soft-deleted assets are always excluded.
    """
    tenant_id = _resolve_list_scope(current_user)

    # End users can only see their own assigned assets
    if not _has_role(current_user, _AGENT_ROLES):
        assigned_to = current_user.local_user_id

    repo = AssetRepository(db)
    assets, next_cursor = await repo.list_assets(
        tenant_id=tenant_id,
        status=status,
        condition=condition,
        type_id=type_id,
        category_id=category_id,
        department_id=department_id,
        assigned_to=assigned_to,
        vendor_id=vendor_id,
        search=search,
        warranty_expiring_before=warranty_expiring_before,
        cursor=cursor,
        limit=limit,
    )
    return PaginatedResponse(
        items=[AssetResponse.model_validate(a) for a in assets],
        next_cursor=next_cursor,
    )


@router.post("", response_model=AssetResponse, status_code=201)
async def create_asset(
    body: CreateAssetRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AssetResponse:
    """Create a new asset. Requires team_lead or above."""
    _require_tenant(current_user)
    _require_any_role(current_user, _TEAM_LEAD_ROLES, "create assets")

    data = CreateAssetData(
        name=body.name,
        type_id=body.type_id,
        description=body.description,
        condition=body.condition,
        serial_number=body.serial_number,
        model_number=body.model_number,
        manufacturer=body.manufacturer,
        vendor_id=body.vendor_id,
        purchase_date=body.purchase_date,
        purchase_cost=body.purchase_cost,
        warranty_expiry_date=body.warranty_expiry_date,
        department_id=body.department_id,
        product_id=body.product_id,
        location=body.location,
        custom_fields=body.custom_fields or {},
    )
    asset = await _service.create_asset(
        tenant_id=current_user.tenant_id,
        actor_id=current_user.local_user_id,
        data=data,
        db=db,
    )
    await db.commit()
    await db.refresh(asset)

    # Webhook fan-out — fire-and-forget via Celery; never blocks the response
    try:
        from app.workers.tasks_webhooks import dispatch_webhook_event
        dispatch_webhook_event.delay(
            str(current_user.tenant_id),
            "asset.created",
            AssetResponse.model_validate(asset).model_dump(mode="json"),
        )
    except Exception:
        pass  # webhook dispatch failure must never fail the asset creation

    return AssetResponse.model_validate(asset)


@router.get("/{asset_id}", response_model=AssetResponse)
async def get_asset(
    asset_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AssetResponse:
    """Get a single asset. Soft-deleted assets return 404."""
    tenant_id = _resolve_list_scope(current_user)

    repo = AssetRepository(db)
    asset = await repo.get_or_404(asset_id, tenant_id)

    # End users may only see assets assigned to them
    if not _has_role(current_user, _AGENT_ROLES):
        if asset.assigned_to != current_user.local_user_id:
            raise ResourceNotFoundError("assets", str(asset_id))

    return AssetResponse.model_validate(asset)


@router.patch("/{asset_id}", response_model=AssetResponse)
async def update_asset(
    asset_id: UUID,
    body: UpdateAssetRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AssetResponse:
    """Partially update an asset. Status changes go through the state machine."""
    _require_tenant(current_user)
    _require_any_role(current_user, _TEAM_LEAD_ROLES, "update assets")

    repo = AssetRepository(db)
    asset = await repo.get_or_404(asset_id, current_user.tenant_id)

    updates = body.model_dump(exclude_unset=True)
    asset = await _service.update_asset(
        asset=asset,
        updates=updates,
        actor_id=current_user.local_user_id,
        db=db,
    )
    await db.commit()
    await db.refresh(asset)
    return AssetResponse.model_validate(asset)


@router.delete("/{asset_id}", status_code=204)
async def delete_asset(
    asset_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Soft-delete an asset (sets deleted_at). Requires manager or admin."""
    _require_tenant(current_user)
    _require_any_role(current_user, _MANAGER_ROLES, "delete assets")

    repo = AssetRepository(db)
    asset = await repo.get_or_404(asset_id, current_user.tenant_id)

    from datetime import datetime

    asset.deleted_at = datetime.utcnow()
    await repo.record_history(
        asset_id=asset.id,
        actor_id=current_user.local_user_id,
        action=AssetHistoryAction.disposed,
        notes="Asset soft-deleted",
    )
    await db.commit()


# ============================================================================
# /assets/{asset_id}/assign  — Assignment
# ============================================================================


@router.post("/{asset_id}/assign", response_model=AssetResponse)
async def assign_asset(
    asset_id: UUID,
    body: AssignAssetRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AssetResponse:
    """Assign (or re-assign) an asset to a user. Requires team_lead or above."""
    _require_tenant(current_user)
    _require_any_role(current_user, _TEAM_LEAD_ROLES, "assign assets")

    repo = AssetRepository(db)
    asset = await repo.get_or_404(asset_id, current_user.tenant_id)

    asset = await _service.assign_asset(
        asset=asset,
        user_id=body.user_id,
        actor_id=current_user.local_user_id,
        db=db,
    )
    await db.commit()
    await db.refresh(asset)
    return AssetResponse.model_validate(asset)


@router.delete("/{asset_id}/assign", response_model=AssetResponse)
async def unassign_asset(
    asset_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AssetResponse:
    """Unassign (clear) the current assignee from an asset."""
    _require_tenant(current_user)
    _require_any_role(current_user, _TEAM_LEAD_ROLES, "unassign assets")

    repo = AssetRepository(db)
    asset = await repo.get_or_404(asset_id, current_user.tenant_id)

    asset = await _service.assign_asset(
        asset=asset,
        user_id=None,
        actor_id=current_user.local_user_id,
        db=db,
    )
    await db.commit()
    await db.refresh(asset)
    return AssetResponse.model_validate(asset)


# ============================================================================
# /assets/{asset_id}/maintenance
# ============================================================================


@router.get("/{asset_id}/maintenance", response_model=list[MaintenanceResponse])
async def list_maintenance(
    asset_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[MaintenanceResponse]:
    """List all maintenance records for an asset."""
    _require_tenant(current_user)

    repo = AssetRepository(db)
    await repo.get_or_404(asset_id, current_user.tenant_id)

    result = await db.execute(
        select(AssetMaintenance)
        .where(AssetMaintenance.asset_id == asset_id)
        .order_by(AssetMaintenance.scheduled_date.desc())
    )
    records = list(result.scalars().all())
    return [MaintenanceResponse.model_validate(r) for r in records]


@router.post("/{asset_id}/maintenance", response_model=MaintenanceResponse, status_code=201)
async def create_maintenance(
    asset_id: UUID,
    body: CreateMaintenanceRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MaintenanceResponse:
    """Log a maintenance record for an asset. Requires agent or above.

    Auto-transitions:
    - reactive type: maintenance status set to ``in_progress`` immediately.
    - asset status changed to ``in_maintenance`` via state machine (any type).
    """
    _require_tenant(current_user)
    _require_any_role(current_user, _AGENT_ROLES, "log maintenance")

    from app.models.asset import MaintenanceType

    repo = AssetRepository(db)
    asset = await repo.get_or_404(asset_id, current_user.tenant_id)

    # Reactive maintenance starts immediately; others are scheduled
    initial_status = (
        MaintenanceStatus.in_progress
        if body.type == MaintenanceType.reactive
        else MaintenanceStatus.scheduled
    )

    record = AssetMaintenance(
        asset_id=asset_id,
        type=body.type,
        status=initial_status,
        title=body.title,
        description=body.description,
        scheduled_date=body.scheduled_date,
        performed_by=body.performed_by,
        vendor_id=body.vendor_id,
        cost=body.cost,
        notes=body.notes,
    )
    db.add(record)
    await db.flush()

    # Transition asset to in_maintenance via state machine (if not already there)
    if asset.status != AssetStatus.in_maintenance:
        valid = await repo.get_valid_transitions(asset.status)
        if AssetStatus.in_maintenance.value in valid:
            old_status = asset.status
            asset.status = AssetStatus.in_maintenance
            await repo.update(asset)
            await repo.record_history(
                asset_id=asset_id,
                actor_id=current_user.local_user_id,
                action=AssetHistoryAction.status_changed,
                field="status",
                old_val=old_status.value,
                new_val=AssetStatus.in_maintenance.value,
                notes="Status auto-changed on maintenance creation",
            )

    await repo.record_history(
        asset_id=asset_id,
        actor_id=current_user.local_user_id,
        action=AssetHistoryAction.maintenance_scheduled,
        notes=f"Maintenance logged: {body.title} (status={initial_status.value})",
    )
    await db.commit()
    await db.refresh(record)
    return MaintenanceResponse.model_validate(record)


@router.patch(
    "/{asset_id}/maintenance/{m_id}", response_model=MaintenanceResponse
)
async def update_maintenance(
    asset_id: UUID,
    m_id: UUID,
    body: UpdateMaintenanceRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MaintenanceResponse:
    """Update a maintenance record's status, completed_date, or cost.

    When status transitions to ``completed`` and the asset is currently
    ``in_maintenance``, the asset status is reverted to ``in_use`` via the
    state machine (validated before any write; HTTP 409 if invalid).
    """
    _require_tenant(current_user)
    _require_any_role(current_user, _AGENT_ROLES, "update maintenance")

    from app.exceptions import InvalidStateTransitionError

    repo = AssetRepository(db)
    asset = await repo.get_or_404(asset_id, current_user.tenant_id)

    result = await db.execute(
        select(AssetMaintenance).where(
            AssetMaintenance.id == m_id,
            AssetMaintenance.asset_id == asset_id,
        )
    )
    record = result.scalar_one_or_none()
    if record is None:
        raise ResourceNotFoundError("asset_maintenance", str(m_id))

    updates = body.model_dump(exclude_unset=True)

    # Detect completion transition before applying any writes
    completing = (
        "status" in updates
        and updates["status"] == MaintenanceStatus.completed
        and record.status != MaintenanceStatus.completed
    )

    if completing and asset.status == AssetStatus.in_maintenance:
        # Validate that in_maintenance → in_use is a permitted transition
        valid = await repo.get_valid_transitions(asset.status)
        if AssetStatus.in_use.value not in valid:
            raise InvalidStateTransitionError(
                from_status=asset.status.value,
                to_status=AssetStatus.in_use.value,
                valid_transitions=valid,
            )

    # Apply maintenance record updates
    for field_name, value in updates.items():
        setattr(record, field_name, value)

    # Revert asset status after a successful completion
    if completing and asset.status == AssetStatus.in_maintenance:
        old_status = asset.status
        asset.status = AssetStatus.in_use
        await repo.update(asset)
        await repo.record_history(
            asset_id=asset_id,
            actor_id=current_user.local_user_id,
            action=AssetHistoryAction.status_changed,
            field="status",
            old_val=old_status.value,
            new_val=AssetStatus.in_use.value,
            notes=f"Status reverted to in_use on maintenance completion (m_id={m_id})",
        )

    await db.commit()
    await db.refresh(record)
    return MaintenanceResponse.model_validate(record)


# ============================================================================
# /assets/{asset_id}/history
# ============================================================================


@router.get("/{asset_id}/history", response_model=list[AssetHistoryResponse])
async def get_asset_history(
    asset_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[AssetHistoryResponse]:
    """Return the audit history for an asset (most recent first)."""
    _require_tenant(current_user)

    repo = AssetRepository(db)
    await repo.get_or_404(asset_id, current_user.tenant_id)

    result = await db.execute(
        select(AssetHistory)
        .where(AssetHistory.asset_id == asset_id)
        .order_by(AssetHistory.changed_at.desc())
        .limit(limit)
    )
    rows = list(result.scalars().all())
    return [AssetHistoryResponse.model_validate(r) for r in rows]


# ============================================================================
# /assets/{asset_id}/attachments
# ============================================================================


@router.post(
    "/{asset_id}/attachments",
    response_model=AttachmentPresignResponse,
    status_code=201,
)
async def create_asset_attachment(
    asset_id: UUID,
    body: CreateAssetAttachmentRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AttachmentPresignResponse:
    """Generate a presigned upload URL for an asset attachment."""
    _require_tenant(current_user)
    _require_any_role(current_user, _AGENT_ROLES, "upload attachments")

    repo = AssetRepository(db)
    await repo.get_or_404(asset_id, current_user.tenant_id)

    from uuid_extensions import uuid7

    storage = get_storage_service()
    upload_url, storage_key = await storage.presigned_upload_url(
        tenant_id=str(current_user.tenant_id),
        filename=body.filename,
        mime_type=body.mime_type,
        max_bytes=body.file_size,
    )

    attachment_id = uuid7()
    att = AssetAttachment(
        id=attachment_id,
        asset_id=asset_id,
        uploaded_by=current_user.local_user_id,
        filename=body.filename,
        storage_url=storage_key,
        file_size=body.file_size,
        mime_type=body.mime_type,
        label=body.label,
    )
    db.add(att)
    await db.commit()

    return AttachmentPresignResponse(
        upload_url=upload_url,
        attachment_id=attachment_id,
        expires_in=3600,
    )


@router.get("/{asset_id}/attachments", response_model=list[AssetAttachmentResponse])
async def list_asset_attachments(
    asset_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[AssetAttachmentResponse]:
    """List all attachments for an asset."""
    _require_tenant(current_user)

    repo = AssetRepository(db)
    await repo.get_or_404(asset_id, current_user.tenant_id)

    result = await db.execute(
        select(AssetAttachment)
        .where(AssetAttachment.asset_id == asset_id)
        .order_by(AssetAttachment.created_at.desc())
    )
    rows = list(result.scalars().all())
    return [AssetAttachmentResponse.model_validate(r) for r in rows]


@router.delete("/{asset_id}/attachments/{att_id}", status_code=204)
async def delete_asset_attachment(
    asset_id: UUID,
    att_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete an asset attachment. Requires agent or above."""
    _require_tenant(current_user)
    _require_any_role(current_user, _AGENT_ROLES, "delete attachments")

    repo = AssetRepository(db)
    await repo.get_or_404(asset_id, current_user.tenant_id)

    result = await db.execute(
        select(AssetAttachment).where(
            AssetAttachment.id == att_id,
            AssetAttachment.asset_id == asset_id,
        )
    )
    att = result.scalar_one_or_none()
    if att is None:
        raise ResourceNotFoundError("asset_attachments", str(att_id))

    await db.delete(att)
    await db.commit()


# ============================================================================
# /assets/{asset_id}/relationships  (CMDB)
# ============================================================================


@router.post(
    "/{asset_id}/relationships",
    response_model=AssetRelationshipResponse,
    status_code=201,
)
async def create_relationship(
    asset_id: UUID,
    body: CreateRelationshipRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AssetRelationshipResponse:
    """Create a directed CMDB relationship between two assets.

    Both assets must belong to the same tenant (IDOR guard on target).
    History is recorded on both the source and target asset.
    """
    _require_tenant(current_user)
    _require_any_role(current_user, _TEAM_LEAD_ROLES, "manage asset relationships")

    repo = AssetRepository(db)
    await repo.get_or_404(asset_id, current_user.tenant_id)
    # IDOR check: target must also belong to the same tenant
    await repo.get_or_404(body.target_asset_id, current_user.tenant_id)

    rel = AssetRelationship(
        tenant_id=current_user.tenant_id,
        source_asset_id=asset_id,
        target_asset_id=body.target_asset_id,
        relationship_type=body.relationship_type,
        description=body.description,
        created_by=current_user.local_user_id,
    )
    db.add(rel)
    await db.flush()

    # Record history on source asset
    await repo.record_history(
        asset_id=asset_id,
        actor_id=current_user.local_user_id,
        action=AssetHistoryAction.relationship_added,
        notes=(
            f"Relationship '{body.relationship_type.value}' added "
            f"→ target={body.target_asset_id}"
        ),
    )
    # Record history on target asset
    await repo.record_history(
        asset_id=body.target_asset_id,
        actor_id=current_user.local_user_id,
        action=AssetHistoryAction.relationship_added,
        notes=(
            f"Relationship '{body.relationship_type.value}' added "
            f"← source={asset_id}"
        ),
    )

    await db.commit()
    await db.refresh(rel)
    return AssetRelationshipResponse.model_validate(rel)


@router.get(
    "/{asset_id}/relationships",
    response_model=list[AssetRelationshipResponse],
)
async def list_relationships(
    asset_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[AssetRelationshipResponse]:
    """List all CMDB relationships for an asset (both directions)."""
    _require_tenant(current_user)

    repo = AssetRepository(db)
    await repo.get_or_404(asset_id, current_user.tenant_id)

    result = await db.execute(
        select(AssetRelationship).where(
            sa.or_(
                AssetRelationship.source_asset_id == asset_id,
                AssetRelationship.target_asset_id == asset_id,
            ),
            AssetRelationship.tenant_id == current_user.tenant_id,
        )
    )
    rows = list(result.scalars().all())
    return [AssetRelationshipResponse.model_validate(r) for r in rows]


@router.delete("/{asset_id}/relationships/{rel_id}", status_code=204)
async def delete_relationship(
    asset_id: UUID,
    rel_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Remove a CMDB relationship.

    The relationship may be found when asset_id is either the source or
    the target.  History is recorded on both assets before deletion.
    """
    _require_tenant(current_user)
    _require_any_role(current_user, _TEAM_LEAD_ROLES, "manage asset relationships")

    repo = AssetRepository(db)
    await repo.get_or_404(asset_id, current_user.tenant_id)

    result = await db.execute(
        select(AssetRelationship).where(
            AssetRelationship.id == rel_id,
            sa.or_(
                AssetRelationship.source_asset_id == asset_id,
                AssetRelationship.target_asset_id == asset_id,
            ),
            AssetRelationship.tenant_id == current_user.tenant_id,
        )
    )
    rel = result.scalar_one_or_none()
    if rel is None:
        raise ResourceNotFoundError("asset_relationships", str(rel_id))

    source_id = rel.source_asset_id
    target_id = rel.target_asset_id

    # Record history on both assets before the delete
    await repo.record_history(
        asset_id=source_id,
        actor_id=current_user.local_user_id,
        action=AssetHistoryAction.relationship_removed,
        notes=f"Relationship '{rel.relationship_type.value}' removed → target={target_id}",
    )
    await repo.record_history(
        asset_id=target_id,
        actor_id=current_user.local_user_id,
        action=AssetHistoryAction.relationship_removed,
        notes=f"Relationship '{rel.relationship_type.value}' removed ← source={source_id}",
    )

    await db.delete(rel)
    await db.commit()


# ============================================================================
# /assets/{asset_id}/impact-analysis  — CMDB Impact Analysis
# ============================================================================


@router.get("/{asset_id}/impact-analysis", response_model=ImpactAnalysisResponse)
async def get_impact_analysis(
    asset_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ImpactAnalysisResponse:
    """Traverse the CMDB relationship graph up to 3 hops from the given asset.

    Returns all potentially affected assets and any open tickets linked to
    those assets.  The source asset is never included in ``affected_assets``.
    Only tickets with status NOT IN (closed, cancelled, resolved) are included.

    Uses a recursive CTE — all parameters are bound, never interpolated.
    """
    _require_tenant(current_user)

    repo = AssetRepository(db)
    await repo.get_or_404(asset_id, current_user.tenant_id)

    affected_asset_ids, affected_ticket_ids = await repo.get_impact_analysis(
        asset_id=asset_id,
        tenant_id=current_user.tenant_id,
    )

    # Load Asset ORM objects for all affected asset IDs
    affected_assets: list[AssetResponse] = []
    if affected_asset_ids:
        from app.models.asset import Asset as AssetModel

        result = await db.execute(
            select(AssetModel).where(
                AssetModel.id.in_(affected_asset_ids),
                AssetModel.tenant_id == current_user.tenant_id,
                AssetModel.deleted_at.is_(None),
            )
        )
        affected_assets = [
            AssetResponse.model_validate(a) for a in result.scalars().all()
        ]

    # Load Ticket ORM objects for all affected ticket IDs
    affected_tickets: list[TicketImpactResponse] = []
    if affected_ticket_ids:
        from app.models.ticket import Ticket as TicketModel

        ticket_result = await db.execute(
            select(TicketModel).where(
                TicketModel.id.in_(affected_ticket_ids),
                TicketModel.tenant_id == current_user.tenant_id,
            )
        )
        affected_tickets = [
            TicketImpactResponse(
                id=t.id,
                ticket_number=t.ticket_number,
                title=t.title,
                status=t.status.value,
                priority=t.priority.value,
            )
            for t in ticket_result.scalars().all()
        ]

    return ImpactAnalysisResponse(
        asset_id=asset_id,
        affected_assets=affected_assets,
        affected_tickets=affected_tickets,
        total_affected_assets=len(affected_assets),
        total_affected_tickets=len(affected_tickets),
    )


# ============================================================================
# /assets/{asset_id}/tickets  — Asset-Ticket Links
# ============================================================================


@router.post("/{asset_id}/tickets", status_code=201, response_model=dict)
async def link_ticket_to_asset(
    asset_id: UUID,
    body: LinkTicketRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Link a ticket to an asset.  Idempotent — returns 201 even if already linked."""
    _require_tenant(current_user)
    _require_any_role(current_user, _AGENT_ROLES, "link tickets to assets")

    from app.models.asset import AssetTicketLink
    from app.models.ticket import Ticket as TicketModel

    repo = AssetRepository(db)
    await repo.get_or_404(asset_id, current_user.tenant_id)

    # Verify ticket belongs to the same tenant (IDOR guard)
    ticket_result = await db.execute(
        select(TicketModel).where(
            TicketModel.id == body.ticket_id,
            TicketModel.tenant_id == current_user.tenant_id,
        )
    )
    if ticket_result.scalar_one_or_none() is None:
        raise ResourceNotFoundError("tickets", str(body.ticket_id))

    # Idempotent: skip insert if link already exists
    existing = await db.execute(
        select(AssetTicketLink).where(
            AssetTicketLink.asset_id == asset_id,
            AssetTicketLink.ticket_id == body.ticket_id,
        )
    )
    if existing.scalar_one_or_none() is None:
        link = AssetTicketLink(
            asset_id=asset_id,
            ticket_id=body.ticket_id,
            linked_by=current_user.local_user_id,
        )
        db.add(link)
        await db.commit()

    return {"asset_id": str(asset_id), "ticket_id": str(body.ticket_id)}


@router.delete("/{asset_id}/tickets/{ticket_id}", status_code=204)
async def unlink_ticket_from_asset(
    asset_id: UUID,
    ticket_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Remove the link between an asset and a ticket."""
    _require_tenant(current_user)
    _require_any_role(current_user, _AGENT_ROLES, "unlink tickets from assets")

    from app.models.asset import AssetTicketLink

    repo = AssetRepository(db)
    await repo.get_or_404(asset_id, current_user.tenant_id)

    result = await db.execute(
        select(AssetTicketLink).where(
            AssetTicketLink.asset_id == asset_id,
            AssetTicketLink.ticket_id == ticket_id,
        )
    )
    link = result.scalar_one_or_none()
    if link is None:
        raise ResourceNotFoundError(
            "asset_ticket_links", f"{asset_id}/{ticket_id}"
        )

    await db.delete(link)
    await db.commit()


@router.get("/{asset_id}/tickets", response_model=list[LinkedTicketResponse])
async def list_tickets_for_asset(
    asset_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[LinkedTicketResponse]:
    """List all tickets linked to an asset."""
    _require_tenant(current_user)

    from app.models.asset import AssetTicketLink
    from app.models.ticket import Ticket as TicketModel

    repo = AssetRepository(db)
    await repo.get_or_404(asset_id, current_user.tenant_id)

    result = await db.execute(
        select(TicketModel)
        .join(AssetTicketLink, AssetTicketLink.ticket_id == TicketModel.id)
        .where(
            AssetTicketLink.asset_id == asset_id,
            TicketModel.tenant_id == current_user.tenant_id,
        )
        .order_by(TicketModel.created_at.desc())
    )
    tickets = list(result.scalars().all())
    return [
        LinkedTicketResponse(
            id=t.id,
            ticket_number=t.ticket_number,
            title=t.title,
            status=t.status.value,
            priority=t.priority.value,
        )
        for t in tickets
    ]


# ============================================================================
# /assets/{asset_id}/ai-maintenance-prediction  — Predictive Maintenance AI
# ============================================================================


@router.get(
    "/{asset_id}/ai-maintenance-prediction",
    response_model=AIPredictiveMaintenanceResponse,
)
async def get_ai_maintenance_prediction(
    asset_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AIPredictiveMaintenanceResponse:
    """Return the latest AI maintenance prediction for an asset.

    Returns 404 if the asset has never been processed by the nightly
    prediction task.  Agents and above may view predictions for any asset;
    end users may only view predictions for assets assigned to them.
    """
    from sqlalchemy import select

    from app.models.ai_asset import AIPredictiveMaintenance

    _require_tenant(current_user)

    repo = AssetRepository(db)
    asset = await repo.get_or_404(asset_id, current_user.tenant_id)

    # End users may only see predictions for their own assigned assets
    if not _has_role(current_user, _AGENT_ROLES):
        if asset.assigned_to != current_user.local_user_id:
            raise ResourceNotFoundError("assets", str(asset_id))

    result = await db.execute(
        select(AIPredictiveMaintenance).where(
            AIPredictiveMaintenance.asset_id == asset_id
        )
    )
    prediction = result.scalar_one_or_none()
    if prediction is None:
        raise ResourceNotFoundError("ai_maintenance_predictions", str(asset_id))

    return AIPredictiveMaintenanceResponse.model_validate(prediction)


@router.post(
    "/{asset_id}/ai-maintenance-prediction/acknowledge",
    response_model=AIPredictiveMaintenanceResponse,
)
async def acknowledge_ai_maintenance_prediction(
    asset_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AIPredictiveMaintenanceResponse:
    """Acknowledge the AI maintenance prediction for an asset.

    Sets ``acknowledged = True`` and records the acknowledging user.
    Requires agent or above.  Returns 404 if no prediction exists yet.
    """
    from sqlalchemy import select

    from app.models.ai_asset import AIPredictiveMaintenance

    _require_tenant(current_user)
    _require_any_role(current_user, _AGENT_ROLES, "acknowledge maintenance predictions")

    repo = AssetRepository(db)
    await repo.get_or_404(asset_id, current_user.tenant_id)

    result = await db.execute(
        select(AIPredictiveMaintenance).where(
            AIPredictiveMaintenance.asset_id == asset_id
        )
    )
    prediction = result.scalar_one_or_none()
    if prediction is None:
        raise ResourceNotFoundError("ai_maintenance_predictions", str(asset_id))

    prediction.acknowledged = True
    prediction.acknowledged_by = current_user.local_user_id
    await db.commit()
    await db.refresh(prediction)

    return AIPredictiveMaintenanceResponse.model_validate(prediction)


# ============================================================================
# /asset-types  — Asset Type CRUD (admin only for create/patch)
# ============================================================================


@asset_types_router.get("", response_model=list[AssetTypeResponse])
async def list_asset_types(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    category_id: UUID | None = Query(default=None),
) -> list[AssetTypeResponse]:
    """List asset types visible to the caller (global + own tenant)."""
    q = select(AssetType)
    scope = ref_visible_filter(AssetType, current_user)
    if scope is not None:
        q = q.where(scope)
    if category_id is not None:
        q = q.where(AssetType.category_id == category_id)
    q = q.order_by(AssetType.name)

    result = await db.execute(q)
    types = list(result.scalars().all())
    return [AssetTypeResponse.model_validate(t) for t in types]


@asset_types_router.post("", response_model=AssetTypeResponse, status_code=201)
async def create_asset_type(
    body: CreateAssetTypeRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AssetTypeResponse:
    """Create an asset type (global if platform/all-tenants, else tenant-scoped)."""
    target_tenant_id = ref_create_tenant_id(current_user)
    require_ref_write(current_user, target_tenant_id)

    at = AssetType(
        tenant_id=target_tenant_id,
        category_id=body.category_id,
        name=body.name,
        custom_fields_schema=body.custom_fields_schema,
    )
    db.add(at)
    await db.commit()
    await db.refresh(at)
    return AssetTypeResponse.model_validate(at)


@asset_types_router.get("/{type_id}", response_model=AssetTypeResponse)
async def get_asset_type(
    type_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AssetTypeResponse:
    at = await _get_visible_asset_type(db, type_id, current_user)
    return AssetTypeResponse.model_validate(at)


@asset_types_router.patch("/{type_id}", response_model=AssetTypeResponse)
async def update_asset_type(
    type_id: UUID,
    body: UpdateAssetTypeRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AssetTypeResponse:
    """Update an asset type (platform for global rows; manager/admin for own tenant)."""
    at = await _get_visible_asset_type(db, type_id, current_user)
    require_ref_write(current_user, at.tenant_id)

    for field_name, value in body.model_dump(exclude_unset=True).items():
        setattr(at, field_name, value)

    await db.commit()
    await db.refresh(at)
    return AssetTypeResponse.model_validate(at)


async def _get_visible_asset_type(
    db: AsyncSession, type_id: UUID, current_user: CurrentUser
) -> AssetType:
    q = select(AssetType).where(AssetType.id == type_id)
    scope = ref_visible_filter(AssetType, current_user)
    if scope is not None:
        q = q.where(scope)
    at = (await db.execute(q)).scalar_one_or_none()
    if at is None:
        raise ResourceNotFoundError("asset_types", str(type_id))
    return at


# ============================================================================
# /vendors  — Vendor CRUD (manager/admin for create/patch)
# ============================================================================


@vendors_router.get("", response_model=list[VendorResponse])
async def list_vendors(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    active_only: bool = Query(default=True),
) -> list[VendorResponse]:
    """List vendors visible to the caller (global + own tenant)."""
    q = select(Vendor)
    scope = ref_visible_filter(Vendor, current_user)
    if scope is not None:
        q = q.where(scope)
    if active_only:
        q = q.where(Vendor.is_active.is_(True))
    q = q.order_by(Vendor.name)

    result = await db.execute(q)
    vendors = list(result.scalars().all())
    return [VendorResponse.model_validate(v) for v in vendors]


@vendors_router.post("", response_model=VendorResponse, status_code=201)
async def create_vendor(
    body: CreateVendorRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> VendorResponse:
    """Create a vendor (global if platform/all-tenants, else tenant-scoped)."""
    target_tenant_id = ref_create_tenant_id(current_user)
    require_ref_write(current_user, target_tenant_id)

    vendor = Vendor(
        tenant_id=target_tenant_id,
        name=body.name,
        contact_email=body.contact_email,
        contact_phone=body.contact_phone,
        website=body.website,
        address=body.address,
    )
    db.add(vendor)
    await db.commit()
    await db.refresh(vendor)
    return VendorResponse.model_validate(vendor)


@vendors_router.get("/{vendor_id}", response_model=VendorResponse)
async def get_vendor(
    vendor_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> VendorResponse:
    vendor = await _get_visible_vendor(db, vendor_id, current_user)
    return VendorResponse.model_validate(vendor)


@vendors_router.patch("/{vendor_id}", response_model=VendorResponse)
async def update_vendor(
    vendor_id: UUID,
    body: UpdateVendorRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> VendorResponse:
    """Update a vendor (platform for global rows; manager/admin for own tenant)."""
    vendor = await _get_visible_vendor(db, vendor_id, current_user)
    require_ref_write(current_user, vendor.tenant_id)

    for field_name, value in body.model_dump(exclude_unset=True).items():
        setattr(vendor, field_name, value)

    await db.commit()
    await db.refresh(vendor)
    return VendorResponse.model_validate(vendor)


async def _get_visible_vendor(
    db: AsyncSession, vendor_id: UUID, current_user: CurrentUser
) -> Vendor:
    q = select(Vendor).where(Vendor.id == vendor_id)
    scope = ref_visible_filter(Vendor, current_user)
    if scope is not None:
        q = q.where(scope)
    vendor = (await db.execute(q)).scalar_one_or_none()
    if vendor is None:
        raise ResourceNotFoundError("vendors", str(vendor_id))
    return vendor


# ============================================================================
# /asset-categories  — Asset Category CRUD (manager/admin for create/patch)
# ============================================================================


@asset_categories_router.get("", response_model=list[AssetCategoryResponse])
async def list_asset_categories(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    parent_id: UUID | None = Query(default=None),
    active_only: bool = Query(default=False),
) -> list[AssetCategoryResponse]:
    """List asset categories visible to the caller (global + own tenant)."""
    q = select(AssetCategory)
    scope = ref_visible_filter(AssetCategory, current_user)
    if scope is not None:
        q = q.where(scope)
    if parent_id is not None:
        q = q.where(AssetCategory.parent_id == parent_id)
    if active_only:
        q = q.where(AssetCategory.is_active.is_(True))
    q = q.order_by(AssetCategory.name)

    result = await db.execute(q)
    categories = list(result.scalars().all())
    return [AssetCategoryResponse.model_validate(c) for c in categories]


@asset_categories_router.post("", response_model=AssetCategoryResponse, status_code=201)
async def create_asset_category(
    body: CreateAssetCategoryRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AssetCategoryResponse:
    """Create an asset category (global if platform/all-tenants, else tenant-scoped)."""
    target_tenant_id = ref_create_tenant_id(current_user)
    require_ref_write(current_user, target_tenant_id)

    if body.parent_id is not None:
        await _get_visible_category(db, body.parent_id, current_user)

    category = AssetCategory(
        tenant_id=target_tenant_id,
        name=body.name,
        description=body.description,
        parent_id=body.parent_id,
        icon=body.icon,
    )
    db.add(category)
    await db.commit()
    await db.refresh(category)
    return AssetCategoryResponse.model_validate(category)


@asset_categories_router.get("/{category_id}", response_model=AssetCategoryResponse)
async def get_asset_category(
    category_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AssetCategoryResponse:
    category = await _get_visible_category(db, category_id, current_user)
    return AssetCategoryResponse.model_validate(category)


@asset_categories_router.patch("/{category_id}", response_model=AssetCategoryResponse)
async def update_asset_category(
    category_id: UUID,
    body: UpdateAssetCategoryRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AssetCategoryResponse:
    """Update an asset category (platform for global rows; manager/admin for own tenant)."""
    category = await _get_visible_category(db, category_id, current_user)
    require_ref_write(current_user, category.tenant_id)

    updates = body.model_dump(exclude_unset=True)
    new_parent = updates.get("parent_id")
    if new_parent is not None:
        if new_parent == category_id:
            raise AuthorizationError("A category cannot be its own parent")
        await _get_visible_category(db, new_parent, current_user)

    for field_name, value in updates.items():
        setattr(category, field_name, value)

    await db.commit()
    await db.refresh(category)
    return AssetCategoryResponse.model_validate(category)


async def _get_visible_category(
    db: AsyncSession, category_id: UUID, current_user: CurrentUser
) -> AssetCategory:
    q = select(AssetCategory).where(AssetCategory.id == category_id)
    scope = ref_visible_filter(AssetCategory, current_user)
    if scope is not None:
        q = q.where(scope)
    category = (await db.execute(q)).scalar_one_or_none()
    if category is None:
        raise ResourceNotFoundError("asset_categories", str(category_id))
    return category
