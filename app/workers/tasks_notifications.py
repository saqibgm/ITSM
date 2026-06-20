"""Celery tasks for transactional email delivery and Socket.IO push.

Both tasks are placed on the "high" priority queue (configured in celery_app).
send_email_notification retries up to 3 times with exponential backoff.
push_socketio_notification is fire-and-forget (no retry — Socket.IO is best-effort).
"""

import logging
import os
from pathlib import Path

from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

# Templates directory relative to this file's package root
_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates" / "email"


# ---------------------------------------------------------------------------
# Email delivery
# ---------------------------------------------------------------------------


@celery_app.task(
    name="app.workers.tasks_notifications.send_email",
    queue="high",
    bind=True,
    max_retries=3,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=120,
    retry_jitter=True,
)
def send_email_notification(
    self,
    to_email: str,
    template_name: str,
    context: dict,
) -> None:
    """Send a transactional email using Jinja2 template and aiosmtplib.

    Args:
        to_email:      Recipient address.
        template_name: Template stem, e.g. "ticket_assigned".
                       Resolves to app/templates/email/{template_name}.html
        context:       Template context variables.

    Retries up to 3 times with exponential backoff on any exception.
    On max retries exceeded the task moves to the dead-letter queue and a
    Sentry alert fires via Celery Flower.
    """
    import asyncio

    try:
        asyncio.run(_async_send_email(to_email=to_email, template_name=template_name, context=context))
    except Exception as exc:
        logger.error(
            "email_send_failed",
            extra={
                "to_email": to_email,
                "template": template_name,
                "attempt": self.request.retries + 1,
                "error": str(exc),
            },
        )
        raise  # autoretry_for will handle retry


async def _async_send_email(
    *,
    to_email: str,
    template_name: str,
    context: dict,
) -> None:
    """Render Jinja2 template and deliver via aiosmtplib."""
    import aiosmtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    from jinja2 import Environment, FileSystemLoader, TemplateNotFound

    from app.config import get_settings

    settings = get_settings()

    # Render template
    try:
        env = Environment(
            loader=FileSystemLoader(str(_TEMPLATES_DIR)),
            autoescape=True,
        )
        tmpl = env.get_template(f"{template_name}.html")
        html_body = tmpl.render(**context)
        subject = context.get("title", f"ITSM Notification — {template_name.replace('_', ' ').title()}")
    except TemplateNotFound:
        logger.warning("email_template_not_found", extra={"template": template_name})
        # Fall back to plain text
        html_body = f"<p>{context.get('body', context.get('title', 'You have a new ITSM notification.'))}</p>"
        subject = context.get("title", "ITSM Notification")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{settings.EMAIL_FROM_NAME} <{settings.SMTP_USER}>"
    msg["To"] = to_email
    msg.attach(MIMEText(html_body, "html"))

    if not settings.SMTP_HOST or settings.SMTP_HOST == "localhost":
        # Dev/test: log instead of actually sending
        logger.info(
            "email_send_skipped_dev",
            extra={"to": to_email, "subject": subject, "template": template_name},
        )
        return

    await aiosmtplib.send(
        msg,
        hostname=settings.SMTP_HOST,
        port=settings.SMTP_PORT,
        username=settings.SMTP_USER or None,
        password=settings.SMTP_PASS or None,
        start_tls=True,
    )

    logger.info(
        "email_sent",
        extra={"to": to_email, "template": template_name, "subject": subject},
    )


# ---------------------------------------------------------------------------
# Socket.IO push
# ---------------------------------------------------------------------------


@celery_app.task(
    name="app.workers.tasks_notifications.push_socketio",
    queue="high",
)
def push_socketio_notification(
    tenant_id: str,
    user_id: str,
    notification_data: dict,
) -> None:
    """Emit a Socket.IO event to the user's room.

    Uses the Redis-backed Socket.IO manager consistent with Project-IQ.
    Room name convention: "user_{user_id}".

    This task is fire-and-forget — no retries.  Socket.IO delivery is
    best-effort; the DB row is the source of truth.
    """
    try:
        import asyncio
        asyncio.run(_async_push_socketio(tenant_id=tenant_id, user_id=user_id, data=notification_data))
    except Exception as exc:
        logger.warning(
            "socketio_push_failed",
            extra={"tenant_id": tenant_id, "user_id": user_id, "error": str(exc)},
        )
        # Intentionally no re-raise — Socket.IO push is best-effort


async def _async_push_socketio(
    *,
    tenant_id: str,
    user_id: str,
    data: dict,
) -> None:
    """Emit to the user's Socket.IO room via the Redis manager.

    Uses a write-only ``AsyncRedisManager`` so the Celery worker does not
    need a full Socket.IO server — it just publishes to Redis and the main
    app's server picks it up.

    Room naming convention must match ``app/socketio_app.py``:
      ``user:<user_id>``
    """
    import socketio

    from app.config import get_settings

    settings = get_settings()

    # write_only=True: Celery worker never acts as a server; it only emits.
    mgr = socketio.AsyncRedisManager(settings.REDIS_URL, write_only=True)

    room = f"user:{user_id}"
    await mgr.emit(
        "notification",
        data={**data, "tenant_id": tenant_id},
        room=room,
        namespace="/",
    )

    logger.info(
        "socketio_pushed",
        extra={"tenant_id": tenant_id, "user_id": user_id, "type": data.get("type")},
    )
