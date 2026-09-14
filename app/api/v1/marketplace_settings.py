"""
Tenant-level marketplace-integration config — the admin config page backend
(§0a of the native-connector plan: "one admin config page, tenant-scoped").
Backs a settings blob (enabled marketplaces, per-event-type auto/manual
mapping per plan §3) rather than individual columns, matching
MarketplaceIntegrationSettings' JSONB shape.

No frontend page built yet — this is the API it would call.
"""

import logging
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentUser, require_role
from app.database import get_db
from app.models.marketplace import MarketplaceIntegrationSettings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/marketplaces/settings", tags=["marketplaces"])
_ADMIN_ROLES = ("admin", "tenant_admin")

# Conservative defaults matching plan §3's event-mapping table — a tenant
# with no settings row yet gets these rather than an empty/undefined config.
_DEFAULT_SETTINGS = {
    "enabled_marketplaces": [],
    "event_mapping": {
        "return_requested": "auto_create_ticket",
        "replacement_requested": "auto_create_ticket",
        "order_created": "silent",
        "order_updated": "silent",
    },
}


class SettingsUpdateRequest(BaseModel):
    settings: dict


@router.get("")
async def get_marketplace_settings(
    current_user: CurrentUser = Depends(require_role(*_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = (
        await db.execute(
            select(MarketplaceIntegrationSettings).where(
                MarketplaceIntegrationSettings.tenant_id == current_user.tenant_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return {"settings": _DEFAULT_SETTINGS, "is_default": True}
    return {"settings": row.settings, "is_default": False, "updated_at": row.updated_at.isoformat()}


@router.put("")
async def update_marketplace_settings(
    body: SettingsUpdateRequest,
    current_user: CurrentUser = Depends(require_role(*_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Full-replace semantics, not a merge — the caller (admin UI, once
    built) is expected to send the complete settings object back, same
    shape GET returns. Simpler and less surprising than a partial-merge
    PATCH for a single JSONB blob with no per-key schema to validate against."""
    row = (
        await db.execute(
            select(MarketplaceIntegrationSettings).where(
                MarketplaceIntegrationSettings.tenant_id == current_user.tenant_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = MarketplaceIntegrationSettings(
            tenant_id=current_user.tenant_id,
            settings=body.settings,
            updated_by=current_user.local_user_id,
        )
        db.add(row)
    else:
        row.settings = body.settings
        row.updated_by = current_user.local_user_id
    await db.commit()
    return {"success": True, "settings": row.settings}


__all__ = ["router"]
