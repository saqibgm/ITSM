"""RCA + recording compliance dashboards (specs/08 §5). Prefixless router."""

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentUser, require_role
from app.database import get_db
from app.exceptions import AuthorizationError
from app.services import rca_service

router = APIRouter(tags=["rca-dashboards"])

_READ = ("agent", "team_lead", "manager", "admin")


def _tenant(cu: CurrentUser) -> UUID:
    if cu.tenant_id is None:
        raise AuthorizationError("Tenant context required")
    return cu.tenant_id


@router.get("/dashboards/rca/summary")
async def rca_summary(cu: CurrentUser = Depends(require_role(*_READ)), db: AsyncSession = Depends(get_db)):
    return await rca_service.dashboard_summary(db, _tenant(cu))


@router.get("/dashboards/rca/pipeline")
async def rca_pipeline(cu: CurrentUser = Depends(require_role(*_READ)), db: AsyncSession = Depends(get_db)):
    return await rca_service.dashboard_pipeline(db, _tenant(cu))


@router.get("/dashboards/rca/overdue")
async def rca_overdue(cu: CurrentUser = Depends(require_role(*_READ)), db: AsyncSession = Depends(get_db)):
    return await rca_service.dashboard_overdue(db, _tenant(cu))


@router.get("/dashboards/rca/action-burndown")
async def rca_action_burndown(cu: CurrentUser = Depends(require_role(*_READ)), db: AsyncSession = Depends(get_db)):
    return await rca_service.dashboard_action_burndown(db, _tenant(cu))


@router.get("/dashboards/rca/evidence-completeness")
async def rca_evidence_completeness(cu: CurrentUser = Depends(require_role(*_READ)), db: AsyncSession = Depends(get_db)):
    return await rca_service.dashboard_evidence_completeness(db, _tenant(cu))


@router.get("/dashboards/rca/root-cause-trends")
async def rca_root_cause_trends(cu: CurrentUser = Depends(require_role(*_READ)), db: AsyncSession = Depends(get_db)):
    return await rca_service.dashboard_root_cause_trends(db, _tenant(cu))
