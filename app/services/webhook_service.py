"""Outbound webhook service (S6B).

Responsibilities
----------------
dispatch_event()  — fan-out a domain event to all matching tenant endpoints;
                    creates WebhookDelivery rows and enqueues Celery tasks.
deliver()         — executes the HTTP POST for a single delivery; handles
                    retry logic and endpoint auto-disable.

HMAC signing
------------
Signature header: ``X-ITSM-Signature: sha256=<hex>``
The HMAC secret is never logged; never returned in API responses.

Retry schedule (exponential back-off)
--------------------------------------
attempt 1 → retry in 1 minute  (2^0)
attempt 2 → retry in 2 minutes (2^1)
attempt 3 → retry in 4 minutes (2^2)
attempt >= 3 → status=failed; endpoint.failure_count++

Auto-disable
------------
When endpoint.failure_count reaches 10 the endpoint is set is_active=False
and a WARNING is logged.  No further deliveries are created until it is
re-enabled via the API.
"""

import hashlib
import hmac
import json
import logging
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatch
from uuid import UUID

import httpx
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.webhook import WebhookDelivery, WebhookEndpoint

logger = logging.getLogger(__name__)

_MAX_RESPONSE_BODY = 500
_MAX_FAILURES = 10
_DELIVERY_TIMEOUT = 10.0  # seconds


class WebhookService:
    """Domain service for outbound webhook fan-out and delivery."""

    # ------------------------------------------------------------------
    # Pattern matching
    # ------------------------------------------------------------------

    def _matches_event(self, pattern: str, event_type: str) -> bool:
        """Return True if *event_type* matches *pattern*.

        Supports a single trailing wildcard via fnmatch:
            "ticket.*"  matches "ticket.created", "ticket.updated", etc.
            "asset.created" matches only "asset.created" (exact).
            "*"          matches everything.
        """
        return fnmatch(event_type, pattern)

    # ------------------------------------------------------------------
    # Fan-out
    # ------------------------------------------------------------------

    async def dispatch_event(
        self,
        db: AsyncSession,
        tenant_id: str,
        event_type: str,
        payload: dict,
    ) -> None:
        """Fan out a domain event to all matching active endpoints.

        Errors creating individual delivery rows are logged as WARNING and
        silently skipped — they must never propagate back to the caller.
        """
        try:
            result = await db.execute(
                select(WebhookEndpoint).where(
                    and_(
                        WebhookEndpoint.tenant_id == UUID(tenant_id),
                        WebhookEndpoint.is_active.is_(True),
                        WebhookEndpoint.failure_count < _MAX_FAILURES,
                    )
                )
            )
            endpoints = result.scalars().all()
        except Exception as exc:
            logger.warning(
                "webhook_dispatch_load_endpoints_failed",
                extra={"tenant_id": tenant_id, "event_type": event_type, "error": str(exc)},
            )
            return

        for endpoint in endpoints:
            patterns: list = endpoint.events or []
            if not any(self._matches_event(p, event_type) for p in patterns):
                continue

            try:
                delivery = WebhookDelivery(
                    endpoint_id=endpoint.id,
                    tenant_id=endpoint.tenant_id,
                    event_type=event_type,
                    payload=payload,
                    status="pending",
                    attempt_count=0,
                )
                db.add(delivery)
                await db.flush()

                # Import here to avoid circular import at module level
                from app.workers.tasks_webhooks import deliver_webhook
                deliver_webhook.delay(str(delivery.id))

            except Exception as exc:
                logger.warning(
                    "webhook_dispatch_create_delivery_failed",
                    extra={
                        "tenant_id": tenant_id,
                        "event_type": event_type,
                        "endpoint_id": str(endpoint.id),
                        "error": str(exc),
                    },
                )

    # ------------------------------------------------------------------
    # Delivery
    # ------------------------------------------------------------------

    async def deliver(self, db: AsyncSession, delivery_id: str) -> None:
        """Execute the HTTP POST for a single WebhookDelivery.

        Called from the Celery worker task.  Updates the delivery row and
        the parent WebhookEndpoint in-place; the caller is responsible for
        committing the session.
        """
        # 1. Load delivery + endpoint
        result = await db.execute(
            select(WebhookDelivery).where(
                WebhookDelivery.id == UUID(delivery_id)
            )
        )
        delivery = result.scalar_one_or_none()
        if delivery is None:
            logger.warning(
                "webhook_deliver_not_found",
                extra={"delivery_id": delivery_id},
            )
            return

        ep_result = await db.execute(
            select(WebhookEndpoint).where(
                WebhookEndpoint.id == delivery.endpoint_id
            )
        )
        endpoint = ep_result.scalar_one_or_none()
        if endpoint is None:
            logger.warning(
                "webhook_deliver_endpoint_not_found",
                extra={"delivery_id": delivery_id, "endpoint_id": str(delivery.endpoint_id)},
            )
            delivery.status = "failed"
            delivery.error_message = "Endpoint no longer exists"
            db.add(delivery)
            return

        # 2. Build HMAC signature
        payload_json = json.dumps(delivery.payload, separators=(",", ":"), ensure_ascii=False)
        sig_hex = hmac.new(
            endpoint.secret.encode("utf-8"),
            payload_json.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        headers = {
            "Content-Type": "application/json",
            "X-ITSM-Signature": f"sha256={sig_hex}",
            "X-ITSM-Event": delivery.event_type,
            "X-ITSM-Delivery": str(delivery.id),
            "User-Agent": "ITSM-Webhook/1.0",
        }

        # 3. POST
        success = False
        response_status_code: int | None = None
        response_body: str | None = None
        error_message: str | None = None

        try:
            async with httpx.AsyncClient(timeout=_DELIVERY_TIMEOUT) as client:
                resp = await client.post(
                    endpoint.url,
                    content=payload_json,
                    headers=headers,
                )
            response_status_code = resp.status_code
            response_body = resp.text[:_MAX_RESPONSE_BODY]
            success = 200 <= resp.status_code < 300

        except httpx.TimeoutException as exc:
            error_message = f"Request timed out after {_DELIVERY_TIMEOUT}s"
            logger.warning(
                "webhook_deliver_timeout",
                extra={"delivery_id": delivery_id, "url": endpoint.url},
            )
        except Exception as exc:
            error_message = str(exc)[:500]
            logger.warning(
                "webhook_deliver_http_error",
                extra={"delivery_id": delivery_id, "url": endpoint.url, "error": error_message},
            )

        delivery.response_status_code = response_status_code
        delivery.response_body = response_body
        delivery.error_message = error_message

        # 4. Update delivery + endpoint
        if success:
            delivery.status = "delivered"
            delivery.delivered_at = func.now()  # type: ignore[assignment]
            endpoint.failure_count = 0
            endpoint.last_success_at = func.now()  # type: ignore[assignment]
        else:
            delivery.attempt_count = (delivery.attempt_count or 0) + 1
            endpoint.last_failure_at = func.now()  # type: ignore[assignment]

            if delivery.attempt_count < 3:
                delivery.status = "retrying"
                retry_minutes = 2 ** (delivery.attempt_count - 1)  # 1, 2, 4
                # Use Python datetime for scheduling (not DB func.now()) per spec.
                delivery.next_retry_at = (  # type: ignore[assignment]
                    datetime.now(timezone.utc) + timedelta(minutes=retry_minutes)
                )
            else:
                delivery.status = "failed"
                endpoint.failure_count = (endpoint.failure_count or 0) + 1
                if endpoint.failure_count >= _MAX_FAILURES:
                    endpoint.is_active = False
                    logger.warning(
                        "webhook_endpoint_auto_disabled",
                        extra={
                            "endpoint_id": str(endpoint.id),
                            "tenant_id": str(endpoint.tenant_id),
                            "name": endpoint.name,
                            "failure_count": endpoint.failure_count,
                        },
                    )

        db.add(delivery)
        db.add(endpoint)


# ---------------------------------------------------------------------------
# Helper — SQLAlchemy interval expression
# ---------------------------------------------------------------------------

def sa_interval(*, minutes: int):
    """Return a SQLAlchemy expression for a PG INTERVAL of N minutes."""
    from sqlalchemy import text
    return text(f"INTERVAL '{minutes} minutes'")


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

webhook_service = WebhookService()
