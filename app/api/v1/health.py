from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.redis_client import get_redis

router = APIRouter(tags=["health"])


@router.get("/health")
async def health():
    return {"status": "ok", "version": "1.0.0"}


@router.get("/ready")
async def ready(
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    errors: dict[str, str] = {}

    try:
        await db.execute(text("SELECT 1"))
    except Exception as exc:
        errors["database"] = str(exc)

    try:
        await redis.ping()
    except Exception as exc:
        errors["redis"] = str(exc)

    if errors:
        return JSONResponse(status_code=503, content={"status": "degraded", "errors": errors})

    return {"status": "ok"}
