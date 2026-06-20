"""
Request logging middleware (S4.2).

Emits ONE structured JSON log line per request containing:
  request_id, method, path, query_string (redacted), status_code,
  duration_ms, tenant_id_hint.

Security rules (hard requirements):
- NEVER logs Authorization header value or any token/cookie value.
- Redacts query-string params named token, key, password, secret → "***".
- Never logs request body.
"""

import base64
import json
import logging
import time
import uuid
from typing import Callable
from urllib.parse import parse_qs, urlencode

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.types import ASGIApp

logger = logging.getLogger(__name__)

# Query-string parameter names whose values must be redacted.
_SENSITIVE_PARAMS = frozenset({"token", "key", "password", "secret"})


def _redact_query_string(raw_qs: str) -> str:
    """Replace sensitive query-param values with '***'."""
    if not raw_qs:
        return ""
    try:
        params = parse_qs(raw_qs, keep_blank_values=True)
        redacted = {
            k: (["***"] if k.lower() in _SENSITIVE_PARAMS else v)
            for k, v in params.items()
        }
        return urlencode(redacted, doseq=True)
    except Exception:
        return "[redacted]"


def _decode_jwt_payload_unverified(authorization: str | None) -> dict:
    """Extract JWT payload claims without signature verification.

    Returns an empty dict on any parse failure.
    The Authorization header value is NEVER stored or forwarded.
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


class RequestLogMiddleware(BaseHTTPMiddleware):
    """Wraps every request, assigns a UUID request-id, logs a single INFO line."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: Callable):
        request_id = str(uuid.uuid4())
        # Make available to downstream handlers (exception handlers, etc.)
        request.state.request_id = request_id

        start = time.monotonic()
        status_code = 500  # default in case call_next raises

        try:
            response = await call_next(request)
            status_code = response.status_code
        except Exception:
            # Log before re-raising so we capture the request details.
            self._emit_log(request, request_id, start, status_code)
            raise

        self._emit_log(request, request_id, start, status_code)
        response.headers["X-Request-ID"] = request_id
        return response

    @staticmethod
    def _emit_log(request: Request, request_id: str, start: float, status_code: int) -> None:
        """Emit one structured INFO log line and record Prometheus metrics."""
        duration_ms = int((time.monotonic() - start) * 1000)

        # Tenant hint: decode JWT payload without signature verification.
        # The Authorization header value is NEVER stored or forwarded.
        auth_header = request.headers.get("Authorization")
        payload = _decode_jwt_payload_unverified(auth_header)
        tenant_id_hint: str = (
            payload.get("tenant_id") or payload.get("org_id") or "anonymous"
        )

        # Redact sensitive query params.
        safe_qs = _redact_query_string(request.url.query)

        logger.info(
            "http_request",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "query_string": safe_qs,
                "status_code": status_code,
                "duration_ms": duration_ms,
                "tenant_id_hint": tenant_id_hint,
            },
        )

        # Record Prometheus metrics (import deferred to avoid circular imports).
        try:
            from app.metrics import record_request
            record_request(
                method=request.method,
                path_template=request.url.path,
                status_code=str(status_code),
                duration_sec=duration_ms / 1000.0,
            )
        except Exception as exc:
            logger.warning("metrics_record_failed", extra={"error": str(exc)})
