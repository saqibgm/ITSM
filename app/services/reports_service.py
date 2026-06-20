"""
Reports & Analytics service — S6C.

Pure read-only aggregate queries. All methods accept (db, tenant_id, ...) and
filter strictly by tenant_id so no cross-tenant data leakage is possible.

No f-strings in SQL.  text() bindings use :named params only.
Division-by-zero is guarded at every percentage calculation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Union
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai_usage import AIUsageDaily
from app.models.asset import Asset, AssetType
from app.models.identity import Team
from app.models.kb import KBArticle
from app.models.ticket import Ticket, TicketStatus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_RESOLVED_STATUSES = {TicketStatus.resolved, TicketStatus.closed}
_OPEN_STATUSES = {
    TicketStatus.open,
    TicketStatus.in_progress,
    TicketStatus.pending,
    TicketStatus.pending_approval,
    TicketStatus.approved,
    TicketStatus.rejected,
    TicketStatus.cancelled,
}

DateLike = Union[datetime, None]


def _to_utc_datetime(value) -> datetime:
    """Convert a date, datetime, or None to a UTC-aware datetime."""
    if value is None:
        return datetime.now(tz=timezone.utc)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    # plain date object
    return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)


def _default_range(start_date, end_date):
    """Return (start_utc, end_utc) applying defaults when None."""
    end_utc = _to_utc_datetime(end_date)
    start_utc = _to_utc_datetime(start_date) if start_date is not None else (
        end_utc - timedelta(days=30)
    )
    return start_utc, end_utc


# ---------------------------------------------------------------------------
# ReportsService
# ---------------------------------------------------------------------------


class ReportsService:

    # ------------------------------------------------------------------
    # 1. Ticket Summary
    # ------------------------------------------------------------------

    async def ticket_summary(
        self,
        db: AsyncSession,
        tenant_id: UUID,
        start_date=None,
        end_date=None,
    ) -> dict:
        """Count tickets grouped by status, priority, and type for the date range."""
        start_utc, end_utc = _default_range(start_date, end_date)

        base_filter = sa.and_(
            Ticket.tenant_id == tenant_id,
            Ticket.created_at >= start_utc,
            Ticket.created_at <= end_utc,
        )

        # --- by_status ---
        stmt_status = (
            sa.select(Ticket.status, sa.func.count().label("cnt"))
            .where(base_filter)
            .group_by(Ticket.status)
        )
        rows_status = (await db.execute(stmt_status)).all()
        by_status = {str(r.status.value if hasattr(r.status, "value") else r.status): r.cnt
                     for r in rows_status}

        # --- by_priority ---
        stmt_priority = (
            sa.select(Ticket.priority, sa.func.count().label("cnt"))
            .where(base_filter)
            .group_by(Ticket.priority)
        )
        rows_priority = (await db.execute(stmt_priority)).all()
        by_priority = {str(r.priority.value if hasattr(r.priority, "value") else r.priority): r.cnt
                       for r in rows_priority}

        # --- by_type ---
        stmt_type = (
            sa.select(Ticket.type, sa.func.count().label("cnt"))
            .where(base_filter)
            .group_by(Ticket.type)
        )
        rows_type = (await db.execute(stmt_type)).all()
        by_type = {str(r.type.value if hasattr(r.type, "value") else r.type): r.cnt
                   for r in rows_type}

        total = sum(by_status.values())

        return {
            "total": total,
            "start_date": start_utc.isoformat(),
            "end_date": end_utc.isoformat(),
            "by_status": by_status,
            "by_priority": by_priority,
            "by_type": by_type,
        }

    # ------------------------------------------------------------------
    # 2. SLA Compliance
    # ------------------------------------------------------------------

    async def sla_compliance(
        self,
        db: AsyncSession,
        tenant_id: UUID,
        start_date=None,
        end_date=None,
    ) -> dict:
        """
        SLA compliance for tickets created in the date window.

        A ticket breached SLA if:
          sla_resolve_due IS NOT NULL AND (
            (status NOT IN (resolved, closed) AND sla_resolve_due < now())
            OR (resolved_at IS NOT NULL AND resolved_at > sla_resolve_due)
          )
        """
        start_utc, end_utc = _default_range(start_date, end_date)
        now = datetime.now(tz=timezone.utc)

        base_filter = sa.and_(
            Ticket.tenant_id == tenant_id,
            Ticket.created_at >= start_utc,
            Ticket.created_at <= end_utc,
        )

        # Breached predicate (uses sla_resolve_due, not sla_due_at)
        breached_cond = sa.and_(
            Ticket.sla_resolve_due.is_not(None),
            sa.or_(
                sa.and_(
                    Ticket.status.not_in([TicketStatus.resolved, TicketStatus.closed]),
                    Ticket.sla_resolve_due < now,
                ),
                sa.and_(
                    Ticket.resolved_at.is_not(None),
                    Ticket.resolved_at > Ticket.sla_resolve_due,
                ),
            ),
        )

        # Total
        total_res = (await db.execute(
            sa.select(sa.func.count()).where(base_filter)
        )).scalar_one()

        # Breached
        breached_res = (await db.execute(
            sa.select(sa.func.count()).where(sa.and_(base_filter, breached_cond))
        )).scalar_one()

        total: int = total_res or 0
        breached: int = breached_res or 0
        within_sla: int = total - breached
        compliance_pct: float = (within_sla / total * 100.0) if total > 0 else 0.0

        # avg resolution hours (resolved tickets only)
        stmt_avg = sa.select(
            sa.func.avg(
                sa.cast(
                    sa.func.extract(
                        "epoch",
                        Ticket.resolved_at - Ticket.created_at,
                    ),
                    sa.Float,
                )
            )
        ).where(
            sa.and_(
                base_filter,
                Ticket.resolved_at.is_not(None),
            )
        )
        avg_seconds = (await db.execute(stmt_avg)).scalar_one()
        avg_resolution_hours: float = float(avg_seconds / 3600.0) if avg_seconds else 0.0

        # by_priority breakdown
        stmt_bp = sa.select(
            Ticket.priority,
            sa.func.count().label("total"),
            sa.func.count(
                sa.case((breached_cond, 1))
            ).label("breached_cnt"),
        ).where(base_filter).group_by(Ticket.priority)

        rows_bp = (await db.execute(stmt_bp)).all()
        by_priority: dict = {}
        for row in rows_bp:
            pkey = str(row.priority.value if hasattr(row.priority, "value") else row.priority)
            t = row.total or 0
            b = row.breached_cnt or 0
            by_priority[pkey] = {
                "total": t,
                "within_sla": t - b,
                "breached": b,
            }

        return {
            "total_tickets": total,
            "within_sla": within_sla,
            "breached": breached,
            "compliance_pct": round(compliance_pct, 2),
            "avg_resolution_hours": round(avg_resolution_hours, 2),
            "by_priority": by_priority,
        }

    # ------------------------------------------------------------------
    # 3. Ticket Volume Trend
    # ------------------------------------------------------------------

    async def ticket_volume_trend(
        self,
        db: AsyncSession,
        tenant_id: UUID,
        start_date=None,
        end_date=None,
        granularity: str = "day",
    ) -> list[dict]:
        """
        Ticket count grouped by date_trunc(granularity, created_at).
        granularity: 'day' | 'week' | 'month'
        """
        start_utc, end_utc = _default_range(start_date, end_date)

        # Whitelist granularity to prevent any injection risk
        if granularity not in {"day", "week", "month"}:
            granularity = "day"

        bucket = sa.func.date_trunc(granularity, Ticket.created_at).label("bucket")

        stmt = (
            sa.select(bucket, sa.func.count().label("cnt"))
            .where(
                sa.and_(
                    Ticket.tenant_id == tenant_id,
                    Ticket.created_at >= start_utc,
                    Ticket.created_at <= end_utc,
                )
            )
            .group_by(bucket)
            .order_by(bucket)
        )

        rows = (await db.execute(stmt)).all()
        return [
            {
                "date": row.bucket.isoformat() if row.bucket else None,
                "count": row.cnt,
            }
            for row in rows
        ]

    # ------------------------------------------------------------------
    # 4. Asset Summary
    # ------------------------------------------------------------------

    async def asset_summary(
        self,
        db: AsyncSession,
        tenant_id: UUID,
    ) -> dict:
        """Count assets (non-deleted) grouped by status and asset_type name."""
        base_filter = sa.and_(
            Asset.tenant_id == tenant_id,
            Asset.deleted_at.is_(None),
        )

        # by_status
        stmt_status = (
            sa.select(Asset.status, sa.func.count().label("cnt"))
            .where(base_filter)
            .group_by(Asset.status)
        )
        rows_status = (await db.execute(stmt_status)).all()
        by_status = {
            str(r.status.value if hasattr(r.status, "value") else r.status): r.cnt
            for r in rows_status
        }

        # by_type (JOIN asset_types)
        stmt_type = (
            sa.select(AssetType.name, sa.func.count(Asset.id).label("cnt"))
            .join(AssetType, Asset.type_id == AssetType.id)
            .where(base_filter)
            .group_by(AssetType.name)
            .order_by(sa.func.count(Asset.id).desc())
        )
        rows_type = (await db.execute(stmt_type)).all()
        by_type = {r.name: r.cnt for r in rows_type}

        total = sum(by_status.values())

        return {
            "total": total,
            "by_status": by_status,
            "by_type": by_type,
        }

    # ------------------------------------------------------------------
    # 5. AI Usage Summary
    # ------------------------------------------------------------------

    async def ai_usage_summary(
        self,
        db: AsyncSession,
        tenant_id: UUID,
        start_date=None,
        end_date=None,
    ) -> dict:
        """
        Aggregate input_tokens, output_tokens, call_count from ai_usage_daily
        grouped by ai_feature and model.
        """
        start_utc, end_utc = _default_range(start_date, end_date)
        start_date_only = start_utc.date()
        end_date_only = end_utc.date()

        base_filter = sa.and_(
            AIUsageDaily.tenant_id == tenant_id,
            AIUsageDaily.date >= start_date_only,
            AIUsageDaily.date <= end_date_only,
        )

        stmt = (
            sa.select(
                AIUsageDaily.ai_feature,
                AIUsageDaily.model,
                sa.func.sum(AIUsageDaily.input_tokens).label("input_tokens"),
                sa.func.sum(AIUsageDaily.output_tokens).label("output_tokens"),
                sa.func.sum(AIUsageDaily.call_count).label("call_count"),
            )
            .where(base_filter)
            .group_by(AIUsageDaily.ai_feature, AIUsageDaily.model)
            .order_by(AIUsageDaily.ai_feature, AIUsageDaily.model)
        )

        rows = (await db.execute(stmt)).all()

        total_tokens = 0
        total_calls = 0
        by_feature: dict = {}

        for row in rows:
            inp = int(row.input_tokens or 0)
            out = int(row.output_tokens or 0)
            calls = int(row.call_count or 0)
            total_tokens += inp + out
            total_calls += calls

            feat = row.ai_feature
            if feat not in by_feature:
                by_feature[feat] = {
                    "total_input_tokens": 0,
                    "total_output_tokens": 0,
                    "total_calls": 0,
                    "by_model": {},
                }
            by_feature[feat]["total_input_tokens"] += inp
            by_feature[feat]["total_output_tokens"] += out
            by_feature[feat]["total_calls"] += calls
            by_feature[feat]["by_model"][row.model] = {
                "input_tokens": inp,
                "output_tokens": out,
                "call_count": calls,
            }

        return {
            "total_tokens": total_tokens,
            "total_calls": total_calls,
            "by_feature": by_feature,
        }

    # ------------------------------------------------------------------
    # 6. KB Top Articles
    # ------------------------------------------------------------------

    async def kb_top_articles(
        self,
        db: AsyncSession,
        tenant_id: UUID,
        limit: int = 20,
    ) -> list[dict]:
        """Top KB articles by view_count DESC."""
        stmt = (
            sa.select(
                KBArticle.id,
                KBArticle.title,
                KBArticle.view_count,
                KBArticle.helpful_count,
                KBArticle.not_helpful_count,
            )
            .where(KBArticle.tenant_id == tenant_id)
            .order_by(KBArticle.view_count.desc())
            .limit(limit)
        )

        rows = (await db.execute(stmt)).all()
        result = []
        for row in rows:
            total_fb = (row.helpful_count or 0) + (row.not_helpful_count or 0)
            helpful_pct = (
                round((row.helpful_count or 0) / total_fb * 100.0, 2)
                if total_fb > 0
                else 0.0
            )
            result.append(
                {
                    "id": str(row.id),
                    "title": row.title,
                    "view_count": row.view_count or 0,
                    "helpful_count": row.helpful_count or 0,
                    "not_helpful_count": row.not_helpful_count or 0,
                    "helpful_pct": helpful_pct,
                }
            )
        return result

    # ------------------------------------------------------------------
    # 7. Team Workload
    # ------------------------------------------------------------------

    async def team_workload(
        self,
        db: AsyncSession,
        tenant_id: UUID,
        start_date=None,
        end_date=None,
    ) -> list[dict]:
        """
        Open ticket count per team with average age in hours.
        Filters by tickets created within the date range that are still open.
        """
        start_utc, end_utc = _default_range(start_date, end_date)
        now = datetime.now(tz=timezone.utc)

        open_status_values = [s.value for s in _OPEN_STATUSES]

        stmt = (
            sa.select(
                Team.id.label("team_id"),
                Team.name.label("team_name"),
                sa.func.count(Ticket.id).label("open_tickets"),
                sa.func.avg(
                    sa.cast(
                        sa.func.extract("epoch", sa.literal(now) - Ticket.created_at),
                        sa.Float,
                    )
                ).label("avg_age_seconds"),
            )
            .join(Ticket, sa.and_(
                Ticket.team_id == Team.id,
                Ticket.tenant_id == tenant_id,
                Ticket.created_at >= start_utc,
                Ticket.created_at <= end_utc,
                Ticket.status.in_(open_status_values),
            ))
            .where(Team.tenant_id == tenant_id)
            .group_by(Team.id, Team.name)
            .order_by(sa.func.count(Ticket.id).desc())
        )

        rows = (await db.execute(stmt)).all()
        return [
            {
                "team_id": str(row.team_id),
                "team_name": row.team_name,
                "open_tickets": row.open_tickets or 0,
                "avg_age_hours": round(float(row.avg_age_seconds or 0) / 3600.0, 2),
            }
            for row in rows
        ]
