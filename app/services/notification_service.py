"""Notification service — write to DB first, then dispatch async via Celery.

Design rules:
- DB row is the source of truth. Never skip the DB write.
- Celery tasks handle email delivery and Socket.IO push — they run after the
  row is committed.
- tenant_id is always sourced from the validated JWT context, never from the
  request body.
- If no NotificationPreference row exists for a user+type, default = enabled.
"""

import asyncio
import logging
from uuid import UUID

from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import Notification, NotificationPreference, NotificationType

logger = logging.getLogger(__name__)


class NotificationService:
    """Async service for creating and managing in-app notifications."""

    # ------------------------------------------------------------------
    # send — single user
    # ------------------------------------------------------------------

    async def send(
        self,
        db: AsyncSession,
        tenant_id: UUID,
        user_id: UUID,
        type: NotificationType,
        title: str,
        body: str | None = None,
        entity_type: str | None = None,
        entity_id: UUID | None = None,
    ) -> Notification | None:
        """Create a notification for one user.

        Steps:
          1. Load NotificationPreference for this user+type.
             Default: both email_enabled and in_app_enabled are True if no row.
          2. If in_app_enabled: insert Notification row to DB.
          3. Enqueue Celery tasks for email (if email_enabled) and Socket.IO push.
          4. Return the Notification row (or None if in_app was disabled).

        The DB write always happens before Celery dispatch so that the inbox
        is consistent even if the broker is temporarily unavailable.
        """
        email_enabled, in_app_enabled = await self._get_preferences(db, user_id, type)

        notification: Notification | None = None

        if in_app_enabled:
            notification = Notification(
                tenant_id=tenant_id,
                user_id=user_id,
                type=type,
                title=title,
                body=body,
                entity_type=entity_type,
                entity_id=entity_id,
            )
            db.add(notification)
            await db.flush()

        # Dispatch Celery tasks after flushing (pre-commit) so the task
        # runner can read the row if needed.  Commit is the caller's
        # responsibility.
        if in_app_enabled and notification is not None:
            # Real-time push via Socket.IO (in-process, non-blocking)
            self._emit_realtime(
                tenant_id=tenant_id,
                user_id=user_id,
                notification=notification,
                type=type,
                title=title,
                body=body,
            )

            self._enqueue_socketio(
                tenant_id=tenant_id,
                user_id=user_id,
                notification_id=notification.id,
                type=type.value,
                title=title,
                body=body,
                entity_type=entity_type,
                entity_id=str(entity_id) if entity_id else None,
            )

        if email_enabled:
            await self._enqueue_email(db, user_id=user_id, type=type, title=title, body=body)

        return notification

    # ------------------------------------------------------------------
    # send_bulk — multiple users, same notification
    # ------------------------------------------------------------------

    async def send_bulk(
        self,
        db: AsyncSession,
        tenant_id: UUID,
        user_ids: list[UUID],
        type: NotificationType,
        title: str,
        body: str | None = None,
        entity_type: str | None = None,
        entity_id: UUID | None = None,
    ) -> None:
        """Send the same notification to multiple users via a bulk insert.

        Each user's preferences are respected individually.  A single
        executemany / bulk insert is used for in-app rows to minimise
        round-trips.
        """
        if not user_ids:
            return

        # Load all preferences for this type in one query
        pref_result = await db.execute(
            select(NotificationPreference).where(
                and_(
                    NotificationPreference.user_id.in_(user_ids),
                    NotificationPreference.type == type,
                )
            )
        )
        prefs_by_user: dict[UUID, NotificationPreference] = {
            p.user_id: p for p in pref_result.scalars().all()
        }

        in_app_rows: list[Notification] = []
        email_user_ids: list[UUID] = []

        for uid in user_ids:
            pref = prefs_by_user.get(uid)
            in_app_ok = pref.in_app_enabled if pref else True
            email_ok = pref.email_enabled if pref else True

            if in_app_ok:
                in_app_rows.append(
                    Notification(
                        tenant_id=tenant_id,
                        user_id=uid,
                        type=type,
                        title=title,
                        body=body,
                        entity_type=entity_type,
                        entity_id=entity_id,
                    )
                )
            if email_ok:
                email_user_ids.append(uid)

        if in_app_rows:
            db.add_all(in_app_rows)
            await db.flush()

            # Enqueue Socket.IO pushes
            for row in in_app_rows:
                self._enqueue_socketio(
                    tenant_id=tenant_id,
                    user_id=row.user_id,
                    notification_id=row.id,
                    type=type.value,
                    title=title,
                    body=body,
                    entity_type=entity_type,
                    entity_id=str(entity_id) if entity_id else None,
                )

        if email_user_ids:
            for uid in email_user_ids:
                await self._enqueue_email(db, user_id=uid, type=type, title=title, body=body)

        logger.info(
            "notifications_bulk_sent",
            extra={
                "tenant_id": str(tenant_id),
                "type": type.value,
                "in_app_count": len(in_app_rows),
                "email_count": len(email_user_ids),
            },
        )

    # ------------------------------------------------------------------
    # mark_read
    # ------------------------------------------------------------------

    async def mark_read(
        self,
        db: AsyncSession,
        user_id: UUID,
        notification_ids: list[UUID] | None = None,
    ) -> int:
        """Mark notifications as read for the given user.

        If notification_ids is None, marks ALL unread notifications.
        Returns the count of rows updated.
        """
        from sqlalchemy import func as sa_func

        filters = [
            Notification.user_id == user_id,
            Notification.is_read.is_(False),
        ]
        if notification_ids is not None:
            filters.append(Notification.id.in_(notification_ids))

        result = await db.execute(
            update(Notification)
            .where(and_(*filters))
            .values(is_read=True, read_at=sa_func.now())
            .returning(Notification.id)
        )
        rows = result.fetchall()
        count = len(rows)

        logger.info(
            "notifications_marked_read",
            extra={"user_id": str(user_id), "count": count},
        )
        return count

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _get_preferences(
        self,
        db: AsyncSession,
        user_id: UUID,
        type: NotificationType,
    ) -> tuple[bool, bool]:
        """Return (email_enabled, in_app_enabled) for user+type.

        Default both True if no row exists.
        """
        result = await db.execute(
            select(NotificationPreference).where(
                and_(
                    NotificationPreference.user_id == user_id,
                    NotificationPreference.type == type,
                )
            )
        )
        pref = result.scalar_one_or_none()
        if pref is None:
            return True, True
        return pref.email_enabled, pref.in_app_enabled

    def _emit_realtime(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        notification: "Notification",
        type: "NotificationType",
        title: str,
        body: str | None,
    ) -> None:
        """Fire-and-forget Socket.IO emit from within the async request context.

        Creates an asyncio Task so the emit does not block the caller.  Any
        exception inside the task is silently swallowed — the DB row is the
        source of truth.
        """
        try:
            from app.socketio_app import emit_notification  # local import — avoids circular dep at module load

            payload = {
                "id": str(notification.id),
                "type": type.value,
                "title": title,
                "body": body,
                "created_at": notification.created_at.isoformat() if notification.created_at else None,
            }

            # Schedule as a task so we never block the request handler
            asyncio.create_task(
                emit_notification(str(tenant_id), str(user_id), payload)
            )
        except Exception as exc:
            # Must never raise — DB notification already created
            logger.warning(
                "socketio_realtime_emit_failed",
                extra={"user_id": str(user_id), "error": str(exc)},
            )

    def _enqueue_socketio(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        notification_id: UUID,
        type: str,
        title: str,
        body: str | None,
        entity_type: str | None,
        entity_id: str | None,
    ) -> None:
        """Enqueue a Socket.IO push task without blocking the request thread."""
        try:
            from app.workers.tasks_notifications import push_socketio_notification

            push_socketio_notification.apply_async(
                args=[
                    str(tenant_id),
                    str(user_id),
                    {
                        "id": str(notification_id),
                        "type": type,
                        "title": title,
                        "body": body,
                        "entity_type": entity_type,
                        "entity_id": entity_id,
                    },
                ]
            )
        except Exception as exc:  # broker unreachable — degrade gracefully
            logger.warning(
                "socketio_enqueue_failed",
                extra={"user_id": str(user_id), "error": str(exc)},
            )

    async def _enqueue_email(
        self,
        db: AsyncSession,
        *,
        user_id: UUID,
        type: NotificationType,
        title: str,
        body: str | None,
    ) -> None:
        """Look up user email and enqueue the email delivery Celery task."""
        try:
            from app.models.identity import User
            from app.workers.tasks_notifications import send_email_notification

            result = await db.execute(
                select(User.email).where(User.id == user_id)
            )
            row = result.scalar_one_or_none()
            if row is None:
                return

            to_email: str = row

            # Map notification type to template name
            template_map = {
                NotificationType.ticket_assigned: "ticket_assigned",
                NotificationType.ticket_updated: "ticket_assigned",
                NotificationType.ticket_commented: "ticket_commented",
                NotificationType.ticket_sla_warning: "ticket_sla_warning",
                NotificationType.ticket_sla_breached: "ticket_sla_breach",
                NotificationType.asset_maintenance_due: "ticket_assigned",  # generic fallback
                NotificationType.kb_article_published: "kb_article_published",
                NotificationType.mention: "mention",
                NotificationType.system: "ticket_assigned",  # generic fallback
            }
            template_name = template_map.get(type, "ticket_assigned")

            send_email_notification.apply_async(
                args=[to_email, template_name, {"title": title, "body": body}]
            )
        except Exception as exc:
            logger.warning(
                "email_enqueue_failed",
                extra={"user_id": str(user_id), "error": str(exc)},
            )
