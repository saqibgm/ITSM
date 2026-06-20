"""Business logic for the Asset Management domain.

State machine:
- Status transitions are validated against asset_status_transitions table.
- InvalidStateTransitionError (HTTP 409) is raised BEFORE any DB write.

Custom fields:
- Validated against the AssetType's custom_fields_schema using jsonschema.
- If the schema is empty ({}) validation is skipped.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import jsonschema
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import InvalidStateTransitionError, ValidationError
from app.models.asset import Asset, AssetCondition, AssetHistoryAction, AssetStatus
from app.repositories.asset_repo import AssetRepository


# ---------------------------------------------------------------------------
# Data transfer objects (used by the API layer to pass validated input)
# ---------------------------------------------------------------------------


@dataclass
class CreateAssetData:
    name: str
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
    custom_fields: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class AssetService:
    """Coordinates asset lifecycle operations across repository and domain rules."""

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    async def create_asset(
        self,
        tenant_id: UUID,
        actor_id: UUID,
        data: CreateAssetData,
        db: AsyncSession,
    ) -> Asset:
        """Create a new asset.

        Steps:
        1. Resolve and validate the AssetType (and its custom_fields_schema).
        2. Validate ``data.custom_fields`` against the schema (jsonschema).
        3. Generate the next asset_tag via the sequence table (SELECT FOR UPDATE).
        4. INSERT the asset row.
        5. INSERT an asset_history row with action=created.
        """
        from sqlalchemy import select
        from app.models.asset import AssetType
        from app.exceptions import ResourceNotFoundError

        repo = AssetRepository(db)

        # 1. Resolve AssetType (tenant-scoped)
        result = await db.execute(
            select(AssetType).where(
                AssetType.id == data.type_id,
                AssetType.tenant_id == tenant_id,
            )
        )
        asset_type = result.scalar_one_or_none()
        if asset_type is None:
            raise ResourceNotFoundError("asset_types", str(data.type_id))

        # 2. Validate custom_fields
        schema = asset_type.custom_fields_schema or {}
        if schema:
            self._validate_custom_fields(data.custom_fields, schema)

        # 3. Generate asset tag
        asset_tag = await repo.next_asset_tag(tenant_id)

        # 4. Create asset
        asset = Asset(
            asset_tag=asset_tag,
            tenant_id=tenant_id,
            type_id=data.type_id,
            name=data.name,
            description=data.description,
            condition=data.condition,
            serial_number=data.serial_number,
            model_number=data.model_number,
            manufacturer=data.manufacturer,
            vendor_id=data.vendor_id,
            purchase_date=data.purchase_date,
            purchase_cost=data.purchase_cost,
            warranty_expiry_date=data.warranty_expiry_date,
            department_id=data.department_id,
            product_id=data.product_id,
            location=data.location,
            custom_fields=data.custom_fields or {},
            metadata_={},
            status=AssetStatus.in_stock,
        )
        asset = await repo.create(asset)

        # 5. Record history
        await repo.record_history(
            asset_id=asset.id,
            actor_id=actor_id,
            action=AssetHistoryAction.created,
            notes=f"Asset created with tag {asset_tag}",
        )

        # Dispatch outbound webhook event — never let errors propagate
        try:
            from app.workers.tasks_webhooks import dispatch_webhook_event
            dispatch_webhook_event.delay(
                str(asset.tenant_id),
                "asset.created",
                {
                    "id": str(asset.id),
                    "asset_tag": asset.asset_tag,
                    "name": asset.name,
                    "status": asset.status.value if asset.status else None,
                },
            )
        except Exception:
            pass

        return asset

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    async def update_asset(
        self,
        asset: Asset,
        updates: dict,
        actor_id: UUID,
        db: AsyncSession,
    ) -> Asset:
        """Apply a partial update to an asset, enforcing the state machine for status.

        For each changed field a history row is recorded.
        If ``status`` is in updates, the transition is validated before any write.
        """
        repo = AssetRepository(db)

        # Validate status transition before touching the asset
        if "status" in updates:
            new_status_raw = updates["status"]
            new_status = (
                AssetStatus(new_status_raw)
                if not isinstance(new_status_raw, AssetStatus)
                else new_status_raw
            )
            valid = await repo.get_valid_transitions(asset.status)
            if new_status.value not in valid:
                raise InvalidStateTransitionError(
                    from_status=asset.status.value,
                    to_status=new_status.value,
                    valid_transitions=valid,
                )

        # Validate custom_fields if included
        if "custom_fields" in updates and updates["custom_fields"] is not None:
            await self._validate_custom_fields_for_asset(asset, updates["custom_fields"], db)

        # Apply changes and record history per field
        mutable_fields = {
            "name", "description", "status", "condition", "location",
            "department_id", "vendor_id", "warranty_expiry_date", "custom_fields",
            "serial_number", "model_number", "manufacturer",
        }
        for field_name, new_value in updates.items():
            if field_name not in mutable_fields:
                continue
            old_value = getattr(asset, field_name, None)
            if old_value == new_value:
                continue

            setattr(asset, field_name, new_value)

            action = (
                AssetHistoryAction.status_changed
                if field_name == "status"
                else AssetHistoryAction.updated
            )
            await repo.record_history(
                asset_id=asset.id,
                actor_id=actor_id,
                action=action,
                field=field_name,
                old_val=old_value.value if hasattr(old_value, "value") else old_value,
                new_val=new_value.value if hasattr(new_value, "value") else new_value,
            )

        asset = await repo.update(asset)

        # Fire automation rules for status change — never let errors propagate
        if "status" in updates:
            try:
                from app.workers.tasks_automation import run_asset_automation
                run_asset_automation.delay(str(asset.id), "asset_status_changed")
            except Exception:
                pass

        # Dispatch outbound webhook event — never let errors propagate
        try:
            from app.workers.tasks_webhooks import dispatch_webhook_event
            dispatch_webhook_event.delay(
                str(asset.tenant_id),
                "asset.updated",
                {
                    "id": str(asset.id),
                    "asset_tag": asset.asset_tag,
                    "name": asset.name,
                    "status": asset.status.value if asset.status else None,
                    "changed_fields": list(updates.keys()),
                },
            )
        except Exception:
            pass

        return asset

    # ------------------------------------------------------------------
    # Assign / Unassign
    # ------------------------------------------------------------------

    async def assign_asset(
        self,
        asset: Asset,
        user_id: UUID | None,
        actor_id: UUID,
        db: AsyncSession,
    ) -> Asset:
        """Assign an asset to a user (or clear the assignment if user_id is None).

        Records history with action=assigned or action=unassigned.
        """
        repo = AssetRepository(db)

        old_assigned_to = asset.assigned_to

        if user_id is not None:
            asset.assigned_to = user_id
            asset.assigned_at = datetime.utcnow()
            action = AssetHistoryAction.assigned
            notes = f"Assigned to user {user_id}"
        else:
            asset.assigned_to = None
            asset.assigned_at = None
            action = AssetHistoryAction.unassigned
            notes = f"Unassigned from user {old_assigned_to}"

        asset = await repo.update(asset)

        await repo.record_history(
            asset_id=asset.id,
            actor_id=actor_id,
            action=action,
            field="assigned_to",
            old_val=str(old_assigned_to) if old_assigned_to else None,
            new_val=str(user_id) if user_id else None,
            notes=notes,
        )

        return asset

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_custom_fields(custom_fields: dict, schema: dict) -> None:
        """Raise ValidationError if custom_fields does not conform to the JSON Schema."""
        try:
            jsonschema.validate(instance=custom_fields, schema=schema)
        except jsonschema.ValidationError as exc:
            raise ValidationError(
                f"custom_fields validation failed: {exc.message}"
            ) from exc
        except jsonschema.SchemaError as exc:
            raise ValidationError(
                f"asset_type custom_fields_schema is invalid: {exc.message}"
            ) from exc

    async def _validate_custom_fields_for_asset(
        self, asset: Asset, custom_fields: dict, db: AsyncSession
    ) -> None:
        """Look up the asset's type schema and validate custom_fields against it."""
        from sqlalchemy import select
        from app.models.asset import AssetType

        result = await db.execute(select(AssetType).where(AssetType.id == asset.type_id))
        asset_type = result.scalar_one_or_none()
        if asset_type and asset_type.custom_fields_schema:
            self._validate_custom_fields(custom_fields, asset_type.custom_fields_schema)
