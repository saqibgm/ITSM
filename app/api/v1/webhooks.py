"""
IAM webhook receiver.

Endpoint: POST /api/v1/webhooks/iam

Security
--------
Every request is verified with HMAC-SHA256.  The IAM service attaches
the header ``X-IAM-Signature: sha256=<hex>`` using the shared secret
``settings.IAM_WEBHOOK_SECRET``.  We recompute the HMAC over the raw
request body and compare with ``secrets.compare_digest`` (constant-time)
to prevent timing-based attacks.

Secrets are never logged — we only log the outcome of verification.

Event routing
-------------
"product.subscribed"      + product == "itsm"  → provision tenant
"product.unsubscribed"    + product == "itsm"  → suspend tenant
"organization.suspended"                        → suspend tenant
"organization.reactivated"                      → unsuspend tenant
"user.deactivated"/"user.suspended"/"user.deleted"/"member.removed"/"user.offboarded"
                                                → deactivate the user's ITSM mirror now
"user.reactivated"/"member.added"/"user.restored"
                                                → reactivate the user's ITSM mirror
"user.role_changed"/"user.updated"/"user.roles_changed" (+ roles[]) → refresh iam_roles
unknown events                                  → 200 OK (forward-compatible)

User-lifecycle events carry `user_id` (the IAM sub) and optional `org_id`/`roles`.
They give *immediate* propagation; absent them, mirrors still refresh on next
login. NOTE: this is the receiver — IAM/Keycloak must be configured to emit these
events for them to fire.

Tenant provisioning
-------------------
Provisioning is idempotent: replayed webhooks are detected via
``iam_org_id`` uniqueness and return 200 without re-seeding.

Defaults seeded in a single DB transaction:
  1. SLA policies (Critical / High / Medium / Low) with business_hours_only=True
  2. TenantSequence rows for prefixes: INC, REQ, PRB, CHG, AST
  3. KBSpace "General" — skipped if the model isn't available yet (TODO)

If anything inside the transaction fails the whole transaction is rolled
back and a 500 is returned so IAM retries the delivery.
"""

import hashlib
import hmac
import logging
import re
import secrets

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.database import AsyncSessionLocal
from app.exceptions import AuthenticationError
from app.models.identity import Tenant, TenantSequence, User

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(name: str) -> str:
    """Convert an org name to a URL-safe slug.

    Examples:
        "Acme Corp"  → "acme-corp"
        "99 Technologies!"  → "99-technologies"
    """
    slug = name.lower().strip()
    slug = _SLUG_RE.sub("-", slug)
    return slug.strip("-")[:100]


async def _verify_hmac(request: Request, body: bytes) -> None:
    """Verify the X-IAM-Signature header against the shared webhook secret.

    Raises:
        AuthenticationError: if the header is absent or the digest mismatches.
    """
    signature_header: str | None = request.headers.get("X-IAM-Signature")
    if not signature_header:
        logger.warning(
            "webhook_missing_signature",
            extra={"request_id": getattr(request.state, "request_id", None)},
        )
        raise AuthenticationError("X-IAM-Signature header is required")

    # Header format: "sha256=<hex>"
    if not signature_header.startswith("sha256="):
        raise AuthenticationError("X-IAM-Signature must use sha256= prefix")

    provided_digest = signature_header[len("sha256="):]

    settings = get_settings()
    expected_digest = hmac.new(
        settings.IAM_WEBHOOK_SECRET.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()

    if not secrets.compare_digest(provided_digest, expected_digest):
        logger.warning(
            "webhook_signature_mismatch",
            extra={"request_id": getattr(request.state, "request_id", None)},
        )
        raise AuthenticationError("Webhook signature verification failed")


async def _provision_tenant(org_id: str, org_name: str) -> None:
    """Create a new tenant and seed default configuration.

    Everything runs in one transaction.  If the tenant already exists
    (idempotent replay) the function returns silently — no re-seeding.

    Raises:
        Exception: any unexpected DB error — caller should return 500.
    """
    async with AsyncSessionLocal() as session:
        async with session.begin():
            # ---- Check idempotency ----------------------------------------
            result = await session.execute(
                select(Tenant).where(Tenant.iam_org_id == org_id)
            )
            existing = result.scalar_one_or_none()
            if existing is not None:
                logger.info(
                    "webhook_tenant_already_exists",
                    extra={"iam_org_id": org_id},
                )
                return  # idempotent — nothing to do

            # ---- Create tenant --------------------------------------------
            slug = _slugify(org_name)

            # Ensure slug uniqueness by appending a short suffix on collision
            suffix = 0
            candidate_slug = slug
            while True:
                clash = await session.execute(
                    select(Tenant).where(Tenant.slug == candidate_slug)
                )
                if clash.scalar_one_or_none() is None:
                    break
                suffix += 1
                candidate_slug = f"{slug}-{suffix}"
            slug = candidate_slug

            tenant = Tenant(
                iam_org_id=org_id,
                name=org_name,
                slug=slug,
                is_active=True,
            )
            session.add(tenant)
            await session.flush()  # populate tenant.id

            # ---- a. SLA Policies ------------------------------------------
            # Inline import to avoid circular dependency at module load time.
            try:
                from app.models.sla import SLAPolicy  # type: ignore[import]

                sla_defaults = [
                    # (name,       response_minutes, resolution_minutes)
                    ("Critical",   15,   240),
                    ("High",       30,   480),
                    ("Medium",    120,  1440),
                    ("Low",       480,  4320),
                ]
                for name, response_mins, resolution_mins in sla_defaults:
                    sla = SLAPolicy(
                        tenant_id=tenant.id,
                        name=name,
                        response_time_minutes=response_mins,
                        resolution_time_minutes=resolution_mins,
                        business_hours_only=True,
                        is_default=True,
                    )
                    session.add(sla)

            except ImportError:
                # SLAPolicy model not yet implemented — skip, will be seeded
                # in the session that introduces app/models/sla.py.
                logger.info(
                    "webhook_sla_seed_skipped",
                    extra={
                        "reason": "SLAPolicy model not yet available",
                        "iam_org_id": org_id,
                    },
                )

            # ---- b. TenantSequence rows -----------------------------------
            for prefix in ("INC", "REQ", "PRB", "CHG", "AST"):
                seq = TenantSequence(
                    tenant_id=tenant.id,
                    prefix=prefix,
                    last_value=0,
                )
                session.add(seq)

            # ---- c. Default KBSpace --------------------------------------
            # TODO: implement once app/models/kb.py is available (KBSpace model).
            try:
                from app.models.kb import KBSpace  # type: ignore[import]

                kb_space = KBSpace(
                    tenant_id=tenant.id,
                    name="General",
                    slug="general",
                    scope="tenant_wide",
                )
                session.add(kb_space)

            except ImportError:
                # KBSpace model not yet implemented — skip silently.
                logger.info(
                    "webhook_kb_space_seed_skipped",
                    extra={
                        "reason": "KBSpace model not yet available",
                        "iam_org_id": org_id,
                    },
                )

            # session.begin() commit happens automatically on context exit

    logger.info(
        "webhook_tenant_provisioned",
        extra={"iam_org_id": org_id, "slug": slug},
    )


async def _set_tenant_active(org_id: str, *, is_active: bool) -> None:
    """Set tenant.is_active for the given iam_org_id.

    If no matching tenant is found, logs a warning and returns — the webhook
    may arrive before the subscription event in an out-of-order delivery.
    """
    async with AsyncSessionLocal() as session:
        async with session.begin():
            result = await session.execute(
                select(Tenant).where(Tenant.iam_org_id == org_id)
            )
            tenant = result.scalar_one_or_none()
            if tenant is None:
                logger.warning(
                    "webhook_tenant_not_found",
                    extra={"iam_org_id": org_id, "action": "set_active", "value": is_active},
                )
                return

            tenant.is_active = is_active

    action = "unsuspended" if is_active else "suspended"
    logger.info(
        f"webhook_tenant_{action}",
        extra={"iam_org_id": org_id},
    )


async def _set_user_active(iam_user_id: str, org_id: str, *, is_active: bool) -> None:
    """Activate/deactivate a user's ITSM mirror immediately on an IAM lifecycle
    event (offboarding / suspension) — instead of waiting for the next login.

    Scoped to the user's tenant (resolved from org_id) when provided; otherwise
    applies to every tenant-mirror of that iam_user_id. No-op if the user has no
    ITSM mirror yet (they never logged in)."""
    async with AsyncSessionLocal() as session:
        async with session.begin():
            q = select(User).where(User.iam_user_id == iam_user_id)
            if org_id:
                tenant = (await session.execute(
                    select(Tenant).where(Tenant.iam_org_id == org_id)
                )).scalar_one_or_none()
                if tenant is not None:
                    q = q.where(User.tenant_id == tenant.id)
            users = (await session.execute(q)).scalars().all()
            for u in users:
                u.is_active = is_active
    logger.info(
        "webhook_user_active_set",
        extra={"iam_user_id": iam_user_id, "org_id": org_id,
               "is_active": is_active, "count": len(users)},
    )


async def _sync_user_roles(iam_user_id: str, org_id: str, roles: list) -> None:
    """Refresh a user's mirrored iam_roles immediately on an IAM role change.

    Only updates when the event payload carries the new role set; otherwise the
    mirror is refreshed on the user's next login anyway."""
    if not roles:
        return
    async with AsyncSessionLocal() as session:
        async with session.begin():
            q = select(User).where(User.iam_user_id == iam_user_id)
            if org_id:
                tenant = (await session.execute(
                    select(Tenant).where(Tenant.iam_org_id == org_id)
                )).scalar_one_or_none()
                if tenant is not None:
                    q = q.where(User.tenant_id == tenant.id)
            for u in (await session.execute(q)).scalars().all():
                u.iam_roles = list(roles)
    logger.info(
        "webhook_user_roles_synced",
        extra={"iam_user_id": iam_user_id, "org_id": org_id, "roles": roles},
    )


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.post("/webhooks/iam", include_in_schema=True)
async def iam_webhook(request: Request) -> JSONResponse:
    """Receive and process IAM lifecycle events.

    Always returns ``{"received": true}`` with HTTP 200 on success.
    Returns HTTP 401 on HMAC failure, HTTP 500 on unexpected errors.

    Rate limiting (10 req/min per IP) is enforced by the global middleware
    layer — no per-endpoint logic required here.
    """
    # Read raw body before JSON parsing so we can verify the HMAC
    body: bytes = await request.body()

    # 1. Verify HMAC — raises AuthenticationError on failure (→ 401 via handler)
    await _verify_hmac(request, body)

    # 2. Parse JSON payload
    try:
        import json
        payload: dict = json.loads(body)
    except Exception:
        logger.warning(
            "webhook_invalid_json",
            extra={"request_id": getattr(request.state, "request_id", None)},
        )
        return JSONResponse(status_code=400, content={"error": {"code": "INVALID_PAYLOAD", "message": "Request body must be valid JSON"}})

    event: str = payload.get("event", "")
    product: str = (payload.get("product") or "").lower()
    org_id: str = payload.get("org_id", "")
    org_name: str = payload.get("org_name", "") or org_id  # fallback to org_id if name absent
    # User-lifecycle fields (present on user.* events)
    user_id: str = payload.get("user_id") or payload.get("sub") or ""
    user_roles: list = payload.get("roles") or []

    logger.info(
        "webhook_received",
        extra={
            "event": event,
            "org_id": org_id,
            "request_id": getattr(request.state, "request_id", None),
        },
    )

    # 3. Route event
    try:
        if event == "product.subscribed" and product == "itsm":
            await _provision_tenant(org_id, org_name)

        elif event == "product.unsubscribed" and product == "itsm":
            await _set_tenant_active(org_id, is_active=False)

        elif event == "organization.suspended":
            await _set_tenant_active(org_id, is_active=False)

        elif event == "organization.reactivated":
            await _set_tenant_active(org_id, is_active=True)

        # ---- User lifecycle — immediate de/re-provisioning -------------------
        elif event in ("user.deactivated", "user.suspended", "user.deleted",
                       "member.removed", "user.offboarded") and user_id:
            await _set_user_active(user_id, org_id, is_active=False)

        elif event in ("user.reactivated", "member.added", "user.restored") and user_id:
            await _set_user_active(user_id, org_id, is_active=True)

        elif event in ("user.role_changed", "user.updated", "user.roles_changed") and user_id:
            await _sync_user_roles(user_id, org_id, user_roles)

        else:
            # Unknown or irrelevant event — forward-compatible, silently ignore
            logger.info(
                "webhook_event_ignored",
                extra={"event": event, "org_id": org_id},
            )

    except IntegrityError as exc:
        # Concurrent duplicate delivery — treat as idempotent success
        logger.warning(
            "webhook_integrity_error",
            extra={
                "event": event,
                "org_id": org_id,
                "exception_type": type(exc).__name__,
            },
        )
        # Return 200 — IAM should not retry a duplicate
        return JSONResponse(status_code=200, content={"received": True})

    except Exception as exc:
        logger.error(
            "webhook_processing_error",
            extra={
                "event": event,
                "org_id": org_id,
                "exception_type": type(exc).__name__,
                "request_id": getattr(request.state, "request_id", None),
            },
        )
        # Return 500 so IAM retries the delivery
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

    return JSONResponse(status_code=200, content={"received": True})
