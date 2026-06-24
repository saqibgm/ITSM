"""
FastAPI dependencies for three-tier identity resolution.

Tier model (per 05-iam-integration.md):
  - PLATFORM  — 99Technologies org users (platform_admin / platform_support)
  - TENANT    — customer org users (admin / agent / end_user / service_account)
"""

import logging
from typing import Literal
from uuid import UUID

from fastapi import Depends, Request
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.jwks import verify_token
from app.config import get_settings
from app.database import get_db
from app.exceptions import (
    AuthenticationError,
    AuthorizationError,
    ResourceNotFoundError,
    SubscriptionRequiredError,
    TenantSuspendedError,
)
from app.models.identity import ITSMRole, PlatformAuditLog, Tenant, User, UserITSMRole

logger = logging.getLogger(__name__)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token", auto_error=False)

# IAM role → ITSM platform role mapping
_IAM_TO_PLATFORM_ROLE: dict[str, str] = {
    "super_admin": "platform_admin",
    "admin": "platform_admin",
    "org_admin": "platform_admin",
    "app_developer": "platform_support",
    "operator": "platform_support",
}

# IAM role → ITSM tenant role name mapping.
# org_admin / super_admin are treated as full admins within their tenant
# (they map to platform_admin in the platform org via _IAM_TO_PLATFORM_ROLE).
_IAM_TO_TENANT_ROLE: dict[str, str] = {
    "super_admin": "admin",
    "org_admin": "admin",
    "admin": "admin",
    "operator": "agent",
    "user": "end_user",
    "bot": "service_account",
}

# When a user carries several IAM roles, the highest-privilege one wins
# (rather than blindly taking iam_roles[0]).
_PLATFORM_ROLE_RANK = {"platform_admin": 2, "platform_support": 1}
_TENANT_ROLE_RANK = {"admin": 4, "manager": 3, "agent": 2, "service_account": 1, "end_user": 0}


def _best_platform_role(iam_roles: list[str]) -> str | None:
    """Highest platform role among the user's IAM roles, or None.

    Matching is case-insensitive: in Keycloak `operator`/`user`/`bot`/`admin`
    are *client* roles on project-iq (capitalized — 'Operator', 'Admin', …),
    while super_admin/org_admin are *realm* roles. Callers pass the combined
    realm+client set so client roles are honoured."""
    best = None
    for r in iam_roles:
        mapped = _IAM_TO_PLATFORM_ROLE.get(r.lower())
        if mapped and (best is None or _PLATFORM_ROLE_RANK[mapped] > _PLATFORM_ROLE_RANK[best]):
            best = mapped
    return best


def _best_tenant_role(iam_roles: list[str]) -> str:
    """Highest tenant role among the user's IAM roles (default end_user).

    Case-insensitive — see _best_platform_role: 'Operator' (client role) →
    'operator' → 'agent'. Pass the realm+client role set."""
    best = "end_user"
    for r in iam_roles:
        mapped = _IAM_TO_TENANT_ROLE.get(r.lower())
        if mapped and _TENANT_ROLE_RANK.get(mapped, 0) > _TENANT_ROLE_RANK.get(best, 0):
            best = mapped
    return best


class CurrentUser(BaseModel):
    iam_user_id: str  # jwt.sub
    org_id: str  # jwt.org_id
    email: str
    roles: list[str]
    tier: Literal["platform", "tenant"]
    platform_role: str | None  # "platform_admin" | "platform_support" | None
    tenant_id: UUID | None  # resolved local tenant UUID (None for platform without X-Tenant-ID)
    local_user_id: UUID | None  # local users.id mirror (None for platform users)
    is_active: bool = True

    model_config = {"from_attributes": True}


async def _resolve_tenant_by_org_id(db: AsyncSession, org_id: str) -> Tenant | None:
    """Return the Tenant row for the given IAM org_id, or None."""
    result = await db.execute(select(Tenant).where(Tenant.iam_org_id == org_id))
    return result.scalar_one_or_none()


async def _resolve_tenant_by_id(db: AsyncSession, tenant_id: UUID) -> Tenant | None:
    """Return the Tenant row for the given ITSM tenant UUID, or None."""
    result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    return result.scalar_one_or_none()


async def _get_or_provision_user(
    db: AsyncSession,
    tenant: Tenant,
    iam_user_id: str,
    email: str,
    first_name: str,
    last_name: str,
    avatar_url: str | None,
    iam_roles: list[str] | None = None,
) -> User:
    """Return the local User mirror, auto-provisioning if not present.

    `iam_roles` (the user's IAM realm + client roles from the token) is mirrored
    onto the row on each login — IAM stays the source of truth.
    """
    result = await db.execute(
        select(User).where(User.iam_user_id == iam_user_id, User.tenant_id == tenant.id)
    )
    user = result.scalar_one_or_none()

    if user is None:
        user = User(
            tenant_id=tenant.id,
            iam_user_id=iam_user_id,
            email=email,
            first_name=first_name or "",
            last_name=last_name or "",
            avatar_url=avatar_url,
            iam_roles=iam_roles or [],
            is_active=True,
        )
        db.add(user)
        await db.flush()  # populate user.id
        # Persist immediately. get_db never commits on its own, so on a read-only
        # request the provisioning would otherwise roll back at request end — and
        # a follow-up call referencing the (now-ephemeral) id would fail with
        # "user not found". Committing here gives the mirror a stable id and makes
        # silent provisioning work uniformly across reads and writes.
        await db.commit()
        await db.refresh(user)
        logger.info(
            "user_auto_provisioned",
            extra={"iam_user_id": iam_user_id, "tenant_id": str(tenant.id)},
        )
    elif iam_roles is not None and list(user.iam_roles or []) != iam_roles:
        # Sync IAM roles → our DB whenever they change (commit so a read-only
        # request still persists the update).
        user.iam_roles = iam_roles
        await db.commit()
        await db.refresh(user)

    return user


async def _get_user_itsm_roles(db: AsyncSession, user_id: UUID, tenant_id: UUID) -> list[str]:
    """Return the list of ITSM role names assigned to the local user."""
    result = await db.execute(
        select(ITSMRole.name)
        .join(UserITSMRole, UserITSMRole.role_id == ITSMRole.id)
        .where(
            UserITSMRole.user_id == user_id,
            UserITSMRole.tenant_id == tenant_id,
        )
    )
    return [row[0] for row in result.all()]


async def _write_platform_audit_log(
    db: AsyncSession,
    *,
    actor_id: str,
    actor_email: str,
    platform_role: str,
    tenant_id: UUID | None,
    http_method: str,
    endpoint: str,
    request_id: str | None,
    ip_address: str | None,
) -> None:
    """Append one row to platform_audit_log (INSERT only, no update/delete)."""
    log_entry = PlatformAuditLog(
        actor_id=actor_id,
        actor_email=actor_email,
        platform_role=platform_role,
        tenant_id=tenant_id,
        http_method=http_method,
        endpoint=endpoint,
        request_id=request_id or "",
        ip_address=ip_address or "",
    )
    db.add(log_entry)
    # We do NOT commit here — the request-level session commit handles it.
    # If the session is rolled back the audit entry is also rolled back, which
    # is intentional (no orphan audit rows for requests that failed before auth).


async def get_current_user(
    request: Request,
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> CurrentUser:
    """Resolve the authenticated identity from the Bearer token.

    Raises AuthenticationError if the token is missing or invalid.
    """
    if not token:
        raise AuthenticationError("Authorization token is required")

    # Verify signature, expiry, issuer, audience — never log the token value
    payload = verify_token(token)

    settings = get_settings()

    iam_user_id: str = payload.get("sub", "")
    # org_id: flat claim OR extracted from Keycloak organization claim {"alias": {"id": "uuid"}}
    org_id: str = payload.get("org_id", "")
    if not org_id:
        org_claim = payload.get("organization", {})
        if isinstance(org_claim, dict):
            for v in org_claim.values():
                if isinstance(v, dict):
                    org_id = v.get("id", "")
                    break
    email: str = payload.get("email", "")
    # roles: flat claim OR extracted from Keycloak realm_access.roles
    iam_roles: list[str] = payload.get("roles", [])
    if not iam_roles:
        realm_access = payload.get("realm_access", {})
        iam_roles = realm_access.get("roles", []) if isinstance(realm_access, dict) else []
    # Full IAM role set = realm roles + client roles (resource_access), e.g. the
    # project-iq client's Member/User/Bot/Operator/Admin. Mirrored onto the user
    # row on login for display/query (excludes Keycloak's own infra clients).
    synced_iam_roles: list[str] = list(iam_roles)
    _resource_access = payload.get("resource_access", {})
    if isinstance(_resource_access, dict):
        for _cid, _v in _resource_access.items():
            if _cid in ("account", "account-console", "broker", "security-admin-console"):
                continue
            if isinstance(_v, dict):
                for _r in _v.get("roles", []):
                    if _r not in synced_iam_roles:
                        synced_iam_roles.append(_r)

    products: list[str] = payload.get("products", [])
    first_name: str = payload.get("given_name") or payload.get("first_name") or ""
    last_name: str = payload.get("family_name") or payload.get("last_name") or ""
    avatar_url: str | None = payload.get("picture") or payload.get("avatar_url")

    if not iam_user_id or not org_id:
        raise AuthenticationError("Token missing required claims (sub, org_id)")

    # ------------------------------------------------------------------ #
    # Branch 1 — PLATFORM USER (99Technologies org)                        #
    # ------------------------------------------------------------------ #
    if org_id == settings.PLATFORM_ORG_ID:
        # Highest platform role across all IAM roles (super_admin/admin/org_admin
        # → platform_admin; app_developer/operator → platform_support). Realm+client
        # set — 'operator' is a client role.
        platform_role = _best_platform_role(synced_iam_roles)
        if platform_role is None:
            raise AuthorizationError(
                f"None of the IAM roles {iam_roles} is a recognised platform role"
            )

        # Optionally resolve tenant from X-Tenant-ID header
        tenant_id_header: str | None = request.headers.get("X-Tenant-ID")
        resolved_tenant_id: UUID | None = None
        resolved_tenant: Tenant | None = None

        if tenant_id_header:
            try:
                tenant_uuid = UUID(tenant_id_header)
            except (ValueError, AttributeError):
                raise AuthorizationError("X-Tenant-ID header is not a valid UUID")

            tenant = await _resolve_tenant_by_id(db, tenant_uuid)
            if tenant is None:
                raise ResourceNotFoundError("tenant", tenant_id_header)

            resolved_tenant = tenant
            resolved_tenant_id = tenant.id
            request.state.tenant_id = str(tenant.id)

        # Write audit log for every platform request
        await _write_platform_audit_log(
            db,
            actor_id=iam_user_id,
            actor_email=email,
            platform_role=platform_role,
            tenant_id=resolved_tenant_id,
            http_method=request.method,
            endpoint=request.url.path,
            request_id=getattr(request.state, "request_id", None),
            ip_address=request.client.host if request.client else None,
        )

        logger.info(
            "platform_user_authenticated",
            extra={
                "platform_role": platform_role,
                "tenant_id": str(resolved_tenant_id) if resolved_tenant_id else None,
                "request_id": getattr(request.state, "request_id", None),
            },
        )

        # Platform (99T) users operate cross-tenant — bypass RLS. App-layer
        # scoping (X-Tenant-ID) still applies where relevant.
        await db.execute(text("SELECT set_config('app.bypass_rls', 'on', false)"))

        # Silently provision a local-user mirror for this platform actor in the
        # selected tenant (same as tenant-user auto-provisioning) so they can
        # author comments/attachments/etc. as themselves. Done AFTER bypass_rls
        # is engaged so the INSERT isn't blocked. Without a selected tenant there
        # is no single tenant to provision into, so local_user_id stays None.
        local_user_id: UUID | None = None
        if resolved_tenant is not None:
            _platform_local_user = await _get_or_provision_user(
                db,
                resolved_tenant,
                iam_user_id=iam_user_id,
                email=email,
                first_name=first_name,
                last_name=last_name,
                avatar_url=avatar_url,
                iam_roles=synced_iam_roles,
            )
            local_user_id = _platform_local_user.id

        # Carry itsm-equivalent role names so the tenant-admin role gates
        # (require_role / _has_role expect 'admin'/'manager'/…) recognise
        # platform staff — otherwise a 99T super_admin is rejected by every
        # itsm-role-gated endpoint. platform_admin == full admin; platform_
        # support == agent-level (read/operate, not configure).
        _PLATFORM_ITSM_ROLES = {
            "platform_admin": ["admin", "manager", "team_lead", "agent"],
            "platform_support": ["agent"],
        }
        combined_roles = list(
            dict.fromkeys([*iam_roles, *_PLATFORM_ITSM_ROLES.get(platform_role, [])])
        )

        return CurrentUser(
            iam_user_id=iam_user_id,
            org_id=org_id,
            email=email,
            roles=combined_roles,
            tier="platform",
            platform_role=platform_role,
            tenant_id=resolved_tenant_id,
            local_user_id=local_user_id,
            is_active=True,
        )

    # ------------------------------------------------------------------ #
    # Branch 2 — TENANT USER                                               #
    # ------------------------------------------------------------------ #

    # Check ITSM product subscription
    if "itsm" not in [p.lower() for p in products]:
        raise SubscriptionRequiredError(
            "Your organisation does not have an active ITSM subscription"
        )

    # Resolve tenant
    tenant = await _resolve_tenant_by_org_id(db, org_id)
    if tenant is None:
        raise ResourceNotFoundError("tenant", org_id)

    if not tenant.is_active:
        raise TenantSuspendedError()

    # Auto-provision local user mirror
    user = await _get_or_provision_user(
        db,
        tenant,
        iam_user_id=iam_user_id,
        email=email,
        first_name=first_name,
        last_name=last_name,
        avatar_url=avatar_url,
        iam_roles=synced_iam_roles,
    )

    if not user.is_active:
        raise AuthorizationError("Your account has been deactivated")

    # Map IAM role(s) → ITSM role name (highest-privilege wins; org_admin /
    # super_admin → admin within this tenant). Use the realm+client role set:
    # operator/user/bot are *client* roles, so realm-only would miss them and
    # operators would wrongly default to end_user instead of agent.
    default_itsm_role = _best_tenant_role(synced_iam_roles)

    # Fetch any promoted roles from user_itsm_roles; fall back to default mapping
    assigned_roles = await _get_user_itsm_roles(db, user.id, tenant.id)
    itsm_roles_list = assigned_roles if assigned_roles else [default_itsm_role]

    # Persist request-level context
    request.state.tenant_id = str(tenant.id)
    request.state.user_id = str(user.id)

    # Engage RLS for this tenant on the request's DB connection (session-level
    # so it survives the mid-request commits a SET LOCAL would lose). get_db
    # cleared it at request start; it's reset again on the next request.
    await db.execute(
        text("SELECT set_config('app.tenant_id', :tid, false)"),
        {"tid": str(tenant.id)},
    )

    return CurrentUser(
        iam_user_id=iam_user_id,
        org_id=org_id,
        email=email,
        roles=itsm_roles_list,
        tier="tenant",
        platform_role=None,
        tenant_id=tenant.id,
        local_user_id=user.id,
        is_active=user.is_active,
    )


# ---------------------------------------------------------------------------
# Role-enforcement dependencies
# ---------------------------------------------------------------------------


def require_role(*roles: str):
    """Return a FastAPI dependency that enforces the current user has one of
    the given ITSM role names.

    Usage::

        @router.get("/tickets", dependencies=[Depends(require_role("admin", "agent"))])
    """

    async def _check(current_user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        user_roles = set(current_user.roles)
        if not user_roles.intersection(roles):
            raise AuthorizationError(
                f"One of the following roles is required: {', '.join(roles)}"
            )
        return current_user

    return _check


def require_platform_admin():
    """FastAPI dependency: only platform_admin may proceed."""

    async def _check(current_user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if current_user.tier != "platform" or current_user.platform_role != "platform_admin":
            raise AuthorizationError("Platform admin role is required")
        return current_user

    return _check


def require_platform_support():
    """FastAPI dependency: platform_admin or platform_support may proceed."""

    async def _check(current_user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if current_user.tier != "platform" or current_user.platform_role not in (
            "platform_admin",
            "platform_support",
        ):
            raise AuthorizationError("Platform support or admin role is required")
        return current_user

    return _check
