"""
Celery beat task: flush_daily_usage

Runs nightly (2 am UTC) to aggregate per-feature AI usage from Redis into
the ai_usage_daily table.

Redis key pattern (written by ai_service.record_usage):
  ai_usage:{tenant_id}:{YYYY-MM-DD}:{feature}:{model}:input   → input_tokens
  ai_usage:{tenant_id}:{YYYY-MM-DD}:{feature}:{model}:output  → output_tokens
  ai_usage:{tenant_id}:{YYYY-MM-DD}:{feature}:{model}:calls   → call_count

Each matching key is parsed, and an UPSERT is issued to ai_usage_daily:
  ON CONFLICT (tenant_id, date, ai_feature, model)
  DO UPDATE SET
    input_tokens  = ai_usage_daily.input_tokens  + EXCLUDED.input_tokens,
    output_tokens = ai_usage_daily.output_tokens + EXCLUDED.output_tokens,
    call_count    = ai_usage_daily.call_count    + EXCLUDED.call_count,
    updated_at    = NOW()
"""

import asyncio
import logging
import re
from datetime import date
from uuid import UUID

from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

# Pattern: ai_usage:{tenant_id}:{YYYY-MM-DD}:{feature}:{model}:input|output|calls
_KEY_RE = re.compile(
    r"^ai_usage:"
    r"(?P<tenant_id>[0-9a-f-]{36}):"
    r"(?P<date>\d{4}-\d{2}-\d{2}):"
    r"(?P<feature>[^:]+):"
    r"(?P<model>[^:]+):"
    r"(?P<metric>input|output|calls)$"
)


@celery_app.task(name="app.workers.tasks_ai_budget.flush_daily_usage")
def flush_daily_usage() -> None:
    """Aggregate nightly Redis AI usage counters into ai_usage_daily."""
    asyncio.run(_flush_daily_usage_async())


async def _flush_daily_usage_async() -> None:
    from app.database import AsyncSessionLocal
    from app.redis_client import get_worker_redis_client
    from sqlalchemy import text

    redis = get_worker_redis_client()

    # Scan all ai_usage:* keys
    pattern = "ai_usage:*"
    keys: list[str] = []
    cursor = 0
    while True:
        cursor, batch = await redis.scan(cursor, match=pattern, count=500)
        keys.extend(batch)
        if cursor == 0:
            break

    if not keys:
        logger.info("flush_daily_usage_no_keys")
        return

    # Aggregate into { (tenant_id, date, feature, model): {input, output, calls} }
    aggregated: dict[tuple, dict] = {}

    for key in keys:
        # Decode bytes if necessary
        key_str = key.decode("utf-8") if isinstance(key, bytes) else key
        match = _KEY_RE.match(key_str)
        if not match:
            continue

        tenant_id_str = match.group("tenant_id")
        date_str = match.group("date")
        feature = match.group("feature")
        model = match.group("model")
        metric = match.group("metric")

        try:
            tenant_uuid = UUID(tenant_id_str)
            usage_date = date.fromisoformat(date_str)
        except (ValueError, AttributeError):
            logger.warning("flush_daily_usage_invalid_key", extra={"key": key_str})
            continue

        bucket = (tenant_uuid, usage_date, feature, model)
        if bucket not in aggregated:
            aggregated[bucket] = {"input_tokens": 0, "output_tokens": 0, "call_count": 0}

        raw_val = await redis.get(key_str)
        value = int(raw_val or 0)

        if metric == "input":
            aggregated[bucket]["input_tokens"] += value
        elif metric == "output":
            aggregated[bucket]["output_tokens"] += value
        elif metric == "calls":
            aggregated[bucket]["call_count"] += value

    if not aggregated:
        logger.info("flush_daily_usage_nothing_to_flush")
        return

    # Upsert into ai_usage_daily
    async with AsyncSessionLocal() as db:
        upsert_sql = text(
            """
            INSERT INTO ai_usage_daily
                (id, tenant_id, date, ai_feature, model,
                 input_tokens, output_tokens, call_count,
                 created_at, updated_at)
            VALUES
                (gen_random_uuid(), :tenant_id, :date, :ai_feature, :model,
                 :input_tokens, :output_tokens, :call_count,
                 NOW(), NOW())
            ON CONFLICT (tenant_id, date, ai_feature, model)
            DO UPDATE SET
                input_tokens  = ai_usage_daily.input_tokens  + EXCLUDED.input_tokens,
                output_tokens = ai_usage_daily.output_tokens + EXCLUDED.output_tokens,
                call_count    = ai_usage_daily.call_count    + EXCLUDED.call_count,
                updated_at    = NOW()
            """
        )

        flushed = 0
        for (tenant_id, usage_date, feature, model), counts in aggregated.items():
            await db.execute(
                upsert_sql,
                {
                    "tenant_id": str(tenant_id),
                    "date": usage_date,
                    "ai_feature": feature,
                    "model": model,
                    "input_tokens": counts["input_tokens"],
                    "output_tokens": counts["output_tokens"],
                    "call_count": counts["call_count"],
                },
            )
            flushed += 1

        await db.commit()

    logger.info(
        "flush_daily_usage_complete",
        extra={"rows_upserted": flushed},
    )
