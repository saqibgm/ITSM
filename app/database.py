from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings


def get_engine():
    s = get_settings()
    if s.IS_CELERY_WORKER:
        # No pooling — every checkout opens a fresh connection bound to
        # whichever event loop is asking, and it's closed (not returned to a
        # pool) when released. See IS_CELERY_WORKER's docstring in config.py.
        return create_async_engine(
            s.DATABASE_URL,
            poolclass=NullPool,
            echo=s.APP_ENV == "development",
        )
    return create_async_engine(
        s.DATABASE_URL,
        pool_size=s.DB_POOL_SIZE,
        max_overflow=s.DB_MAX_OVERFLOW,
        pool_timeout=s.DB_POOL_TIMEOUT,
        echo=s.APP_ENV == "development",
    )


engine = get_engine()
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db():
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(f"SET LOCAL statement_timeout = '{get_settings().DB_STATEMENT_TIMEOUT_SECONDS}s'")
        )
        # Clear any RLS GUC inherited from a pooled connection so each request
        # starts fail-open; get_current_user sets the real tenant/bypass value.
        # (session-level set_config — survives the mid-request commits that
        # SET LOCAL would not.)
        await session.execute(
            text("SELECT set_config('app.tenant_id', '', false), set_config('app.bypass_rls', '', false)")
        )
        try:
            yield session
        finally:
            await session.close()
