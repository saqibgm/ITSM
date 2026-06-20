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


# Module-level singleton used by Celery tasks (synchronous context).
# Celery tasks must call redis_client.get() or use the sync redis library directly.
redis_client: Redis = _get_pool()


async def get_redis() -> AsyncGenerator[Redis, None]:
    """FastAPI dependency that yields the shared async Redis pool."""
    client = _get_pool()
    try:
        yield client
    finally:
        pass  # Pool is shared — do not close on request end.
