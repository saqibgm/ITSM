"""
Reporting & Analytics API — S6C.

All endpoints:
  - Require agent role or higher (agent, team_lead, manager, admin).
  - Filter exclusively by the tenant_id from the JWT — no cross-tenant access.
  - Are read-only (GET only).

Prefix: /reports
Tag:    reports
"""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentUser, require_role
from app.database import get_db
from app.exceptions import AuthorizationError
from app.schemas.reports import (
    AIUsageSummaryResponse,
    AssetSummaryResponse,
    KBTopArticlesResponse,
    KBTopArticleItem,
    SLAByPriorityItem,
    SLAComplianceResponse,
    TeamWorkloadItem,
    TeamWorkloadResponse,
    TicketSummaryResponse,
    VolumeTrendPoint,
    VolumeTrendResponse,
)
from app.services.reports_service import ReportsService

router = APIRouter(prefix="/reports", tags=["reports"])

_service = ReportsService()

# Roles that may access reports
_REPORT_ROLES = ("agent", "team_lead", "manager", "admin")

_AgentOrAbove = Depends(require_role(*_REPORT_ROLES))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_date(value: str | None):
    """Parse an ISO date string to a date object, or return None."""
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _require_tenant(current_user: CurrentUser) -> None:
    if current_user.tenant_id is None:
        raise AuthorizationError("Tenant context required for reports")


# ---------------------------------------------------------------------------
# GET /reports/tickets/summary
# ---------------------------------------------------------------------------


@router.get(
    "/tickets/summary",
    response_model=TicketSummaryResponse,
    summary="Ticket count grouped by status, priority, and type",
)
async def ticket_summary(
    start_date: str | None = Query(default=None, description="ISO date YYYY-MM-DD"),
    end_date: str | None = Query(default=None, description="ISO date YYYY-MM-DD"),
    current_user: CurrentUser = _AgentOrAbove,
    db: AsyncSession = Depends(get_db),
) -> TicketSummaryResponse:
    _require_tenant(current_user)
    data = await _service.ticket_summary(
        db,
        tenant_id=current_user.tenant_id,
        start_date=_parse_date(start_date),
        end_date=_parse_date(end_date),
    )
    return TicketSummaryResponse(**data)


# ---------------------------------------------------------------------------
# GET /reports/tickets/sla-compliance
# ---------------------------------------------------------------------------


@router.get(
    "/tickets/sla-compliance",
    response_model=SLAComplianceResponse,
    summary="SLA compliance statistics for the date window",
)
async def sla_compliance(
    start_date: str | None = Query(default=None, description="ISO date YYYY-MM-DD"),
    end_date: str | None = Query(default=None, description="ISO date YYYY-MM-DD"),
    current_user: CurrentUser = _AgentOrAbove,
    db: AsyncSession = Depends(get_db),
) -> SLAComplianceResponse:
    _require_tenant(current_user)
    data = await _service.sla_compliance(
        db,
        tenant_id=current_user.tenant_id,
        start_date=_parse_date(start_date),
        end_date=_parse_date(end_date),
    )
    # Coerce nested by_priority dicts into SLAByPriorityItem
    by_priority = {
        k: SLAByPriorityItem(**v) for k, v in data.get("by_priority", {}).items()
    }
    return SLAComplianceResponse(
        total_tickets=data["total_tickets"],
        within_sla=data["within_sla"],
        breached=data["breached"],
        compliance_pct=data["compliance_pct"],
        avg_resolution_hours=data["avg_resolution_hours"],
        by_priority=by_priority,
    )


# ---------------------------------------------------------------------------
# GET /reports/tickets/volume-trend
# ---------------------------------------------------------------------------


@router.get(
    "/tickets/volume-trend",
    response_model=VolumeTrendResponse,
    summary="Ticket creation count grouped by time bucket",
)
async def ticket_volume_trend(
    start_date: str | None = Query(default=None, description="ISO date YYYY-MM-DD"),
    end_date: str | None = Query(default=None, description="ISO date YYYY-MM-DD"),
    granularity: str = Query(default="day", pattern="^(day|week|month)$"),
    current_user: CurrentUser = _AgentOrAbove,
    db: AsyncSession = Depends(get_db),
) -> VolumeTrendResponse:
    _require_tenant(current_user)

    from app.services.reports_service import _default_range, _to_utc_datetime

    start_parsed = _parse_date(start_date)
    end_parsed = _parse_date(end_date)
    start_utc, end_utc = _default_range(start_parsed, end_parsed)

    points_raw = await _service.ticket_volume_trend(
        db,
        tenant_id=current_user.tenant_id,
        start_date=start_parsed,
        end_date=end_parsed,
        granularity=granularity,
    )
    return VolumeTrendResponse(
        granularity=granularity,
        start_date=start_utc.isoformat(),
        end_date=end_utc.isoformat(),
        points=[VolumeTrendPoint(**p) for p in points_raw],
    )


# ---------------------------------------------------------------------------
# GET /reports/assets/summary
# ---------------------------------------------------------------------------


@router.get(
    "/assets/summary",
    response_model=AssetSummaryResponse,
    summary="Current asset count grouped by status and asset type",
)
async def asset_summary(
    current_user: CurrentUser = _AgentOrAbove,
    db: AsyncSession = Depends(get_db),
) -> AssetSummaryResponse:
    _require_tenant(current_user)
    data = await _service.asset_summary(db, tenant_id=current_user.tenant_id)
    return AssetSummaryResponse(**data)


# ---------------------------------------------------------------------------
# GET /reports/ai/usage
# ---------------------------------------------------------------------------


@router.get(
    "/ai/usage",
    response_model=AIUsageSummaryResponse,
    summary="AI token usage and call counts grouped by feature and model",
)
async def ai_usage(
    start_date: str | None = Query(default=None, description="ISO date YYYY-MM-DD"),
    end_date: str | None = Query(default=None, description="ISO date YYYY-MM-DD"),
    current_user: CurrentUser = _AgentOrAbove,
    db: AsyncSession = Depends(get_db),
) -> AIUsageSummaryResponse:
    _require_tenant(current_user)
    data = await _service.ai_usage_summary(
        db,
        tenant_id=current_user.tenant_id,
        start_date=_parse_date(start_date),
        end_date=_parse_date(end_date),
    )
    return AIUsageSummaryResponse(**data)


# ---------------------------------------------------------------------------
# GET /reports/kb/top-articles
# ---------------------------------------------------------------------------


@router.get(
    "/kb/top-articles",
    response_model=KBTopArticlesResponse,
    summary="Top KB articles by view count",
)
async def kb_top_articles(
    limit: int = Query(default=20, ge=1, le=100, description="Max articles to return"),
    current_user: CurrentUser = _AgentOrAbove,
    db: AsyncSession = Depends(get_db),
) -> KBTopArticlesResponse:
    _require_tenant(current_user)
    articles_raw = await _service.kb_top_articles(
        db, tenant_id=current_user.tenant_id, limit=limit
    )
    return KBTopArticlesResponse(
        total=len(articles_raw),
        articles=[KBTopArticleItem(**a) for a in articles_raw],
    )


# ---------------------------------------------------------------------------
# GET /reports/teams/workload
# ---------------------------------------------------------------------------


@router.get(
    "/teams/workload",
    response_model=TeamWorkloadResponse,
    summary="Open ticket count and average age per team",
)
async def team_workload(
    start_date: str | None = Query(default=None, description="ISO date YYYY-MM-DD"),
    end_date: str | None = Query(default=None, description="ISO date YYYY-MM-DD"),
    current_user: CurrentUser = _AgentOrAbove,
    db: AsyncSession = Depends(get_db),
) -> TeamWorkloadResponse:
    _require_tenant(current_user)

    from app.services.reports_service import _default_range

    start_parsed = _parse_date(start_date)
    end_parsed = _parse_date(end_date)
    start_utc, end_utc = _default_range(start_parsed, end_parsed)

    teams_raw = await _service.team_workload(
        db,
        tenant_id=current_user.tenant_id,
        start_date=start_parsed,
        end_date=end_parsed,
    )
    return TeamWorkloadResponse(
        start_date=start_utc.isoformat(),
        end_date=end_utc.isoformat(),
        teams=[TeamWorkloadItem(**t) for t in teams_raw],
    )
