"""Tenant admin endpoints — settings, business hours, users, teams, SLA policies."""

from datetime import date as date_type
from datetime import datetime
from datetime import time
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from redis.asyncio import Redis
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1._refscope import ref_create_tenant_id, ref_visible_filter, require_ref_write
from app.auth.dependencies import CurrentUser, get_current_user, require_role
from app.config import get_settings
from app.database import get_db
from app.exceptions import AuthorizationError, ResourceNotFoundError
from app.models.identity import (
    Department,
    ITSMRole,
    PlatformAuditLog,
    Product,
    Team,
    TeamMember,
    Tenant,
    User,
    UserITSMRole,
)
from app.models.system_logs import SystemLog
from app.models.ticket import BusinessHoursConfig, SLAPolicy, TicketCategory
from app.redis_client import get_redis
from app.schemas.common import PaginatedResponse

router = APIRouter(prefix="/tenant", tags=["admin"])


# ---------------------------------------------------------------------------
# Helpers — guard tenant context
# ---------------------------------------------------------------------------


def _require_tenant(current_user: CurrentUser) -> UUID:
    if current_user.tenant_id is None:
        raise AuthorizationError("Tenant context required")
    return current_user.tenant_id


# ---------------------------------------------------------------------------
# Inline Pydantic schemas (admin-domain; kept here to avoid proliferating files)
# ---------------------------------------------------------------------------


class TenantSettingsResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    settings: dict[str, Any]


class UpdateTenantSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    settings: dict[str, Any]


class BusinessHoursResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    timezone: str
    work_days: list[int]
    work_start_time: time
    work_end_time: time
    holidays: list[Any]


class UpsertBusinessHoursRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    timezone: str = Field(min_length=1, max_length=100)
    work_days: list[int] = Field(min_length=1)
    work_start_time: str = Field(pattern=r"^\d{2}:\d{2}$", description="HH:MM")
    work_end_time: str = Field(pattern=r"^\d{2}:\d{2}$", description="HH:MM")
    holidays: list[str] = Field(default_factory=list, description="YYYY-MM-DD dates")


class UserSummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    email: str
    first_name: str
    last_name: str
    is_active: bool
    roles: list[str] = []          # itsm-derived roles (UserITSMRole)
    iam_roles: list[str] = []      # IAM roles synced from the token on login


class UpdateUserRolesRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    role_names: list[str] = Field(min_length=1)


class TeamResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str
    description: str | None
    lead_id: UUID | None


class CreateTeamRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    lead_id: UUID | None = None


class SLAPolicyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str
    response_time_minutes: int
    resolution_time_minutes: int
    business_hours_only: bool


class CreateSLAPolicyRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    name: str = Field(min_length=1, max_length=255)
    response_time_minutes: int = Field(ge=1)
    resolution_time_minutes: int = Field(ge=1)
    business_hours_only: bool = True


class UpdateSLAPolicyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=255)
    response_time_minutes: int | None = Field(default=None, ge=1)
    resolution_time_minutes: int | None = Field(default=None, ge=1)
    business_hours_only: bool | None = None


class DepartmentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    tenant_id: UUID | None = None  # None = global department
    name: str
    description: str | None = None
    parent_id: UUID | None = None
    iam_facility_id: str | None = None
    is_active: bool


class CreateDepartmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    parent_id: UUID | None = None
    iam_facility_id: str | None = Field(default=None, max_length=255)


class UpdateDepartmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    parent_id: UUID | None = None
    iam_facility_id: str | None = Field(default=None, max_length=255)
    is_active: bool | None = None


class ProductResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    tenant_id: UUID
    name: str
    slug: str
    description: str | None = None
    is_active: bool


class TicketCategoryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    tenant_id: UUID | None = None  # None = global category
    name: str
    description: str | None = None
    parent_id: UUID | None = None
    product_id: UUID | None = None
    default_sla_policy_id: UUID | None = None
    default_team_id: UUID | None = None
    is_active: bool


class CreateTicketCategoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    parent_id: UUID | None = None
    product_id: UUID | None = None
    default_sla_policy_id: UUID | None = None
    default_team_id: UUID | None = None


class UpdateTicketCategoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    parent_id: UUID | None = None
    product_id: UUID | None = None
    default_sla_policy_id: UUID | None = None
    default_team_id: UUID | None = None
    is_active: bool | None = None


class TeamMemberResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    user_id: UUID
    name: str
    email: str
    is_lead: bool = False
    joined_at: datetime | None = None


class AddTeamMemberRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: UUID


class SystemLogResponse(BaseModel):
    """System log projection. NB: stack_trace is deliberately excluded —
    it must never be forwarded in an HTTP response."""
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    level: str
    event: str
    message: str
    exception_type: str | None = None
    request_id: str | None = None
    created_at: datetime


# ---------------------------------------------------------------------------
# GET /tenant/settings
# ---------------------------------------------------------------------------


@router.get("/settings", response_model=TenantSettingsResponse)
async def get_tenant_settings(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TenantSettingsResponse:
    """Return the JSONB settings blob for the current tenant."""
    tenant_id = _require_tenant(current_user)

    result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    tenant = result.scalar_one_or_none()
    if tenant is None:
        raise ResourceNotFoundError("tenant", str(tenant_id))

    return TenantSettingsResponse(settings=tenant.settings)


# ---------------------------------------------------------------------------
# PATCH /tenant/settings
# ---------------------------------------------------------------------------


@router.patch("/settings", response_model=TenantSettingsResponse)
async def update_tenant_settings(
    body: UpdateTenantSettingsRequest,
    current_user: CurrentUser = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> TenantSettingsResponse:
    """Shallow-merge settings into the tenant JSONB blob. Admin only."""
    tenant_id = _require_tenant(current_user)

    result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    tenant = result.scalar_one_or_none()
    if tenant is None:
        raise ResourceNotFoundError("tenant", str(tenant_id))

    # Shallow merge: caller's keys override existing keys
    merged = {**tenant.settings, **body.settings}
    tenant.settings = merged
    db.add(tenant)
    await db.commit()
    await db.refresh(tenant)
    return TenantSettingsResponse(settings=tenant.settings)


# ---------------------------------------------------------------------------
# GET /tenant/business-hours
# ---------------------------------------------------------------------------


@router.get("/business-hours", response_model=BusinessHoursResponse)
async def get_business_hours(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> BusinessHoursResponse:
    """Return the business hours configuration for the current tenant."""
    tenant_id = _require_tenant(current_user)

    result = await db.execute(
        select(BusinessHoursConfig).where(BusinessHoursConfig.tenant_id == tenant_id)
    )
    bh = result.scalar_one_or_none()
    if bh is None:
        raise ResourceNotFoundError("business_hours_config", str(tenant_id))

    return BusinessHoursResponse.model_validate(bh)


# ---------------------------------------------------------------------------
# PATCH /tenant/business-hours
# ---------------------------------------------------------------------------


@router.patch("/business-hours", response_model=BusinessHoursResponse)
async def upsert_business_hours(
    body: UpsertBusinessHoursRequest,
    current_user: CurrentUser = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> BusinessHoursResponse:
    """Create or update the business hours config for the tenant. Admin only."""
    from datetime import date as date_type

    tenant_id = _require_tenant(current_user)

    result = await db.execute(
        select(BusinessHoursConfig).where(BusinessHoursConfig.tenant_id == tenant_id)
    )
    bh = result.scalar_one_or_none()

    # Parse HH:MM strings to time objects
    start_h, start_m = [int(x) for x in body.work_start_time.split(":")]
    end_h, end_m = [int(x) for x in body.work_end_time.split(":")]
    work_start = time(start_h, start_m)
    work_end = time(end_h, end_m)

    # Parse holiday date strings
    holiday_dates = []
    for d in body.holidays:
        try:
            holiday_dates.append(date_type.fromisoformat(d))
        except (ValueError, TypeError):
            pass  # Silently skip malformed dates

    if bh is None:
        bh = BusinessHoursConfig(
            tenant_id=tenant_id,
            timezone=body.timezone,
            work_days=body.work_days,
            work_start_time=work_start,
            work_end_time=work_end,
            holidays=holiday_dates,
        )
        db.add(bh)
    else:
        bh.timezone = body.timezone
        bh.work_days = body.work_days
        bh.work_start_time = work_start
        bh.work_end_time = work_end
        bh.holidays = holiday_dates
        db.add(bh)

    await db.commit()
    await db.refresh(bh)
    return BusinessHoursResponse.model_validate(bh)


# ---------------------------------------------------------------------------
# GET /tenant/users
# ---------------------------------------------------------------------------


@router.get("/users", response_model=PaginatedResponse[UserSummaryResponse])
async def list_tenant_users(
    limit: int = Query(default=25, ge=1, le=100),
    cursor: str | None = Query(default=None),
    current_user: CurrentUser = Depends(require_role("admin", "manager")),
    db: AsyncSession = Depends(get_db),
) -> PaginatedResponse[UserSummaryResponse]:
    """Paginated list of tenant users with their ITSM roles."""
    tenant_id = _require_tenant(current_user)

    filters = [User.tenant_id == tenant_id]
    if cursor:
        try:
            filters.append(User.id > UUID(cursor))
        except ValueError:
            pass

    result = await db.execute(
        select(User).where(and_(*filters)).order_by(User.id).limit(limit + 1)
    )
    users = list(result.scalars().all())

    has_more = len(users) > limit
    items = users[:limit]
    next_cursor = str(items[-1].id) if has_more and items else None

    # Fetch roles for all users in one query
    if items:
        role_result = await db.execute(
            select(UserITSMRole.user_id, ITSMRole.name)
            .join(ITSMRole, ITSMRole.id == UserITSMRole.role_id)
            .where(
                UserITSMRole.user_id.in_([u.id for u in items]),
                UserITSMRole.tenant_id == tenant_id,
            )
        )
        roles_by_user: dict[UUID, list[str]] = {}
        for row in role_result.all():
            roles_by_user.setdefault(row.user_id, []).append(row.name)
    else:
        roles_by_user = {}

    responses = [
        UserSummaryResponse(
            id=u.id,
            email=u.email,
            first_name=u.first_name,
            last_name=u.last_name,
            is_active=u.is_active,
            roles=roles_by_user.get(u.id, []),
            iam_roles=list(u.iam_roles or []),
        )
        for u in items
    ]

    return PaginatedResponse(items=responses, next_cursor=next_cursor)


# ---------------------------------------------------------------------------
# PATCH /tenant/users/{user_id}/roles
# ---------------------------------------------------------------------------


@router.patch("/users/{user_id}/roles", response_model=UserSummaryResponse)
async def update_user_roles(
    user_id: UUID,
    body: UpdateUserRolesRequest,
    current_user: CurrentUser = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> UserSummaryResponse:
    """Replace the ITSM roles for a tenant user. Admin only."""
    tenant_id = _require_tenant(current_user)

    result = await db.execute(
        select(User).where(and_(User.id == user_id, User.tenant_id == tenant_id))
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise ResourceNotFoundError("user", str(user_id))

    # Resolve role names to ITSMRole rows
    role_result = await db.execute(
        select(ITSMRole).where(
            and_(
                ITSMRole.name.in_(body.role_names),
                (ITSMRole.tenant_id == tenant_id) | (ITSMRole.tenant_id.is_(None)),
            )
        )
    )
    roles = list(role_result.scalars().all())

    # Remove existing role assignments for this user in this tenant
    existing_result = await db.execute(
        select(UserITSMRole).where(
            and_(UserITSMRole.user_id == user_id, UserITSMRole.tenant_id == tenant_id)
        )
    )
    for assignment in existing_result.scalars().all():
        await db.delete(assignment)

    # Add new assignments
    for role in roles:
        db.add(
            UserITSMRole(
                user_id=user_id,
                role_id=role.id,
                tenant_id=tenant_id,
                assigned_by=current_user.local_user_id,
            )
        )

    await db.commit()

    return UserSummaryResponse(
        id=user.id,
        email=user.email,
        first_name=user.first_name,
        last_name=user.last_name,
        is_active=user.is_active,
        roles=[r.name for r in roles],
    )


# ---------------------------------------------------------------------------
# GET /tenant/teams
# ---------------------------------------------------------------------------


@router.get("/teams", response_model=list[TeamResponse])
async def list_teams(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[TeamResponse]:
    """Return all teams for the current tenant."""
    tenant_id = _require_tenant(current_user)

    result = await db.execute(select(Team).where(Team.tenant_id == tenant_id).order_by(Team.name))
    return [TeamResponse.model_validate(t) for t in result.scalars().all()]


# ---------------------------------------------------------------------------
# POST /tenant/teams
# ---------------------------------------------------------------------------


@router.post("/teams", response_model=TeamResponse, status_code=201)
async def create_team(
    body: CreateTeamRequest,
    current_user: CurrentUser = Depends(require_role("admin", "manager")),
    db: AsyncSession = Depends(get_db),
) -> TeamResponse:
    """Create a new team. Admin or manager only."""
    tenant_id = _require_tenant(current_user)

    team = Team(
        tenant_id=tenant_id,
        name=body.name,
        description=body.description,
        lead_id=body.lead_id,
    )
    db.add(team)
    await db.commit()
    await db.refresh(team)
    return TeamResponse.model_validate(team)


# ---------------------------------------------------------------------------
# GET /tenant/sla-policies
# ---------------------------------------------------------------------------


@router.get("/sla-policies", response_model=list[SLAPolicyResponse])
async def list_sla_policies(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[SLAPolicyResponse]:
    """Return all SLA policies for the current tenant."""
    tenant_id = _require_tenant(current_user)

    result = await db.execute(
        select(SLAPolicy).where(SLAPolicy.tenant_id == tenant_id).order_by(SLAPolicy.name)
    )
    return [SLAPolicyResponse.model_validate(p) for p in result.scalars().all()]


# ---------------------------------------------------------------------------
# POST /tenant/sla-policies
# ---------------------------------------------------------------------------


@router.post("/sla-policies", response_model=SLAPolicyResponse, status_code=201)
async def create_sla_policy(
    body: CreateSLAPolicyRequest,
    current_user: CurrentUser = Depends(require_role("admin", "manager")),
    db: AsyncSession = Depends(get_db),
) -> SLAPolicyResponse:
    """Create an SLA policy. Admin or manager only."""
    tenant_id = _require_tenant(current_user)

    policy = SLAPolicy(
        tenant_id=tenant_id,
        name=body.name,
        response_time_minutes=body.response_time_minutes,
        resolution_time_minutes=body.resolution_time_minutes,
        business_hours_only=body.business_hours_only,
    )
    db.add(policy)
    await db.commit()
    await db.refresh(policy)
    return SLAPolicyResponse.model_validate(policy)


# ---------------------------------------------------------------------------
# PATCH /tenant/sla-policies/{id}
# ---------------------------------------------------------------------------


@router.patch("/sla-policies/{policy_id}", response_model=SLAPolicyResponse)
async def update_sla_policy(
    policy_id: UUID,
    body: UpdateSLAPolicyRequest,
    current_user: CurrentUser = Depends(require_role("admin", "manager")),
    db: AsyncSession = Depends(get_db),
) -> SLAPolicyResponse:
    """Update an SLA policy. Admin or manager only."""
    tenant_id = _require_tenant(current_user)

    result = await db.execute(
        select(SLAPolicy).where(
            and_(SLAPolicy.id == policy_id, SLAPolicy.tenant_id == tenant_id)
        )
    )
    policy = result.scalar_one_or_none()
    if policy is None:
        raise ResourceNotFoundError("sla_policy", str(policy_id))

    updates = body.model_dump(exclude_none=True)
    for field_name, value in updates.items():
        setattr(policy, field_name, value)

    db.add(policy)
    await db.commit()
    await db.refresh(policy)
    return SLAPolicyResponse.model_validate(policy)


# ---------------------------------------------------------------------------
# Departments  — /tenant/departments  (CRUD, self-referencing tree)
# ---------------------------------------------------------------------------


@router.get("/departments", response_model=list[DepartmentResponse])
async def list_departments(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    active_only: bool = Query(default=False),
) -> list[DepartmentResponse]:
    """Return departments visible to the caller (global + own tenant)."""
    q = select(Department)
    scope = ref_visible_filter(Department, current_user)
    if scope is not None:
        q = q.where(scope)
    if active_only:
        q = q.where(Department.is_active.is_(True))
    q = q.order_by(Department.name)
    result = await db.execute(q)
    return [DepartmentResponse.model_validate(d) for d in result.scalars().all()]


@router.post("/departments", response_model=DepartmentResponse, status_code=201)
async def create_department(
    body: CreateDepartmentRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DepartmentResponse:
    """Create a department (global if platform/all-tenants, else tenant-scoped)."""
    target_tenant_id = ref_create_tenant_id(current_user)
    require_ref_write(current_user, target_tenant_id)

    if body.parent_id is not None:
        await _get_visible_department(db, body.parent_id, current_user)

    dept = Department(
        tenant_id=target_tenant_id,
        name=body.name,
        description=body.description,
        parent_id=body.parent_id,
        iam_facility_id=body.iam_facility_id,
    )
    db.add(dept)
    await db.commit()
    await db.refresh(dept)
    return DepartmentResponse.model_validate(dept)


@router.patch("/departments/{department_id}", response_model=DepartmentResponse)
async def update_department(
    department_id: UUID,
    body: UpdateDepartmentRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DepartmentResponse:
    """Update a department (platform for global rows; manager/admin for own tenant)."""
    dept = await _get_visible_department(db, department_id, current_user)
    require_ref_write(current_user, dept.tenant_id)

    updates = body.model_dump(exclude_unset=True)
    new_parent = updates.get("parent_id")
    if new_parent is not None:
        if new_parent == department_id:
            raise AuthorizationError("A department cannot be its own parent")
        await _get_visible_department(db, new_parent, current_user)

    for field_name, value in updates.items():
        setattr(dept, field_name, value)

    db.add(dept)
    await db.commit()
    await db.refresh(dept)
    return DepartmentResponse.model_validate(dept)


async def _get_visible_department(
    db: AsyncSession, department_id: UUID, current_user: CurrentUser
) -> Department:
    q = select(Department).where(Department.id == department_id)
    scope = ref_visible_filter(Department, current_user)
    if scope is not None:
        q = q.where(scope)
    dept = (await db.execute(q)).scalar_one_or_none()
    if dept is None:
        raise ResourceNotFoundError("department", str(department_id))
    return dept


# ---------------------------------------------------------------------------
# Products  — /tenant/products  (read-only list, used by the KB access-level
# picker to scope an article to a specific product)
# ---------------------------------------------------------------------------


@router.get("/products", response_model=list[ProductResponse])
async def list_products(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    active_only: bool = Query(default=True),
) -> list[ProductResponse]:
    """List the caller's tenant's products.

    Products are tenant-scoped (Product.tenant_id is NOT NULL). Tenant users
    get their own tenant's products; platform users get the products of the
    tenant they've selected (empty when none is selected — there are no global
    products to fall back to).
    """
    if current_user.tenant_id is None:
        return []
    q = select(Product).where(Product.tenant_id == current_user.tenant_id)
    if active_only:
        q = q.where(Product.is_active.is_(True))
    q = q.order_by(Product.name)
    result = await db.execute(q)
    return [ProductResponse.model_validate(p) for p in result.scalars().all()]


class ProductSyncItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    slug: str = Field(min_length=1, max_length=100)
    name: str | None = Field(default=None, max_length=255)


class ProductSyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    products: list[ProductSyncItem]


@router.post("/products/sync", response_model=list[ProductResponse])
async def sync_products(
    body: ProductSyncRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ProductResponse]:
    """Project the caller tenant's IAM-subscribed products into the products table.

    IAM is the source of authority; this table is a slug-keyed projection. Upsert
    by (tenant, slug): subscribed → active (refresh name); no-longer-subscribed →
    soft-deactivate (is_active=false). NEVER deletes — tickets/KB/assets FK to
    products and historical references must survive. Returns the active set.
    """
    if current_user.tenant_id is None:
        return []
    tenant_id = current_user.tenant_id

    existing = {
        p.slug: p
        for p in (
            await db.execute(select(Product).where(Product.tenant_id == tenant_id))
        ).scalars().all()
    }
    subscribed_slugs = set()
    for item in body.products:
        slug = item.slug.strip()
        if not slug:
            continue
        subscribed_slugs.add(slug)
        p = existing.get(slug)
        if p is None:
            db.add(Product(tenant_id=tenant_id, slug=slug,
                           name=(item.name or slug), is_active=True))
        else:
            if item.name and p.name != item.name:
                p.name = item.name
            if not p.is_active:
                p.is_active = True

    # Soft-deactivate products no longer in the subscription (keep the row).
    for slug, p in existing.items():
        if slug not in subscribed_slugs and p.is_active:
            p.is_active = False

    await db.commit()

    q = (
        select(Product)
        .where(Product.tenant_id == tenant_id, Product.is_active.is_(True))
        .order_by(Product.name)
    )
    return [
        ProductResponse.model_validate(p)
        for p in (await db.execute(q)).scalars().all()
    ]


# ---------------------------------------------------------------------------
# Ticket Categories  — /tenant/ticket-categories  (CRUD, self-referencing tree)
# ---------------------------------------------------------------------------


@router.get("/ticket-categories", response_model=list[TicketCategoryResponse])
async def list_ticket_categories(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    active_only: bool = Query(default=False),
) -> list[TicketCategoryResponse]:
    """Return ticket categories visible to the caller (global + own tenant)."""
    q = select(TicketCategory)
    scope = ref_visible_filter(TicketCategory, current_user)
    if scope is not None:
        q = q.where(scope)
    if active_only:
        q = q.where(TicketCategory.is_active.is_(True))
    q = q.order_by(TicketCategory.name)
    result = await db.execute(q)
    return [TicketCategoryResponse.model_validate(c) for c in result.scalars().all()]


@router.post("/ticket-categories", response_model=TicketCategoryResponse, status_code=201)
async def create_ticket_category(
    body: CreateTicketCategoryRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TicketCategoryResponse:
    """Create a ticket category (global if platform/all-tenants, else tenant-scoped)."""
    target_tenant_id = ref_create_tenant_id(current_user)
    require_ref_write(current_user, target_tenant_id)

    if body.parent_id is not None:
        await _get_visible_ticket_category(db, body.parent_id, current_user)

    category = TicketCategory(
        tenant_id=target_tenant_id,
        name=body.name,
        description=body.description,
        parent_id=body.parent_id,
        product_id=body.product_id,
        default_sla_policy_id=body.default_sla_policy_id,
        default_team_id=body.default_team_id,
    )
    db.add(category)
    await db.commit()
    await db.refresh(category)
    return TicketCategoryResponse.model_validate(category)


@router.patch("/ticket-categories/{category_id}", response_model=TicketCategoryResponse)
async def update_ticket_category(
    category_id: UUID,
    body: UpdateTicketCategoryRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TicketCategoryResponse:
    """Update a ticket category (platform for global rows; manager/admin for own tenant)."""
    category = await _get_visible_ticket_category(db, category_id, current_user)
    require_ref_write(current_user, category.tenant_id)

    updates = body.model_dump(exclude_unset=True)
    new_parent = updates.get("parent_id")
    if new_parent is not None:
        if new_parent == category_id:
            raise AuthorizationError("A category cannot be its own parent")
        await _get_visible_ticket_category(db, new_parent, current_user)

    for field_name, value in updates.items():
        setattr(category, field_name, value)

    db.add(category)
    await db.commit()
    await db.refresh(category)
    return TicketCategoryResponse.model_validate(category)


async def _get_visible_ticket_category(
    db: AsyncSession, category_id: UUID, current_user: CurrentUser
) -> TicketCategory:
    q = select(TicketCategory).where(TicketCategory.id == category_id)
    scope = ref_visible_filter(TicketCategory, current_user)
    if scope is not None:
        q = q.where(scope)
    category = (await db.execute(q)).scalar_one_or_none()
    if category is None:
        raise ResourceNotFoundError("ticket_category", str(category_id))
    return category


# ---------------------------------------------------------------------------
# Team members  — /tenant/teams/{team_id}/members  (list / add / remove)
# ---------------------------------------------------------------------------


async def _get_tenant_team(db: AsyncSession, team_id: UUID, tenant_id: UUID) -> Team:
    result = await db.execute(
        select(Team).where(and_(Team.id == team_id, Team.tenant_id == tenant_id))
    )
    team = result.scalar_one_or_none()
    if team is None:
        raise ResourceNotFoundError("team", str(team_id))
    return team


def _user_label(u: User) -> str:
    return f"{u.first_name or ''} {u.last_name or ''}".strip() or u.email


@router.get("/teams/{team_id}/members", response_model=list[TeamMemberResponse])
async def list_team_members(
    team_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[TeamMemberResponse]:
    """List members of a team."""
    tenant_id = _require_tenant(current_user)
    team = await _get_tenant_team(db, team_id, tenant_id)

    result = await db.execute(
        select(TeamMember, User)
        .join(User, User.id == TeamMember.user_id)
        .where(TeamMember.team_id == team_id)
        .order_by(User.first_name, User.last_name)
    )
    return [
        TeamMemberResponse(
            user_id=u.id,
            name=_user_label(u),
            email=u.email,
            is_lead=(team.lead_id == u.id),
            joined_at=tm.joined_at,
        )
        for tm, u in result.all()
    ]


@router.post("/teams/{team_id}/members", response_model=TeamMemberResponse, status_code=201)
async def add_team_member(
    team_id: UUID,
    body: AddTeamMemberRequest,
    current_user: CurrentUser = Depends(require_role("admin", "manager")),
    db: AsyncSession = Depends(get_db),
) -> TeamMemberResponse:
    """Add a user to a team. Admin or manager only. Idempotent."""
    tenant_id = _require_tenant(current_user)
    team = await _get_tenant_team(db, team_id, tenant_id)

    # User must belong to the same tenant
    user_res = await db.execute(
        select(User).where(and_(User.id == body.user_id, User.tenant_id == tenant_id))
    )
    user = user_res.scalar_one_or_none()
    if user is None:
        raise ResourceNotFoundError("user", str(body.user_id))

    existing = await db.execute(
        select(TeamMember).where(
            and_(TeamMember.team_id == team_id, TeamMember.user_id == body.user_id)
        )
    )
    if existing.scalar_one_or_none() is None:
        db.add(TeamMember(team_id=team_id, user_id=body.user_id))
        await db.commit()

    member = await db.execute(
        select(TeamMember).where(
            and_(TeamMember.team_id == team_id, TeamMember.user_id == body.user_id)
        )
    )
    tm = member.scalar_one()
    return TeamMemberResponse(
        user_id=user.id, name=_user_label(user), email=user.email,
        is_lead=(team.lead_id == user.id), joined_at=tm.joined_at,
    )


@router.delete("/teams/{team_id}/members/{user_id}", status_code=204)
async def remove_team_member(
    team_id: UUID,
    user_id: UUID,
    current_user: CurrentUser = Depends(require_role("admin", "manager")),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Remove a user from a team. Admin or manager only."""
    tenant_id = _require_tenant(current_user)
    await _get_tenant_team(db, team_id, tenant_id)

    result = await db.execute(
        select(TeamMember).where(
            and_(TeamMember.team_id == team_id, TeamMember.user_id == user_id)
        )
    )
    member = result.scalar_one_or_none()
    if member is None:
        raise ResourceNotFoundError("team_member", f"{team_id}:{user_id}")
    await db.delete(member)
    await db.commit()


# ---------------------------------------------------------------------------
# System logs  — /tenant/system-logs  (admin only; read-only)
# ---------------------------------------------------------------------------


@router.get("/system-logs", response_model=list[SystemLogResponse])
async def list_system_logs(
    current_user: CurrentUser = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
    level: str | None = Query(default=None, description="warning | error | critical"),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[SystemLogResponse]:
    """List WARNING/ERROR/CRITICAL system logs for the tenant (admin only).

    stack_trace is never included in the response (SystemLogResponse omits it).
    """
    tenant_id = _require_tenant(current_user)

    q = select(SystemLog).where(SystemLog.tenant_id == tenant_id)
    if level:
        q = q.where(SystemLog.level == level.lower())
    q = q.order_by(SystemLog.created_at.desc()).limit(limit)

    result = await db.execute(q)
    return [SystemLogResponse.model_validate(row) for row in result.scalars().all()]


# ---------------------------------------------------------------------------
# AI budget schemas
# ---------------------------------------------------------------------------


class AIBudgetResponse(BaseModel):
    month: str
    used_tokens: int
    budget_tokens: int
    percentage: float


class UpdateAIBudgetRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    monthly_tokens: int = Field(ge=0)


# ---------------------------------------------------------------------------
# Audit-log schema
# ---------------------------------------------------------------------------


class AuditLogItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    actor_id: str
    actor_email: str
    platform_role: str
    http_method: str
    endpoint: str
    action: str | None
    details: dict | None
    accessed_at: Any  # datetime — Any avoids serialiser imports


class AuditLogPageResponse(BaseModel):
    items: list[AuditLogItemResponse]
    total: int
    page: int
    page_size: int


# ---------------------------------------------------------------------------
# GET /tenant/ai-budget
# ---------------------------------------------------------------------------


async def _read_ai_budget(tenant_id: str, redis: Redis) -> tuple[int, int]:
    """Return (used_tokens, budget_tokens) for the current calendar month."""
    from datetime import datetime

    s = get_settings()
    month = datetime.now().strftime("%Y-%m")
    key = f"ai_budget:{tenant_id}:{month}"
    used = int(await redis.get(key) or 0)
    return used, s.AI_DEFAULT_MONTHLY_TOKEN_BUDGET


@router.get("/ai-budget", response_model=AIBudgetResponse)
async def get_ai_budget(
    current_user: CurrentUser = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> AIBudgetResponse:
    """Return current-month AI token usage vs. the tenant budget cap. Admin only."""
    from datetime import datetime

    tenant_id = _require_tenant(current_user)

    result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    tenant = result.scalar_one_or_none()
    if tenant is None:
        raise ResourceNotFoundError("tenant", str(tenant_id))

    s = get_settings()
    month = datetime.now().strftime("%Y-%m")
    key = f"ai_budget:{str(tenant_id)}:{month}"
    used = int(await redis.get(key) or 0)
    budget = tenant.ai_budget_monthly_tokens or s.AI_DEFAULT_MONTHLY_TOKEN_BUDGET
    pct = round((used / budget * 100), 2) if budget else 0.0

    return AIBudgetResponse(month=month, used_tokens=used, budget_tokens=budget, percentage=pct)


# ---------------------------------------------------------------------------
# PATCH /tenant/ai-budget
# ---------------------------------------------------------------------------


@router.patch("/ai-budget", response_model=AIBudgetResponse)
async def update_ai_budget(
    body: UpdateAIBudgetRequest,
    current_user: CurrentUser = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> AIBudgetResponse:
    """Update the monthly AI token budget cap for the tenant. Admin only."""
    from datetime import datetime

    tenant_id = _require_tenant(current_user)

    result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    tenant = result.scalar_one_or_none()
    if tenant is None:
        raise ResourceNotFoundError("tenant", str(tenant_id))

    tenant.ai_budget_monthly_tokens = body.monthly_tokens
    db.add(tenant)
    await db.commit()
    await db.refresh(tenant)

    s = get_settings()
    month = datetime.now().strftime("%Y-%m")
    key = f"ai_budget:{str(tenant_id)}:{month}"
    used = int(await redis.get(key) or 0)
    budget = tenant.ai_budget_monthly_tokens or s.AI_DEFAULT_MONTHLY_TOKEN_BUDGET
    pct = round((used / budget * 100), 2) if budget else 0.0

    return AIBudgetResponse(month=month, used_tokens=used, budget_tokens=budget, percentage=pct)


# ---------------------------------------------------------------------------
# GET /tenant/audit-log
# ---------------------------------------------------------------------------


@router.get("/audit-log", response_model=AuditLogPageResponse)
async def get_audit_log(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    start_date: date_type | None = Query(default=None),
    end_date: date_type | None = Query(default=None),
    current_user: CurrentUser = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> AuditLogPageResponse:
    """Paginated audit log for the current tenant. Admin only."""
    tenant_id = _require_tenant(current_user)

    filters = [PlatformAuditLog.tenant_id == tenant_id]
    if start_date:
        filters.append(func.date(PlatformAuditLog.accessed_at) >= start_date)
    if end_date:
        filters.append(func.date(PlatformAuditLog.accessed_at) <= end_date)

    count_result = await db.execute(
        select(func.count()).select_from(PlatformAuditLog).where(and_(*filters))
    )
    total = count_result.scalar_one()

    offset = (page - 1) * page_size
    rows_result = await db.execute(
        select(PlatformAuditLog)
        .where(and_(*filters))
        .order_by(PlatformAuditLog.accessed_at.desc())
        .offset(offset)
        .limit(page_size)
    )
    items = [AuditLogItemResponse.model_validate(row) for row in rows_result.scalars().all()]

    return AuditLogPageResponse(items=items, total=total, page=page, page_size=page_size)
