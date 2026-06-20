"""
Redis sliding-window rate limiting middleware.

Key design:
- Tenant-scoped:  rl:{tenant_id}:{minute_bucket}
- IP-scoped:      rl:ip:{client_ip}:{minute_bucket}
- Pattern: INCR + EXPIRE(70) on first call; compare to limit.
- Fails OPEN on Redis error — never blocks requests due to infra failure.
- Exempt paths: /metrics, /health, /docs, /openapi.json, /webhooks/
"""

import base64
import json
import logging
import time
from typing import Callable

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from app.config import get_settings

logger = logging.getLogger(__name__)

# Paths that bypass rate limiting entirely.
_EXEMPT_PREFIXES = (
    "/metrics",
    "/health",
    "/docs",
    "/api/docs",
    "/api/redoc",
    "/openapi.json",
    "/webhooks/",
)


def _decode_jwt_payload_unverified(authorization: str | None) -> dict:
    """Extract JWT payload claims without signature verification.

    The Authorization header value is NEVER logged.  Returns an empty dict
    on any parse failure.
    """
    if not authorization or not authorization.startswith("Bearer "):
        return {}
    try:
        token = authorization[len("Bearer "):]
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        # JWT payload is the second segment; base64url-encoded.
        segment = parts[1]
        # Re-pad to a multiple of 4 for standard base64 decoder.
        padding = 4 - len(segment) % 4
        if padding != 4:
            segment += "=" * padding
        decoded = base64.urlsafe_b64decode(segment)
        return json.loads(decoded)
    except Exception:
        return {}


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Per-tenant (or per-IP) sliding-window rate limiter backed by Redis."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)
        self._settings = get_settings()

    async def dispatch(self, request: Request, call_next: Callable):
        # Fast-path: exempt paths skip rate limiting entirely.
        path = request.url.path
        if any(path == p or path.startswith(p) for p in _EXEMPT_PREFIXES):
            return await call_next(request)

        limit = self._settings.RATE_LIMIT_PER_MINUTE

        try:
            redis = self._get_redis()
            key, bucket_start = self._build_key(request)
            count = await self._increment(redis, key)

            next_minute = bucket_start + 60
            remaining = max(0, limit - count)

            if count > limit:
                return JSONResponse(
                    status_code=429,
                    content={
                        "error": {
                            "code": "RATE_LIMIT_EXCEEDED",
                            "message": "Too many requests. Please slow down.",
                        }
                    },
                    headers={
                        "X-RateLimit-Limit": str(limit),
                        "X-RateLimit-Remaining": "0",
                        "X-RateLimit-Reset": str(int(next_minute)),
                    },
                )

            response = await call_next(request)
            response.headers["X-RateLimit-Limit"] = str(limit)
            response.headers["X-RateLimit-Remaining"] = str(remaining)
            response.headers["X-RateLimit-Reset"] = str(int(next_minute))
            return response

        except Exception as exc:
            # Fail open — Redis failure must never block legitimate requests.
            logger.warning(
                "rate_limit_middleware_error",
                extra={"error": str(exc), "path": path},
            )
            return await call_next(request)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_key(self, request: Request) -> tuple[str, float]:
        """Return (redis_key, minute_bucket_start_epoch)."""
        minute_bucket = int(time.time() // 60)
        bucket_start = minute_bucket * 60

        # Try to extract tenant_id from JWT payload (no sig check).
        auth_header = request.headers.get("Authorization")
        payload = _decode_jwt_payload_unverified(auth_header)
        tenant_id: str | None = payload.get("tenant_id") or payload.get("org_id")

        if tenant_id:
            key = f"rl:{tenant_id}:{minute_bucket}"
        else:
            client_ip = (request.client.host if request.client else "unknown")
            key = f"rl:ip:{client_ip}:{minute_bucket}"

        return key, float(bucket_start)

    @staticmethod
    async def _increment(redis, key: str) -> int:
        """INCR key; set EXPIRE 70s only on the first increment (pipe)."""
        pipe = redis.pipeline()
        pipe.incr(key)
        pipe.ttl(key)
        results = await pipe.execute()
        count: int = results[0]
        ttl: int = results[1]
        if ttl == -1:
            # Key exists but has no TTL — set it now.
            await redis.expire(key, 70)
        return count

    @staticmethod
    def _get_redis():
        """Return the shared async Redis client; import deferred to avoid circular imports."""
        from app.redis_client import redis_client
        return redis_client
