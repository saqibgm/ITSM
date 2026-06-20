"""
GDPR Data Export & Right-to-Erasure endpoints (S6D).

Routes
------
GET  /gdpr/export                  — self-service data export (any tenant user)
POST /gdpr/erasure-request         — self-service PII erasure (any tenant user)
GET  /gdpr/erasure-history         — list past erasure events (tenant_admin)
POST /gdpr/admin/erase-user/{user_id} — admin-initiated erasure (tenant_admin)

Tenant context always comes from the JWT — cross-tenant operations are not
possible from this API.
"""

import json
import logging
from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentUser, get_current_user, require_role
from app.database import get_db
from app.exceptions import AuthorizationError, ResourceNotFoundError, ValidationError
from app.services.gdpr_service import GDPRService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/gdpr", tags=["gdpr"])

_gdpr_service = GDPRService()


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class ErasureRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: bool = Field(..., description="Must be true to proceed with erasure")


class AdminErasureRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(..., min_length=1, description="Business reason for erasure")


# ---------------------------------------------------------------------------
# Helper: assert the authenticated user belongs to a tenant
# ---------------------------------------------------------------------------


def _assert_tenant_user(current_user: CurrentUser) -> tuple[str, str]:
    """Return (user_id_str, tenant_id_str) or raise AuthorizationError."""
    if current_user.tier != "tenant" or current_user.local_user_id is None or current_user.tenant_id is None:
        raise AuthorizationError("This endpoint is only available to tenant users")
    return str(current_user.local_user_id), str(current_user.tenant_id)


# ---------------------------------------------------------------------------
# GET /gdpr/export
# ---------------------------------------------------------------------------


@router.get(
    "/export",
    summary="Export all personal data for the authenticated user",
    response_class=Response,
)
async def export_my_data(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return a JSON attachment containing every record linked to the caller
    within their tenant.  No body required — identity comes from the JWT.
    """
    user_id, tenant_id = _assert_tenant_user(current_user)

    data = await _gdpr_service.export_user_data(db, user_id, tenant_id)
    json_bytes = json.dumps(data, default=str, indent=2).encode()

    filename = f"itsm-export-{date.today()}.json"
    return Response(
        content=json_bytes,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# POST /gdpr/erasure-request
# ---------------------------------------------------------------------------


@router.post(
    "/erasure-request",
    summary="Request erasure of the authenticated user's personal data",
)
async def request_self_erasure(
    body: ErasureRequestBody,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Anonymise the caller's PII within their tenant.

    ``{"confirm": true}`` is required; returns 422 if ``confirm`` is false or
    absent.
    """
    if not body.confirm:
        raise ValidationError(
            "confirm must be true to proceed with erasure"
        )

    user_id, tenant_id = _assert_tenant_user(current_user)

    result = await _gdpr_service.erase_user_data(
        db,
        user_id=user_id,
        tenant_id=tenant_id,
        requested_by_id=user_id,
    )
    await db.commit()

    logger.info(
        "gdpr_self_erasure_requested",
        extra={"user_id": user_id, "tenant_id": tenant_id},
    )
    return result


# ---------------------------------------------------------------------------
# GET /gdpr/erasure-history
# ---------------------------------------------------------------------------


@router.get(
    "/erasure-history",
    summary="List past GDPR erasure requests for this tenant (tenant_admin only)",
    dependencies=[Depends(require_role("admin"))],
)
async def list_erasure_history(
    page: int = Query(default=1, ge=1, description="Page number (1-based)"),
    page_size: int = Query(default=50, ge=1, le=200, description="Items per page"),
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return paginated history of GDPR erasure events for the caller's tenant."""
    _, tenant_id = _assert_tenant_user(current_user)

    return await _gdpr_service.list_erasure_requests(
        db,
        tenant_id=tenant_id,
        page=page,
        page_size=page_size,
    )


# ---------------------------------------------------------------------------
# POST /gdpr/admin/erase-user/{user_id}
# ---------------------------------------------------------------------------


@router.post(
    "/admin/erase-user/{user_id}",
    summary="Admin-initiated erasure of another user in the same tenant",
    dependencies=[Depends(require_role("admin"))],
)
async def admin_erase_user(
    user_id: UUID,
    body: AdminErasureRequestBody,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Anonymise another user's PII within the admin's tenant.

    Requires ``tenant_admin`` role.  The requesting admin's local user-id is
    recorded in the PlatformAuditLog ``details``.
    """
    requesting_user_id, tenant_id = _assert_tenant_user(current_user)

    # Prevent erasing yourself via the admin path (use the self-service route)
    if str(user_id) == requesting_user_id:
        raise AuthorizationError(
            "Use the self-service /gdpr/erasure-request endpoint to erase your own data"
        )

    result = await _gdpr_service.erase_user_data(
        db,
        user_id=str(user_id),
        tenant_id=tenant_id,
        requested_by_id=requesting_user_id,
    )

    # Augment the audit log details with the admin-supplied reason.
    # The audit row was already added to the session by erase_user_data;
    # we add a second, reason-annotated entry for full traceability.
    from app.models.identity import PlatformAuditLog  # local import to avoid circularity

    reason_log = PlatformAuditLog(
        actor_id=requesting_user_id,
        actor_email=current_user.email,
        platform_role="tenant_admin_gdpr",
        tenant_id=UUID(tenant_id),
        http_method="POST",
        endpoint=f"/api/v1/gdpr/admin/erase-user/{user_id}",
        request_id="",
        ip_address="",
        action="gdpr_erasure_admin",
        details={
            "erased_user_id": str(user_id),
            "reason": body.reason,
            "requested_by": requesting_user_id,
        },
    )
    db.add(reason_log)

    await db.commit()

    logger.info(
        "gdpr_admin_erasure_executed",
        extra={
            "erased_user_id": str(user_id),
            "tenant_id": tenant_id,
            "admin_user_id": requesting_user_id,
        },
    )
    return result
