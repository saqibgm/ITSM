"""Shared scoping helpers for reference-data routers (departments, ticket /
asset categories, asset types, vendors).

Reference data is dual-scoped, mirroring KB:
  - tenant_id NULL      -> GLOBAL: shared by every tenant, managed by platform
                           (99T) users only.
  - tenant_id NOT NULL  -> TENANT-SPECIFIC: managed by that tenant's
                           manager/admin (or a platform user acting on it).

Visibility:
  - Tenant users (and platform users with a tenant selected via X-Tenant-ID)
    see GLOBAL rows + their own tenant's rows.
  - Platform users with no tenant selected ("all tenants" view) see everything.
"""

from uuid import UUID

from sqlalchemy import or_

from app.auth.dependencies import CurrentUser
from app.exceptions import AuthorizationError

_MANAGER_ROLES = {"manager", "admin"}


def ref_visible_filter(model, current_user: CurrentUser):
    """SQLAlchemy condition limiting a query to rows the caller may see.

    Returns ``None`` when no restriction applies (platform user, all-tenants
    view) — callers should skip ``.where()`` in that case.
    """
    if current_user.tier == "platform" and current_user.tenant_id is None:
        return None  # platform all-tenants view: everything
    # tenant user, or platform with a selected tenant: global + that tenant
    return or_(model.tenant_id.is_(None), model.tenant_id == current_user.tenant_id)


def ref_create_tenant_id(current_user: CurrentUser) -> UUID | None:
    """tenant_id to stamp on a newly created reference row.

    Platform user with no tenant selected -> NULL (global).
    Otherwise -> the caller's (or selected) tenant.
    """
    if current_user.tier == "platform" and current_user.tenant_id is None:
        return None
    if current_user.tenant_id is None:
        raise AuthorizationError("Tenant context required")
    return current_user.tenant_id


def require_ref_write(current_user: CurrentUser, row_tenant_id: UUID | None) -> None:
    """Authorize a create/update/delete against a row's scope.

    - Platform users may manage global rows and any tenant's rows.
    - Tenant users may only manage their OWN tenant's rows (never global),
      and only with a manager/admin role.
    """
    if current_user.tier == "platform":
        return
    if row_tenant_id is None:
        raise AuthorizationError(
            "Only platform administrators can manage global reference data"
        )
    if current_user.tenant_id != row_tenant_id:
        raise AuthorizationError("This reference item belongs to another tenant")
    if not (set(current_user.roles) & _MANAGER_ROLES):
        raise AuthorizationError(
            "A manager or admin role is required to manage reference data"
        )
