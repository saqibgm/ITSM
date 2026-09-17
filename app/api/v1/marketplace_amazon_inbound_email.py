"""
Amazon inbound-message email bridge — webhook route (2026-09-17).

Amazon's SP-API has no endpoint to read buyer-sent messages (confirmed,
see connectors/amazon.py's module docstring) — but Amazon officially
forwards every buyer-seller message to an email address configured in
Seller Central under Notification Preferences → Buyer-Seller Messages.
Replying is also legitimate: Amazon's Buyer-Seller Messaging policy
permits a normal email reply from the address registered on that seller
account, delivered back to the buyer. This route is the INBOUND half of
that bridge — receives the forwarded email, extracts the order id and
message body, and feeds it into the same MarketplaceEvent → Celery
pipeline every other connector's real webhook already uses (see
tasks_marketplace_sync.py's process_marketplace_event).

Routing to the right tenant: each MarketplaceConnection gets its own
inbound address — amazon+{connection_id}@{AMAZON_INBOUND_EMAIL_DOMAIN} —
surfaced on the Amazon admin card (see marketplace_amazon.py's
/connection route) for a tenant to paste into their own Seller Central
Notification Preferences. No new DB column needed; the address is
derived from connection.id.

What this route does NOT do: talk to the org's actual mail service.
"We have our own email service, will integrate it later" (2026-09-17) —
this route accepts raw RFC822 email bytes as its POST body regardless of
which mail service eventually delivers them, so the deployment step is
just "make our mail service POST forwarded mail here," not anything this
app needs to know in advance. Until AMAZON_INBOUND_EMAIL_WEBHOOK_TOKEN is
configured, the route 503s rather than silently accepting unauthenticated
mail — a placeholder auth boundary until the org's own mail service's
real signing/auth scheme (unknown at build time) can be verified instead.
"""

import email
import email.policy
import email.utils
import hmac
import logging
import re
import uuid
from email.message import EmailMessage
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models.marketplace import MarketplaceConnection, MarketplaceEvent

logger = logging.getLogger(__name__)

webhook_router = APIRouter(tags=["marketplaces-webhooks"])

# amazon+{uuid}@... — the plus-addressed connection id this route resolves
# incoming mail against. Deliberately matches any UUID shape rather than
# requiring dashes-only, since some mail services normalize addressing.
_CONNECTION_ADDRESS_RE = re.compile(r"amazon\+([0-9a-fA-F-]{32,36})@", re.IGNORECASE)
_AMAZON_ORDER_ID_RE = re.compile(r"\d{3}-\d{7}-\d{7}")

# Heuristic quoted-reply/signature stripping — good enough for a first
# pass; Amazon's own forwarded-message wrapper text is not something this
# had a real sample to test against (no live seller account/traffic yet),
# so this errs toward keeping content rather than aggressively trimming.
_QUOTE_MARKERS = (
    re.compile(r"^\s*On .+ wrote:\s*$", re.MULTILINE),
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^From:\s.+$", re.MULTILINE),
)


def _extract_body(msg: EmailMessage) -> str:
    body_part = msg.get_body(preferencelist=("plain", "html"))
    if body_part is None:
        return ""
    try:
        text = body_part.get_content()
    except Exception:
        return ""
    if body_part.get_content_type() == "text/html":
        text = re.sub(r"<[^>]+>", " ", text)
    for marker in _QUOTE_MARKERS:
        match = marker.search(text)
        if match:
            text = text[: match.start()]
    return text.strip()


def _extract_order_id(subject: str, body: str) -> Optional[str]:
    match = _AMAZON_ORDER_ID_RE.search(subject) or _AMAZON_ORDER_ID_RE.search(body)
    return match.group(0) if match else None


@webhook_router.post("/webhooks/marketplace-email/amazon")
async def amazon_inbound_email(request: Request, db: AsyncSession = Depends(get_db)):
    settings = get_settings()
    if not settings.AMAZON_INBOUND_EMAIL_WEBHOOK_TOKEN:
        logger.warning("amazon_inbound_email_not_configured")
        return JSONResponse(status_code=503, content={"error": "Amazon inbound-email bridge is not configured on this deployment"})

    provided_token = request.query_params.get("token", "")
    if not hmac.compare_digest(provided_token, settings.AMAZON_INBOUND_EMAIL_WEBHOOK_TOKEN):
        logger.warning("amazon_inbound_email_bad_token")
        return JSONResponse(status_code=401, content={"error": "invalid token"})

    raw_body = await request.body()
    try:
        msg = email.message_from_bytes(raw_body, policy=email.policy.default)
    except Exception as exc:
        logger.error("amazon_inbound_email_parse_failed", extra={"error": str(exc)}, exc_info=True)
        return JSONResponse(status_code=400, content={"error": "could not parse email"})

    to_header = str(msg.get("To", ""))
    match = _CONNECTION_ADDRESS_RE.search(to_header)
    if not match:
        # Not one of our per-connection addresses — 200 so the mail
        # service doesn't retry-forward this indefinitely.
        logger.info("amazon_inbound_email_unroutable_address", extra={"to": to_header})
        return JSONResponse(status_code=200, content={"received": True})

    try:
        connection_id = uuid.UUID(match.group(1))
    except ValueError:
        return JSONResponse(status_code=200, content={"received": True})

    connection = (
        await db.execute(
            select(MarketplaceConnection).where(
                MarketplaceConnection.id == connection_id,
                MarketplaceConnection.provider == "amazon",
            )
        )
    ).scalar_one_or_none()
    if connection is None:
        logger.info("amazon_inbound_email_unknown_connection", extra={"connection_id": str(connection_id)})
        return JSONResponse(status_code=200, content={"received": True})

    message_id = str(msg.get("Message-ID", "")).strip() or str(uuid.uuid4())
    from_name, from_addr = email.utils.parseaddr(str(msg.get("From", "")))
    subject = str(msg.get("Subject", ""))
    body = _extract_body(msg)
    order_id = _extract_order_id(subject, body)

    external_event_id = f"inbound_email:{message_id}"
    existing_event = (
        await db.execute(
            select(MarketplaceEvent).where(
                MarketplaceEvent.provider == "amazon",
                MarketplaceEvent.external_event_id == external_event_id,
            )
        )
    ).scalar_one_or_none()
    if existing_event is not None:
        return JSONResponse(status_code=200, content={"received": True})

    event = MarketplaceEvent(
        tenant_id=connection.tenant_id,
        connection_id=connection.id,
        provider="amazon",
        external_event_id=external_event_id,
        event_type="inbound_email",
        payload={
            "message_id": message_id,
            "from": from_addr,
            "subject": subject,
            "body": body,
            "order_id": order_id,
        },
        status="received",
    )
    db.add(event)
    await db.commit()
    await db.refresh(event)

    from app.workers.tasks_marketplace_sync import process_marketplace_event
    process_marketplace_event.delay(str(event.id))

    return JSONResponse(status_code=200, content={"received": True})


__all__ = ["webhook_router"]
