from typing import AsyncGenerator

import redis.asyncio as aioredis
from redis.asyncio import Redis

from app.config import get_settings

_pool: Redis | None = None


def _get_pool() -> Redis:
    global _pool
    if _pool is None:
        _pool = aioredis.from_url(
            get_settings().REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
        )
    return _pool


# Module-level singleton — safe for the API server, where one event loop
# lives for the whole process. NOT safe for Celery workers: each task runs
# inside its own asyncio.run() (see app/workers/*), so this client's
# internal locks/connections bind to whichever task's loop touches it
# first and raise "Event loop is closed" the moment a second task reuses
# it. Worker code must use get_worker_redis_client() instead.
redis_client: Redis = _get_pool()


async def get_redis() -> AsyncGenerator[Redis, None]:
    """FastAPI dependency that yields the shared async Redis pool."""
    client = _get_pool()
    try:
        yield client
    finally:
        pass  # Pool is shared — do not close on request end.


def get_worker_redis_client() -> Redis:
    """A fresh, unshared Redis client for Celery task use.

    Same disease as the DB engine's NullPool fix (app/database.py) and the
    AI providers' fresh-client-per-call pattern (app/services/ai/
    ai_service.py) — construct fresh per task instead of caching across
    asyncio.run() boundaries.
    """
    return aioredis.from_url(
        get_settings().REDIS_URL,
        encoding="utf-8",
        decode_responses=True,
    )
