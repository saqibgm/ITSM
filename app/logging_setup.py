"""
Structured JSON logging + SystemLog dual-write handler.

Call ``configure_logging()`` once at application startup — before anything
that might emit a log record.

Design rules (per 06-engineering-standards.md):
- Every log line is valid JSON (JSONFormatter).
- WARNING+ records are dual-written to the system_logs DB table so tenant
  admins and platform admins can query them from the UI.
- Secrets (Authorization header, API keys, passwords) are NEVER present in
  any field that reaches this formatter.  Callers are responsible for not
  passing secret values in ``extra={}`` dicts.
- Stack traces are stored in system_logs.stack_trace but never forwarded in
  HTTP responses.
"""

import asyncio
import json
import logging
from datetime import UTC, datetime


# ---------------------------------------------------------------------------
# JSON formatter
# ---------------------------------------------------------------------------


class JSONFormatter(logging.Formatter):
    """Formats every log record as a single JSON line written to stdout."""

    # Fields that callers may attach via logger.xxx("msg", extra={...})
    _EXTRA_FIELDS = (
        "request_id",
        "tenant_id",
        "user_id",
        "method",
        "path",
        "status",
        "latency_ms",
        "error_code",
        "exception_type",
        "ticket_id",
        "asset_id",
        "ai_feature",
        "model",
        "input_tokens",
        "output_tokens",
        "client_ip",
        "platform_role",
        "iam_user_id",
        "attempt",
        "used_tokens",
        "limit_tokens",
        "traceback",
        "extra_context",
    )

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "event": record.getMessage(),
            "logger": record.name,
        }

        for key in self._EXTRA_FIELDS:
            if hasattr(record, key):
                payload[key] = getattr(record, key)

        return json.dumps(payload, default=str)


# ---------------------------------------------------------------------------
# DB dual-write handler
# ---------------------------------------------------------------------------


class SystemLogDBHandler(logging.Handler):
    """Dual-writes WARNING+ records to the system_logs DB table.

    Uses ``asyncio.get_running_loop().create_task()`` so the DB write is
    fire-and-forget and never blocks the calling coroutine.  If there is no
    running event loop (e.g. during module import or Alembic migration runs),
    the DB write is silently skipped — the stdout write still happens via the
    JSONFormatter handler.
    """

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < logging.WARNING:
            return  # DEBUG and INFO skip DB write

        # Capture values synchronously before the record is recycled.
        tenant_id = getattr(record, "tenant_id", None)
        request_id = getattr(record, "request_id", None)
        level = record.levelname.lower()
        event = record.getMessage()
        exception_type = getattr(record, "exception_type", None)
        # Stack trace only for CRITICAL / unhandled exceptions
        stack_trace: str | None = None
        if record.levelno >= logging.CRITICAL:
            stack_trace = getattr(record, "traceback", None)
        extra_context = getattr(record, "extra_context", {})

        async def _write() -> None:
            try:
                from app.database import AsyncSessionLocal
                from app.models.system_logs import SystemLog
                from uuid_extensions import uuid7

                async with AsyncSessionLocal() as session:
                    log_row = SystemLog(
                        id=uuid7(),
                        tenant_id=tenant_id,
                        request_id=request_id,
                        level=level,
                        event=event,
                        message=event,
                        exception_type=exception_type,
                        stack_trace=stack_trace,
                        metadata_=extra_context if isinstance(extra_context, dict) else {},
                    )
                    session.add(log_row)
                    await session.commit()
            except Exception:
                # Never let a logging handler crash the application.
                # If the DB is down, the record still appeared on stdout.
                pass

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_write())
        except RuntimeError:
            # No running event loop (startup, migration, sync context) —
            # skip DB write, stdout-only is acceptable.
            pass


# ---------------------------------------------------------------------------
# Public setup function
# ---------------------------------------------------------------------------


def configure_logging() -> None:
    """Configure root logger with JSON stdout handler and DB dual-write handler.

    Must be called once, as the very first action inside ``create_app()``,
    before any other logger is obtained or used.
    """
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # Remove any default handlers added by uvicorn or other frameworks
    # that may already be present before our call (idempotent-safe check).
    if not any(isinstance(h, logging.StreamHandler) and isinstance(h.formatter, JSONFormatter)
               for h in root.handlers):
        stdout_handler = logging.StreamHandler()
        stdout_handler.setFormatter(JSONFormatter())
        root.addHandler(stdout_handler)

    if not any(isinstance(h, SystemLogDBHandler) for h in root.handlers):
        db_handler = SystemLogDBHandler()
        db_handler.setLevel(logging.WARNING)
        root.addHandler(db_handler)
