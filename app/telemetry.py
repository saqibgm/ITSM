"""
OpenTelemetry setup for IQ-ITSM (S4.2).

configure_telemetry(app) is a no-op when settings.OTEL_ENABLED is False,
so there is zero overhead in development / test environments.

Instruments:
- FastAPI (HTTP spans)
- SQLAlchemy (DB query spans)
- Redis (cache spans)
- AsyncPG (low-level DB spans)

Also installs a BaggageMiddleware that propagates tenant_id as a baggage
attribute on every trace span, extracted from the JWT payload without
signature verification.
"""

import base64
import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# JWT payload helper (no sig check — sig is already verified by get_current_user)
# ---------------------------------------------------------------------------


def _decode_jwt_payload_unverified(authorization: str | None) -> dict:
    """Extract JWT payload claims without signature verification.

    The Authorization header value is NEVER logged or stored.
    Returns an empty dict on any parse failure.
    """
    if not authorization or not authorization.startswith("Bearer "):
        return {}
    try:
        token = authorization[len("Bearer "):]
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        segment = parts[1]
        padding = 4 - len(segment) % 4
        if padding != 4:
            segment += "=" * padding
        decoded = base64.urlsafe_b64decode(segment)
        return json.loads(decoded)
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Baggage middleware class (defined at module level so add_middleware works)
# ---------------------------------------------------------------------------


class _TenantBaggageMiddleware:
    """ASGI middleware that sets tenant_id as OpenTelemetry baggage on every span.

    Intentionally NOT a BaseHTTPMiddleware subclass so it can be instantiated
    without a bound app reference before the provider is initialised.
    """

    def __init__(self, app):
        self._app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self._app(scope, receive, send)
            return

        from opentelemetry import baggage
        from opentelemetry.context import attach, detach

        # Extract Authorization from scope headers (list of byte tuples).
        auth_header: str | None = None
        for name, value in scope.get("headers", []):
            if name.lower() == b"authorization":
                try:
                    auth_header = value.decode("latin-1")
                except Exception:
                    pass
                break

        payload = _decode_jwt_payload_unverified(auth_header)
        tenant_id: str = (
            payload.get("tenant_id") or payload.get("org_id") or "anonymous"
        )

        ctx = baggage.set_baggage("tenant_id", tenant_id)
        token = attach(ctx)
        try:
            await self._app(scope, receive, send)
        finally:
            detach(token)


# ---------------------------------------------------------------------------
# Public entry-point
# ---------------------------------------------------------------------------


def configure_telemetry(app: "FastAPI") -> None:
    """Instrument the FastAPI application with OpenTelemetry.

    This is a no-op when settings.OTEL_ENABLED is False.
    All OTel imports are local so that the package is optional at runtime
    (import errors are caught and logged as warnings).
    """
    from app.config import get_settings

    settings = get_settings()

    if not settings.OTEL_ENABLED:
        return

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.instrumentation.asyncpg import AsyncPGInstrumentor
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.redis import RedisInstrumentor
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create({"service.name": "itsm-service"})
        provider = TracerProvider(resource=resource)

        exporter = OTLPSpanExporter(endpoint=settings.OTEL_ENDPOINT, insecure=True)
        provider.add_span_processor(BatchSpanProcessor(exporter))

        trace.set_tracer_provider(provider)

        # Instrument each library.
        FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)
        SQLAlchemyInstrumentor().instrument(tracer_provider=provider)
        RedisInstrumentor().instrument(tracer_provider=provider)
        AsyncPGInstrumentor().instrument(tracer_provider=provider)

        # Add baggage middleware for tenant_id propagation.
        app.add_middleware(_TenantBaggageMiddleware)

        logger.info(
            "otel_configured",
            extra={"endpoint": settings.OTEL_ENDPOINT},
        )

    except ImportError as exc:
        logger.warning(
            "otel_packages_missing",
            extra={"error": str(exc)},
        )
    except Exception as exc:
        logger.warning(
            "otel_configure_failed",
            extra={"error": str(exc)},
        )
