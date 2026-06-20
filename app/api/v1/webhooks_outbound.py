"""Outbound Webhook Configuration API (S6B).

Routes (prefix /webhooks-config):
  GET    /endpoints                    list endpoints; paginated
  POST   /endpoints                    create endpoint; admin
  GET    /endpoints/{id}               detail; secret masked
  PATCH  /endpoints/{id}               update endpoint; admin
  DELETE /endpoints/{id}               hard delete; admin
  POST   /endpoints/{id}/test          send test ping delivery
  POST   /endpoints/{id}/enable        re-enable after auto-disable; admin
  GET    /endpoints/{id}/deliveries    delivery log; newest first; paginated
  GET    /deliveries/{delivery_id}     single delivery detail

Security:
  - tenant_id always sourced from JWT
  - secret never returned in GET responses (masked as "*****")
  - URL must start with http:// or https://
"""

import base64
import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentUser, get_current_user, require_role
from app.database import get_db
from app.exceptions import AuthorizationError, ResourceNotFoundError, ValidationError
from app.models.webhook import WebhookDelivery, WebhookEndpoint
from app.schemas.common import PaginatedResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks-config", tags=["webhooks-config"])

_ADMIN_ROLES = ("admin", "tenant_admin")
_READ_ROLES = ("admin", "tenant_admin", "agent", "team_lead", "manager")

_SECRET_MASK = "*****"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_tenant(current_user: CurrentUser) -> None:
    if current_user.tenant_id is None:
        raise AuthorizationError("Tenant context required")


def _require_admin(current_user: CurrentUser) -> None:
    _require_tenant(current_user)
    if not set(current_user.roles) & set(_ADMIN_ROLES):
        raise AuthorizationError("admin role is required")


def _encode_cursor(value: str) -> str:
    return base64.b64encode(value.encode()).decode()


def _decode_cursor(cursor: str) -> str:
    try:
        return base64.b64decode(cursor.encode()).decode()
    except Exception:
        return ""


async def _get_endpoint_or_404(
    db: AsyncSession,
    endpoint_id: UUID,
    tenant_id: UUID,
) -> WebhookEndpoint:
    result = await db.execute(
        select(WebhookEndpoint).where(
            WebhookEndpoint.id == endpoint_id,
            WebhookEndpoint.tenant_id == tenant_id,
        )
    )
    endpoint = result.scalar_one_or_none()
    if endpoint is None:
        raise ResourceNotFoundError("webhook_endpoint", str(endpoint_id))
    return endpoint


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class CreateWebhookEndpointRequest(BaseModel):
    name: str = Field(..., max_length=255)
    url: str
    secret: str = Field(..., min_length=16)
    events: list[str] = Field(default_factory=list)

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("URL must be http or https")
        return v


class UpdateWebhookEndpointRequest(BaseModel):
    name: Optional[str] = Field(default=None, max_length=255)
    url: Optional[str] = None
    secret: Optional[str] = Field(default=None, min_length=16)
    events: Optional[list[str]] = None
    is_active: Optional[bool] = None

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("URL must be http or https")
        return v


class WebhookEndpointResponse(BaseModel):
    id: UUID
    tenant_id: UUID
    name: str
    url: str
    secret: str
    events: list
    is_active: bool
    failure_count: int
    last_success_at: Optional[datetime]
    last_failure_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

    @classmethod
    def from_orm_masked(cls, endpoint: WebhookEndpoint) -> "WebhookEndpointResponse":
        """Build response, masking the HMAC secret."""
        obj = cls.model_validate(endpoint)
        obj.secret = _SECRET_MASK
        return obj


class WebhookDeliveryResponse(BaseModel):
    id: UUID
    endpoint_id: UUID
    tenant_id: UUID
    event_type: str
    payload: dict
    status: str
    attempt_count: int
    response_status_code: Optional[int]
    response_body: Optional[str]
    error_message: Optional[str]
    delivered_at: Optional[datetime]
    next_retry_at: Optional[datetime]
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# GET /webhooks-config/endpoints
# ---------------------------------------------------------------------------


@router.get("/endpoints", response_model=PaginatedResponse[WebhookEndpointResponse])
async def list_endpoints(
    cursor: Optional[str] = Query(default=None),
    page_size: int = Query(default=25, ge=1, le=100, alias="page_size"),
    current_user: CurrentUser = Depends(require_role(*_READ_ROLES)),
    db: AsyncSession = Depends(get_db),
) -> PaginatedResponse[WebhookEndpointResponse]:
    _require_tenant(current_user)
    tenant_id = current_user.tenant_id

    query = select(WebhookEndpoint).where(WebhookEndpoint.tenant_id == tenant_id)

    if cursor:
        decoded = _decode_cursor(cursor)
        if decoded:
            query = query.where(WebhookEndpoint.id > decoded)

    query = query.order_by(WebhookEndpoint.created_at.asc(), WebhookEndpoint.id.asc()).limit(
        page_size + 1
    )

    result = await db.execute(query)
    endpoints = list(result.scalars().all())

    next_cursor: Optional[str] = None
    if len(endpoints) > page_size:
        endpoints = endpoints[:page_size]
        next_cursor = _encode_cursor(str(endpoints[-1].id))

    return PaginatedResponse(
        items=[WebhookEndpointResponse.from_orm_masked(ep) for ep in endpoints],
        next_cursor=next_cursor,
        total=None,
    )


# ---------------------------------------------------------------------------
# POST /webhooks-config/endpoints
# ---------------------------------------------------------------------------


@router.post("/endpoints", response_model=WebhookEndpointResponse, status_code=201)
async def create_endpoint(
    body: CreateWebhookEndpointRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> WebhookEndpointResponse:
    _require_admin(current_user)

    endpoint = WebhookEndpoint(
        tenant_id=current_user.tenant_id,
        name=body.name,
        url=body.url,
        secret=body.secret,
        events=body.events,
        is_active=True,
        failure_count=0,
    )
    db.add(endpoint)
    await db.flush()
    await db.refresh(endpoint)
    return WebhookEndpointResponse.from_orm_masked(endpoint)


# ---------------------------------------------------------------------------
# GET /webhooks-config/endpoints/{endpoint_id}
# ---------------------------------------------------------------------------


@router.get("/endpoints/{endpoint_id}", response_model=WebhookEndpointResponse)
async def get_endpoint(
    endpoint_id: UUID,
    current_user: CurrentUser = Depends(require_role(*_READ_ROLES)),
    db: AsyncSession = Depends(get_db),
) -> WebhookEndpointResponse:
    _require_tenant(current_user)
    endpoint = await _get_endpoint_or_404(db, endpoint_id, current_user.tenant_id)
    return WebhookEndpointResponse.from_orm_masked(endpoint)


# ---------------------------------------------------------------------------
# PATCH /webhooks-config/endpoints/{endpoint_id}
# ---------------------------------------------------------------------------


@router.patch("/endpoints/{endpoint_id}", response_model=WebhookEndpointResponse)
async def update_endpoint(
    endpoint_id: UUID,
    body: UpdateWebhookEndpointRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> WebhookEndpointResponse:
    _require_admin(current_user)
    endpoint = await _get_endpoint_or_404(db, endpoint_id, current_user.tenant_id)

    if body.name is not None:
        endpoint.name = body.name
    if body.url is not None:
        endpoint.url = body.url
    if body.secret is not None:
        endpoint.secret = body.secret
    if body.events is not None:
        endpoint.events = body.events
    if body.is_active is not None:
        endpoint.is_active = body.is_active

    db.add(endpoint)
    await db.flush()
    await db.refresh(endpoint)
    return WebhookEndpointResponse.from_orm_masked(endpoint)


# ---------------------------------------------------------------------------
# DELETE /webhooks-config/endpoints/{endpoint_id}
# ---------------------------------------------------------------------------


@router.delete("/endpoints/{endpoint_id}", status_code=204)
async def delete_endpoint(
    endpoint_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    _require_admin(current_user)
    endpoint = await _get_endpoint_or_404(db, endpoint_id, current_user.tenant_id)
    await db.delete(endpoint)
    await db.flush()


# ---------------------------------------------------------------------------
# POST /webhooks-config/endpoints/{endpoint_id}/test
# ---------------------------------------------------------------------------


@router.post("/endpoints/{endpoint_id}/test", response_model=WebhookDeliveryResponse, status_code=202)
async def test_endpoint(
    endpoint_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> WebhookDeliveryResponse:
    """Send a test ping delivery to the endpoint."""
    _require_admin(current_user)
    endpoint = await _get_endpoint_or_404(db, endpoint_id, current_user.tenant_id)

    test_payload: dict = {"test": True, "timestamp": datetime.now(timezone.utc).isoformat()}

    delivery = WebhookDelivery(
        endpoint_id=endpoint.id,
        tenant_id=endpoint.tenant_id,
        event_type="ping",
        payload=test_payload,
        status="pending",
        attempt_count=0,
    )
    db.add(delivery)
    await db.flush()
    await db.refresh(delivery)

    # Enqueue the Celery task — errors must not propagate
    try:
        from app.workers.tasks_webhooks import deliver_webhook
        deliver_webhook.delay(str(delivery.id))
    except Exception:
        pass

    return WebhookDeliveryResponse.model_validate(delivery)


# ---------------------------------------------------------------------------
# POST /webhooks-config/endpoints/{endpoint_id}/enable
# ---------------------------------------------------------------------------


@router.post("/endpoints/{endpoint_id}/enable", response_model=WebhookEndpointResponse)
async def enable_endpoint(
    endpoint_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> WebhookEndpointResponse:
    """Re-enable an auto-disabled endpoint; resets failure_count to 0."""
    _require_admin(current_user)
    endpoint = await _get_endpoint_or_404(db, endpoint_id, current_user.tenant_id)

    endpoint.is_active = True
    endpoint.failure_count = 0

    db.add(endpoint)
    await db.flush()
    await db.refresh(endpoint)

    logger.info(
        "webhook_endpoint_re_enabled",
        extra={"endpoint_id": str(endpoint.id), "tenant_id": str(endpoint.tenant_id)},
    )
    return WebhookEndpointResponse.from_orm_masked(endpoint)


# ---------------------------------------------------------------------------
# GET /webhooks-config/endpoints/{endpoint_id}/deliveries
# ---------------------------------------------------------------------------


@router.get(
    "/endpoints/{endpoint_id}/deliveries",
    response_model=PaginatedResponse[WebhookDeliveryResponse],
)
async def list_deliveries(
    endpoint_id: UUID,
    cursor: Optional[str] = Query(default=None),
    page_size: int = Query(default=25, ge=1, le=100, alias="page_size"),
    current_user: CurrentUser = Depends(require_role(*_READ_ROLES)),
    db: AsyncSession = Depends(get_db),
) -> PaginatedResponse[WebhookDeliveryResponse]:
    _require_tenant(current_user)
    # Verify ownership
    await _get_endpoint_or_404(db, endpoint_id, current_user.tenant_id)

    query = select(WebhookDelivery).where(
        and_(
            WebhookDelivery.endpoint_id == endpoint_id,
            WebhookDelivery.tenant_id == current_user.tenant_id,
        )
    )

    if cursor:
        decoded = _decode_cursor(cursor)
        if decoded:
            # Newest-first: cursor points to the last seen id; next page has older rows
            query = query.where(WebhookDelivery.id < decoded)

    query = query.order_by(WebhookDelivery.created_at.desc(), WebhookDelivery.id.desc()).limit(
        page_size + 1
    )

    result = await db.execute(query)
    deliveries = list(result.scalars().all())

    next_cursor: Optional[str] = None
    if len(deliveries) > page_size:
        deliveries = deliveries[:page_size]
        next_cursor = _encode_cursor(str(deliveries[-1].id))

    return PaginatedResponse(
        items=[WebhookDeliveryResponse.model_validate(d) for d in deliveries],
        next_cursor=next_cursor,
        total=None,
    )


# ---------------------------------------------------------------------------
# GET /webhooks-config/deliveries/{delivery_id}
# ---------------------------------------------------------------------------


@router.get("/deliveries/{delivery_id}", response_model=WebhookDeliveryResponse)
async def get_delivery(
    delivery_id: UUID,
    current_user: CurrentUser = Depends(require_role(*_READ_ROLES)),
    db: AsyncSession = Depends(get_db),
) -> WebhookDeliveryResponse:
    _require_tenant(current_user)
    result = await db.execute(
        select(WebhookDelivery).where(
            and_(
                WebhookDelivery.id == delivery_id,
                WebhookDelivery.tenant_id == current_user.tenant_id,
            )
        )
    )
    delivery = result.scalar_one_or_none()
    if delivery is None:
        raise ResourceNotFoundError("webhook_delivery", str(delivery_id))
    return WebhookDeliveryResponse.model_validate(delivery)
