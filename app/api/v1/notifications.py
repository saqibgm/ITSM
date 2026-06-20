"""Notification endpoints — inbox, mark-read, preferences."""

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentUser, get_current_user
from app.database import get_db
from app.exceptions import AuthorizationError, ResourceNotFoundError
from app.models.notification import Notification, NotificationPreference, NotificationType
from app.schemas.common import PaginatedResponse
from app.schemas.notification import (
    MarkReadRequest,
    MarkReadResponse,
    NotificationPreferenceResponse,
    NotificationResponse,
    UpdateNotificationPreferenceRequest,
)
from app.services.notification_service import NotificationService

router = APIRouter(prefix="/notifications", tags=["notifications"])

_service = NotificationService()


# ---------------------------------------------------------------------------
# GET /notifications
# ---------------------------------------------------------------------------


@router.get("", response_model=PaginatedResponse[NotificationResponse])
async def list_notifications(
    unread: bool = Query(default=False, description="If true, return only unread notifications"),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None, description="Cursor-based pagination token (notification id)"),
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PaginatedResponse[NotificationResponse]:
    """Return paginated inbox notifications for the authenticated user.

    Scoped strictly to current_user.local_user_id — never a requested user_id.
    Ordered newest-first.
    """
    if current_user.local_user_id is None or current_user.tenant_id is None:
        raise AuthorizationError("Tenant context required")

    filters = [Notification.user_id == current_user.local_user_id]

    if unread:
        filters.append(Notification.is_read.is_(False))

    if cursor:
        try:
            cursor_uuid = UUID(cursor)
            # Cursor is the id of the last item seen; fetch older items
            filters.append(Notification.id < cursor_uuid)
        except ValueError:
            pass  # Invalid cursor — ignore and return from start

    result = await db.execute(
        select(Notification)
        .where(and_(*filters))
        .order_by(Notification.created_at.desc())
        .limit(limit + 1)
    )
    rows = list(result.scalars().all())

    has_more = len(rows) > limit
    items = rows[:limit]
    next_cursor = str(items[-1].id) if has_more and items else None

    return PaginatedResponse(
        items=[NotificationResponse.model_validate(n) for n in items],
        next_cursor=next_cursor,
    )


# ---------------------------------------------------------------------------
# PATCH /notifications/mark-read
# ---------------------------------------------------------------------------


@router.patch("/mark-read", response_model=MarkReadResponse)
async def mark_notifications_read(
    body: MarkReadRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MarkReadResponse:
    """Mark one or more notifications (or all) as read for the current user."""
    if current_user.local_user_id is None:
        raise AuthorizationError("Tenant context required")

    notification_ids: list[UUID] | None = None

    if not body.all:
        if body.ids is None:
            raise AuthorizationError("Provide either `ids` or `all: true`")
        notification_ids = body.ids

    count = await _service.mark_read(
        db=db,
        user_id=current_user.local_user_id,
        notification_ids=notification_ids,
    )
    await db.commit()
    return MarkReadResponse(marked=count)


# ---------------------------------------------------------------------------
# DELETE /notifications/{notification_id}
# ---------------------------------------------------------------------------


@router.delete("/{notification_id}", status_code=204)
async def delete_notification(
    notification_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a single notification — current user may only delete their own."""
    if current_user.local_user_id is None:
        raise AuthorizationError("Tenant context required")

    result = await db.execute(
        select(Notification).where(
            and_(
                Notification.id == notification_id,
                Notification.user_id == current_user.local_user_id,
            )
        )
    )
    notification = result.scalar_one_or_none()
    if notification is None:
        raise ResourceNotFoundError("notification", str(notification_id))

    await db.delete(notification)
    await db.commit()


# ---------------------------------------------------------------------------
# GET /notifications/preferences
# ---------------------------------------------------------------------------


@router.get("/preferences", response_model=list[NotificationPreferenceResponse])
async def get_notification_preferences(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[NotificationPreferenceResponse]:
    """Return the current user's notification preferences.

    Types with no explicit preference row are omitted — the caller should
    treat missing types as both email_enabled=True and in_app_enabled=True
    (the system default).
    """
    if current_user.local_user_id is None:
        raise AuthorizationError("Tenant context required")

    result = await db.execute(
        select(NotificationPreference).where(
            NotificationPreference.user_id == current_user.local_user_id
        )
    )
    prefs = list(result.scalars().all())
    return [NotificationPreferenceResponse.model_validate(p) for p in prefs]


# ---------------------------------------------------------------------------
# PATCH /notifications/preferences
# ---------------------------------------------------------------------------


@router.patch("/preferences", response_model=NotificationPreferenceResponse)
async def update_notification_preference(
    body: UpdateNotificationPreferenceRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NotificationPreferenceResponse:
    """Upsert a notification preference for the current user.

    tenant_id is always taken from the validated JWT — never from the request body.
    """
    if current_user.local_user_id is None or current_user.tenant_id is None:
        raise AuthorizationError("Tenant context required")

    result = await db.execute(
        select(NotificationPreference).where(
            and_(
                NotificationPreference.user_id == current_user.local_user_id,
                NotificationPreference.type == body.type,
            )
        )
    )
    pref = result.scalar_one_or_none()

    if pref is None:
        pref = NotificationPreference(
            user_id=current_user.local_user_id,
            tenant_id=current_user.tenant_id,
            type=body.type,
            email_enabled=body.email_enabled,
            in_app_enabled=body.in_app_enabled,
        )
        db.add(pref)
    else:
        pref.email_enabled = body.email_enabled
        pref.in_app_enabled = body.in_app_enabled
        db.add(pref)

    await db.commit()
    await db.refresh(pref)
    return NotificationPreferenceResponse.model_validate(pref)
