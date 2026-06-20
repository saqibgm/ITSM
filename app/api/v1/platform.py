"""Platform-operator endpoints — cross-tenant management.

All routes require platform_admin or platform_support tier.
tenant_id always comes from path param and is validated against the DB before any action.

PlatformAuditLog is written for every mutating operation (PATCH, DELETE).
The table is INSERT-only — no UPDATE or DELETE ever.
"""

import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from redis.asyncio import Redis
from sqlalchemy import and_, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import (
    CurrentUser,
    require_platform_admin,
    require_platform_support,
)
from app.database import get_db
from app.exceptions import AuthorizationError, ResourceNotFoundError
from app.models.identity import PlatformAuditLog, Tenant
from app.models.ticket import Ticket, TicketStatus
from app.redis_client import get_redis

logger = logging.getLogger(__name__)

router = APIRouter(tags=["platform"])


# ---------------------------------------------------------------------------
# Inline Pydantic schemas
# ---------------------------------------------------------------------------


class TenantSummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str
    slug: str
    iam_org_id: str
    is_active: bool
    deleted_at: datetime | None
    created_at: datetime
    updated_at: datetime


class TenantDetailResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str
    slug: str
    iam_org_id: str
    is_active: bool
    settings: dict[str, Any]
    ai_budget_monthly_tokens: int | None
    deleted_at: datetime | None
    created_at: datetime
    updated_at: datetime
    products: list[dict[str, Any]] = []


class PatchTenantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str | None = Field(
        default=None,
        description="'active' sets is_active=True; 'suspended' sets is_active=False",
    )
    plan: str | None = Field(default=None, description="Arbitrary plan label stored in settings.plan")


class PlatformAuditLogItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    actor_id: str
    actor_email: str
    platform_role: str
    tenant_id: UUID | None
    http_method: str
    endpoint: str
    action: str | None
    details: dict | None
    accessed_at: datetime


class PlatformAuditLogPageResponse(BaseModel):
    items: list[PlatformAuditLogItemResponse]
    total: int
    page: int
    page_size: int


class MonthlyAIUsageResponse(BaseModel):
    month: str
    used_tokens: int


class AIUsageMonthsResponse(BaseModel):
    tenant_id: str
    months: list[MonthlyAIUsageResponse]


class SystemHealthResponse(BaseModel):
    db: str
    redis: str


class TenantAIUsageSummary(BaseModel):
    tenant_id: str
    used_tokens: int


class AIUsageOverviewResponse(BaseModel):
    month: str
    top_tenants: list[TenantAIUsageSummary]


class TenantsPageResponse(BaseModel):
    items: list[TenantSummaryResponse]
    total: int
    page: int
    page_size: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _get_tenant_or_404(db: AsyncSession, tenant_id: UUID) -> Tenant:
    """Fetch the Tenant row by PK; raise ResourceNotFoundError if absent."""
    result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    tenant = result.scalar_one_or_none()
    if tenant is None:
        raise ResourceNotFoundError("tenant", str(tenant_id))
    return tenant


def _write_audit_log(
    db: AsyncSession,
    *,
    current_user: CurrentUser,
    tenant_id: UUID,
    action: str,
    details: dict,
    http_method: str,
    endpoint: str,
) -> None:
    """Append one PlatformAuditLog row. INSERT only — never update this table."""
    entry = PlatformAuditLog(
        actor_id=current_user.iam_user_id,
        actor_email=current_user.email,
        platform_role=current_user.platform_role or "",
        tenant_id=tenant_id,
        http_method=http_method,
        endpoint=endpoint,
        request_id="",
        ip_address="",
        action=action,
        details=details,
    )
    db.add(entry)


# ---------------------------------------------------------------------------
# GET /platform/tenants
# ---------------------------------------------------------------------------


@router.get("/tenants", response_model=TenantsPageResponse)
async def list_tenants(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=200),
    status: str = Query(default="all", description="active | suspended | all"),
    current_user: CurrentUser = Depends(require_platform_support()),
    db: AsyncSession = Depends(get_db),
) -> TenantsPageResponse:
    """List all tenants with optional status filter. Requires platform_support."""
    filters = [Tenant.deleted_at.is_(None)]
    if status == "active":
        filters.append(Tenant.is_active.is_(True))
    elif status == "suspended":
        filters.append(Tenant.is_active.is_(False))

    count_result = await db.execute(
        select(func.count()).select_from(Tenant).where(and_(*filters))
    )
    total = count_result.scalar_one()

    offset = (page - 1) * page_size
    rows_result = await db.execute(
        select(Tenant)
        .where(and_(*filters))
        .order_by(Tenant.created_at.desc())
        .offset(offset)
        .limit(page_size)
    )
    items = [TenantSummaryResponse.model_validate(t) for t in rows_result.scalars().all()]

    return TenantsPageResponse(items=items, total=total, page=page, page_size=page_size)


# ---------------------------------------------------------------------------
# GET /platform/tenants/{tenant_id}
# ---------------------------------------------------------------------------


@router.get("/tenants/{tenant_id}", response_model=TenantDetailResponse)
async def get_tenant(
    tenant_id: UUID,
    current_user: CurrentUser = Depends(require_platform_support()),
    db: AsyncSession = Depends(get_db),
) -> TenantDetailResponse:
    """Get tenant detail. Requires platform_support."""
    from sqlalchemy.orm import selectinload

    result = await db.execute(
        select(Tenant)
        .options(selectinload(Tenant.products))
        .where(Tenant.id == tenant_id)
    )
    tenant = result.scalar_one_or_none()
    if tenant is None:
        raise ResourceNotFoundError("tenant", str(tenant_id))

    products = [
        {"id": str(p.id), "slug": p.slug, "name": p.name, "is_active": p.is_active}
        for p in tenant.products
    ]

    return TenantDetailResponse(
        id=tenant.id,
        name=tenant.name,
        slug=tenant.slug,
        iam_org_id=tenant.iam_org_id,
        is_active=tenant.is_active,
        settings=tenant.settings,
        ai_budget_monthly_tokens=tenant.ai_budget_monthly_tokens,
        deleted_at=tenant.deleted_at,
        created_at=tenant.created_at,
        updated_at=tenant.updated_at,
        products=products,
    )


# ---------------------------------------------------------------------------
# PATCH /platform/tenants/{tenant_id}
# ---------------------------------------------------------------------------


@router.patch("/tenants/{tenant_id}", response_model=TenantDetailResponse)
async def patch_tenant(
    tenant_id: UUID,
    body: PatchTenantRequest,
    current_user: CurrentUser = Depends(require_platform_admin()),
    db: AsyncSession = Depends(get_db),
) -> TenantDetailResponse:
    """Update tenant status or plan. Requires platform_admin. Writes audit log."""
    tenant = await _get_tenant_or_404(db, tenant_id)

    changed: dict[str, Any] = {}

    if body.status is not None:
        if body.status == "active":
            if not tenant.is_active:
                tenant.is_active = True
                changed["is_active"] = True
        elif body.status == "suspended":
            if tenant.is_active:
                tenant.is_active = False
                changed["is_active"] = False
        else:
            raise AuthorizationError(f"Unknown status value '{body.status}'; use 'active' or 'suspended'")

    if body.plan is not None:
        current_plan = tenant.settings.get("plan")
        if current_plan != body.plan:
            tenant.settings = {**tenant.settings, "plan": body.plan}
            changed["plan"] = body.plan

    if changed:
        db.add(tenant)
        _write_audit_log(
            db,
            current_user=current_user,
            tenant_id=tenant_id,
            action="platform.tenant.updated",
            details={"changed_fields": changed},
            http_method="PATCH",
            endpoint=f"/platform/tenants/{tenant_id}",
        )
        await db.commit()
        await db.refresh(tenant)

    return TenantDetailResponse(
        id=tenant.id,
        name=tenant.name,
        slug=tenant.slug,
        iam_org_id=tenant.iam_org_id,
        is_active=tenant.is_active,
        settings=tenant.settings,
        ai_budget_monthly_tokens=tenant.ai_budget_monthly_tokens,
        deleted_at=tenant.deleted_at,
        created_at=tenant.created_at,
        updated_at=tenant.updated_at,
        products=[],
    )


# ---------------------------------------------------------------------------
# DELETE /platform/tenants/{tenant_id}
# ---------------------------------------------------------------------------


@router.delete("/tenants/{tenant_id}", status_code=204)
async def delete_tenant(
    tenant_id: UUID,
    current_user: CurrentUser = Depends(require_platform_admin()),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Soft-delete a tenant (sets deleted_at). Refuses with 409 if open tickets exist. Requires platform_admin."""
    from fastapi import HTTPException

    tenant = await _get_tenant_or_404(db, tenant_id)

    if tenant.deleted_at is not None:
        # Already deleted — idempotent no-op
        return

    # Check for open / in-progress tickets — refuse if any exist
    open_count_result = await db.execute(
        select(func.count())
        .select_from(Ticket)
        .where(
            Ticket.tenant_id == tenant_id,
            Ticket.status.in_([TicketStatus.open, TicketStatus.in_progress]),
        )
    )
    open_count = open_count_result.scalar_one()
    if open_count > 0:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "TENANT_HAS_OPEN_TICKETS",
                "message": f"Tenant has {open_count} open/in-progress ticket(s). Resolve or close them before deleting the tenant.",
            },
        )

    # Perform soft delete using func.now() — never Python datetime
    tenant.deleted_at = func.now()  # type: ignore[assignment]
    tenant.is_active = False
    db.add(tenant)

    _write_audit_log(
        db,
        current_user=current_user,
        tenant_id=tenant_id,
        action="platform.tenant.deleted",
        details={"soft_delete": True, "tenant_name": tenant.name},
        http_method="DELETE",
        endpoint=f"/platform/tenants/{tenant_id}",
    )
    await db.commit()


# ---------------------------------------------------------------------------
# GET /platform/tenants/{tenant_id}/audit-log
# ---------------------------------------------------------------------------


@router.get("/tenants/{tenant_id}/audit-log", response_model=PlatformAuditLogPageResponse)
async def get_tenant_audit_log(
    tenant_id: UUID,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    start_date: str | None = Query(default=None, description="ISO date YYYY-MM-DD"),
    end_date: str | None = Query(default=None, description="ISO date YYYY-MM-DD"),
    current_user: CurrentUser = Depends(require_platform_support()),
    db: AsyncSession = Depends(get_db),
) -> PlatformAuditLogPageResponse:
    """Full audit log for a specific tenant. Requires platform_support."""
    from datetime import date as date_type

    # Validate tenant exists
    await _get_tenant_or_404(db, tenant_id)

    filters = [PlatformAuditLog.tenant_id == tenant_id]
    if start_date:
        try:
            filters.append(func.date(PlatformAuditLog.accessed_at) >= date_type.fromisoformat(start_date))
        except ValueError:
            pass
    if end_date:
        try:
            filters.append(func.date(PlatformAuditLog.accessed_at) <= date_type.fromisoformat(end_date))
        except ValueError:
            pass

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
    items = [PlatformAuditLogItemResponse.model_validate(row) for row in rows_result.scalars().all()]

    return PlatformAuditLogPageResponse(items=items, total=total, page=page, page_size=page_size)


# ---------------------------------------------------------------------------
# GET /platform/tenants/{tenant_id}/ai-usage
# ---------------------------------------------------------------------------


@router.get("/tenants/{tenant_id}/ai-usage", response_model=AIUsageMonthsResponse)
async def get_tenant_ai_usage(
    tenant_id: UUID,
    current_user: CurrentUser = Depends(require_platform_support()),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> AIUsageMonthsResponse:
    """All-months AI usage for a tenant from Redis. Requires platform_support."""
    # Validate tenant exists
    await _get_tenant_or_404(db, tenant_id)

    pattern = f"ai_budget:{tenant_id}:*"
    keys = await redis.keys(pattern)

    months: list[MonthlyAIUsageResponse] = []
    if keys:
        values = await redis.mget(*keys)
        for key, raw_value in zip(keys, values):
            # key format: ai_budget:{tenant_id}:{YYYY-MM}
            parts = key.split(":")
            month_label = parts[-1] if len(parts) >= 3 else "unknown"
            used = int(raw_value or 0)
            months.append(MonthlyAIUsageResponse(month=month_label, used_tokens=used))

    months.sort(key=lambda m: m.month, reverse=True)

    return AIUsageMonthsResponse(tenant_id=str(tenant_id), months=months)


# ---------------------------------------------------------------------------
# GET /platform/system-health
# ---------------------------------------------------------------------------


@router.get("/system-health", response_model=SystemHealthResponse)
async def system_health(
    current_user: CurrentUser = Depends(require_platform_support()),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> SystemHealthResponse:
    """DB + Redis liveness check. Requires platform_support."""
    db_status = "ok"
    redis_status = "ok"

    try:
        await db.execute(text("SELECT 1"))
    except Exception:
        db_status = "error"

    try:
        await redis.ping()
    except Exception:
        redis_status = "error"

    return SystemHealthResponse(db=db_status, redis=redis_status)


# ---------------------------------------------------------------------------
# GET /platform/ai-usage-overview
# ---------------------------------------------------------------------------


@router.get("/ai-usage-overview", response_model=AIUsageOverviewResponse)
async def ai_usage_overview(
    current_user: CurrentUser = Depends(require_platform_support()),
    redis: Redis = Depends(get_redis),
) -> AIUsageOverviewResponse:
    """Top-10 tenants by current-month token usage from Redis. Requires platform_support."""
    month = datetime.now().strftime("%Y-%m")
    pattern = f"ai_budget:*:{month}"
    keys = await redis.keys(pattern)

    tenant_usage: list[TenantAIUsageSummary] = []
    if keys:
        values = await redis.mget(*keys)
        for key, raw_value in zip(keys, values):
            # key format: ai_budget:{tenant_id}:{YYYY-MM}
            parts = key.split(":")
            if len(parts) >= 3:
                tid = parts[1]
                used = int(raw_value or 0)
                tenant_usage.append(TenantAIUsageSummary(tenant_id=tid, used_tokens=used))

    tenant_usage.sort(key=lambda t: t.used_tokens, reverse=True)
    top10 = tenant_usage[:10]

    return AIUsageOverviewResponse(month=month, top_tenants=top10)
