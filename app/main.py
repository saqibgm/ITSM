import asyncio
import logging
import traceback
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import sentry_sdk
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy.exc import IntegrityError

from app.api.v1.router import router as v1_router
from app.auth.jwks import _jwks_refresh_loop, refresh_jwks
from app.config import get_settings
from app.exceptions import ITSMError
from app.logging_setup import configure_logging
from app.middleware.rate_limit import RateLimitMiddleware
from app.middleware.request_log import RequestLogMiddleware
from app.telemetry import configure_telemetry

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan: fetch JWKS on startup, refresh every 6 h."""
    # Fetch JWKS from IAM before accepting any requests
    await refresh_jwks()

    # Ensure the object-storage bucket exists (best-effort) so attachment
    # uploads/downloads work on a fresh environment.
    try:
        from app.services.storage_service import get_storage_service
        await get_storage_service().ensure_bucket()
        logger.info("storage_bucket_ensured")
    except Exception as exc:  # never block startup on storage
        logger.warning("storage_bucket_ensure_failed: %r", exc)

    # Schedule background refresh every 6 hours
    task = asyncio.create_task(_jwks_refresh_loop())
    logger.info("jwks_background_refresh_scheduled")

    yield

    # Shutdown: cancel the background task cleanly
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    logger.info("jwks_background_refresh_stopped")


def create_app() -> FastAPI:
    # Structured JSON logging must be the very first action so that every
    # subsequent log call (including those inside middleware and lifespan)
    # is formatted correctly and dual-written to system_logs where needed.
    configure_logging()

    settings = get_settings()

    # Sentry init — right after logging so Sentry captures exceptions that
    # occur during the rest of startup.
    if settings.SENTRY_DSN:
        sentry_sdk.init(
            dsn=settings.SENTRY_DSN,
            environment=settings.APP_ENV,
        )

    app = FastAPI(
        title="IQ-ITSM",
        version="1.0.0",
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        lifespan=lifespan,
    )

    # --------------------------------------------------------------------- #
    # Telemetry — configure OTEL before any middleware so instrumentation     #
    # wraps the full stack (no-op when OTEL_ENABLED=False).                  #
    # --------------------------------------------------------------------- #
    configure_telemetry(app)

    # --------------------------------------------------------------------- #
    # Middleware — added in LIFO order: last added = outermost wrapper.      #
    # Desired call order (outermost → innermost):                            #
    #   RequestLogMiddleware → RateLimitMiddleware → CORSMiddleware          #
    #   → security_headers                                                   #
    # --------------------------------------------------------------------- #

    # 1. CORS (innermost of the three we add via add_middleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=get_settings().ALLOWED_ORIGINS,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Tenant-ID"],
    )

    # 2. Rate limiting (wraps CORS)
    app.add_middleware(RateLimitMiddleware)

    # 3. Request logging + metrics recording (outermost — wraps everything)
    app.add_middleware(RequestLogMiddleware)

    # Security headers applied via inline @app.middleware so they run on
    # every response regardless of which middleware handled the request.
    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "default-src 'none'"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        return response

    @app.exception_handler(ITSMError)
    async def itsm_error_handler(request: Request, exc: ITSMError):
        level = "error" if exc.status_code >= 500 else "warning"
        getattr(logger, level)(
            "handled_exception",
            extra={
                "request_id": getattr(request.state, "request_id", None),
                "tenant_id": getattr(request.state, "tenant_id", None),
                "error_code": exc.error_code,
                "status_code": exc.status_code,
                "exception_type": type(exc).__name__,
            },
        )

        content: dict = {
            "error": {
                "code": exc.error_code,
                "message": exc.message,
                "request_id": getattr(request.state, "request_id", None),
            }
        }
        if hasattr(exc, "extra_context"):
            content["error"].update(exc.extra_context())

        return JSONResponse(status_code=exc.status_code, content=content)

    @app.exception_handler(IntegrityError)
    async def integrity_error_handler(request: Request, exc: IntegrityError):
        """DB constraint violations (e.g. duplicate reference-data name) → 409.

        Avoids leaking a 500 for a user-correctable conflict.
        """
        logger.warning(
            "integrity_error",
            extra={
                "request_id": getattr(request.state, "request_id", None),
                "tenant_id": getattr(request.state, "tenant_id", None),
            },
        )
        return JSONResponse(
            status_code=409,
            content={
                "error": {
                    "code": "CONFLICT",
                    "message": "That value conflicts with an existing record (duplicate or constraint violation).",
                    "request_id": getattr(request.state, "request_id", None),
                }
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        logger.critical(
            "unhandled_exception",
            extra={
                "request_id": getattr(request.state, "request_id", None),
                "tenant_id": getattr(request.state, "tenant_id", None),
                "exception_type": type(exc).__name__,
                "traceback": traceback.format_exc(),
            },
        )
        sentry_sdk.capture_exception(exc)

        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "An unexpected error occurred",
                    "request_id": getattr(request.state, "request_id", None),
                }
            },
        )

    # --------------------------------------------------------------------- #
    # Prometheus /metrics endpoint                                           #
    # Gated on PROMETHEUS_ENABLED; returns prometheus_client text format.   #
    # --------------------------------------------------------------------- #
    if settings.PROMETHEUS_ENABLED:
        @app.get("/metrics", include_in_schema=False)
        async def metrics_endpoint():
            return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    app.include_router(v1_router, prefix="/api/v1")

    # --------------------------------------------------------------------- #
    # Socket.IO — mounted AFTER all middleware so the ASGI sub-app is        #
    # wrapped by the full middleware stack.                                   #
    # --------------------------------------------------------------------- #
    from app.socketio_app import socket_app  # noqa: PLC0415
    app.mount("/ws", socket_app)

    return app


app = create_app()
