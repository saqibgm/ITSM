"""SLA daily-metrics rollup (Phase 7 / S7.3).

Aggregates sla_instances into per-tenant ``sla_metrics_daily`` rows for a given
date. Kept as a standalone async helper so both the nightly Celery task and the
tests can call it with a session. Runs as the platform worker (RLS fail-open
when no tenant GUC is set) to roll up all tenants in one pass.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_ROLLUP_SQL = text(
    """
    INSERT INTO sla_metrics_daily
        (id, tenant_id, date, dimension, opened_count, met_count, breached_count, total_count)
    SELECT gen_random_uuid(), tenant_id, :d, '{}'::jsonb,
           count(*) FILTER (WHERE created_at::date = :d),
           count(*) FILTER (WHERE status = 'met' AND updated_at::date = :d),
           count(*) FILTER (WHERE breached_at::date = :d),
           count(*) FILTER (WHERE created_at::date = :d)
    FROM sla_instances
    WHERE created_at::date = :d
       OR breached_at::date = :d
       OR (status = 'met' AND updated_at::date = :d)
    GROUP BY tenant_id
    ON CONFLICT (tenant_id, date, dimension) DO UPDATE SET
        opened_count = EXCLUDED.opened_count,
        met_count = EXCLUDED.met_count,
        breached_count = EXCLUDED.breached_count,
        total_count = EXCLUDED.total_count
    """
)


async def compute_sla_metrics_for_date(db: AsyncSession, the_date: date) -> None:
    """Upsert the per-tenant sla_metrics_daily rows for ``the_date``."""
    await db.execute(_ROLLUP_SQL, {"d": the_date})
