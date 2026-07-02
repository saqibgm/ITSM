"""SLO reliability workers (Phase 9).

- sample_slo_measurements: every 5 min, write one measurement bucket per active
  SLO from its SLI source (internal metric = free win; http_uptime_check probes;
  external query sources are no-ops until a backend is wired).
- evaluate_slo_burn: every 5 min, roll up windows + evaluate burn-rate rules,
  firing through Module B (alerting_service).
Both run on DB/UTC now; additive to the SLA/alerting workers.
"""

import logging

from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="app.workers.tasks_slo.sample_slo_measurements",
                 bind=True, max_retries=3, default_retry_delay=30)
def sample_slo_measurements(self) -> None:
    import asyncio
    try:
        asyncio.run(_async_sample())
    except Exception as exc:
        logger.error("slo_sample_failed", extra={"error": str(exc)})
        raise self.retry(exc=exc)


async def _async_sample() -> None:
    from datetime import datetime, timezone
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker

    from app.config import get_settings
    from app.models.slo import SLISource, SLOObjective
    from app.services import slo_service

    settings = get_settings()
    engine = create_async_engine(settings.DATABASE_URL, pool_size=3, max_overflow=2)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    now = datetime.now(timezone.utc)
    bucket = slo_service._floor_bucket(now)
    written = 0
    try:
        async with async_session() as db:
            slos = (await db.execute(select(SLOObjective).where(SLOObjective.is_active.is_(True)))).scalars().all()
            for slo in slos:
                src = (await db.execute(select(SLISource).where(SLISource.id == slo.sli_source_id))).scalar_one_or_none()
                if src is None or not src.is_active:
                    continue
                good = total = None
                if src.type == "internal_metric":
                    good, total = await slo_service.sample_internal_sli(db, src, slo.service_id)
                elif src.type == "http_uptime_check":
                    good, total = await _probe_http((src.config or {}).get("url"))
                # push_api + external query sources: ingested elsewhere / no backend → skip
                if total is None:
                    continue
                await slo_service.record_measurement(db, slo.id, bucket, good, total)
                written += 1
            await db.commit()
    finally:
        await engine.dispose()
    if written:
        logger.info("slo_measurements_sampled", extra={"count": written})


async def _probe_http(url):
    """Best-effort uptime probe. Returns (good, total) or (None, None) on no-url."""
    if not url:
        return (None, None)
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(url)
            return (1, 1) if r.status_code < 500 else (0, 1)
    except Exception:
        return (0, 1)


@celery_app.task(name="app.workers.tasks_slo.evaluate_slo_burn",
                 bind=True, max_retries=3, default_retry_delay=30)
def evaluate_slo_burn(self) -> None:
    import asyncio
    try:
        asyncio.run(_async_evaluate())
    except Exception as exc:
        logger.error("slo_burn_eval_failed", extra={"error": str(exc)})
        raise self.retry(exc=exc)


async def _async_evaluate() -> None:
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker

    from app.config import get_settings
    from app.models.slo import SLOObjective
    from app.services import slo_service

    settings = get_settings()
    engine = create_async_engine(settings.DATABASE_URL, pool_size=3, max_overflow=2)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    fired = 0
    try:
        async with async_session() as db:
            slos = (await db.execute(select(SLOObjective).where(SLOObjective.is_active.is_(True)))).scalars().all()
            for slo in slos:
                fired += len(await slo_service.evaluate_burn_alerts(db, slo))
            await db.commit()
    finally:
        await engine.dispose()
    if fired:
        logger.info("slo_burn_alerts_fired", extra={"count": fired})
