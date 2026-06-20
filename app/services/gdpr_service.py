"""
GDPR Data Export & Right-to-Erasure service (S6D).

Rules:
- Export:  collect ALL user data within tenant into a single structured dict.
- Erasure: anonymise PII in-place — never hard-delete records.
- PlatformAuditLog: INSERT-only; written for every erasure event.
- func.now() for all DB timestamp expressions; never Python datetime.now().
"""

import logging
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import ResourceNotFoundError
from app.models.identity import PlatformAuditLog, User, UserITSMRole, ITSMRole
from app.models.ticket import Ticket, TicketComment
from app.models.asset import Asset
from app.models.kb import KBArticle, KBArticleFeedback
from app.models.virtual_agent import VirtualAgentSession, VirtualAgentMessage
from app.models.notification import Notification

logger = logging.getLogger(__name__)


def _uuid_str(val) -> str:
    """Safely stringify a UUID or None."""
    return str(val) if val is not None else None


def _row_to_dict(row, fields: list[str]) -> dict:
    """Extract only the listed attribute names from an ORM row."""
    return {f: _uuid_str(getattr(row, f)) if isinstance(getattr(row, f), UUID) else getattr(row, f)
            for f in fields}


class GDPRService:
    """Application service for GDPR export and erasure operations."""

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    async def export_user_data(
        self,
        db: AsyncSession,
        user_id: str,
        tenant_id: str,
    ) -> dict:
        """Collect ALL data for *user_id* within *tenant_id*.

        Returns a structured dict suitable for JSON serialisation via
        ``json.dumps(..., default=str)``.
        """
        uid = UUID(user_id)
        tid = UUID(tenant_id)

        # ---- User profile ------------------------------------------------
        user_result = await db.execute(
            select(User).where(User.id == uid, User.tenant_id == tid)
        )
        user: User | None = user_result.scalar_one_or_none()

        # Roles
        roles_result = await db.execute(
            select(ITSMRole.name)
            .join(UserITSMRole, UserITSMRole.role_id == ITSMRole.id)
            .where(UserITSMRole.user_id == uid, UserITSMRole.tenant_id == tid)
        )
        role_names = [r[0] for r in roles_result.all()]

        user_section: dict = {}
        if user:
            user_section = {
                "id": str(user.id),
                "email": user.email,
                "first_name": user.first_name,
                "last_name": user.last_name,
                "is_active": user.is_active,
                "created_at": user.created_at,
                "roles": role_names,
            }

        # ---- Tickets created by user -------------------------------------
        tickets_result = await db.execute(
            select(Ticket).where(
                Ticket.requester_id == uid,
                Ticket.tenant_id == tid,
            )
        )
        tickets = tickets_result.scalars().all()
        tickets_section = [
            {
                "id": str(t.id),
                "ticket_number": t.ticket_number,
                "type": t.type.value if t.type else None,
                "status": t.status.value if t.status else None,
                "priority": t.priority.value if t.priority else None,
                "title": t.title,
                "description": t.description,
                "created_at": t.created_at,
                "resolved_at": t.resolved_at,
                "closed_at": t.closed_at,
            }
            for t in tickets
        ]

        # ---- Ticket comments authored by user ----------------------------
        comments_result = await db.execute(
            select(TicketComment).where(
                TicketComment.author_id == uid,
                TicketComment.ticket_id.in_(
                    select(Ticket.id).where(Ticket.tenant_id == tid)
                ),
            )
        )
        comments = comments_result.scalars().all()
        comments_section = [
            {
                "id": str(c.id),
                "ticket_id": str(c.ticket_id),
                "body": c.body,
                "is_internal": c.is_internal,
                "created_at": c.created_at,
                "deleted_at": c.deleted_at,
            }
            for c in comments
        ]

        # ---- Assets assigned to user -------------------------------------
        assets_result = await db.execute(
            select(Asset).where(
                Asset.assigned_to == uid,
                Asset.tenant_id == tid,
            )
        )
        assets = assets_result.scalars().all()
        assets_section = [
            {
                "id": str(a.id),
                "asset_tag": a.asset_tag,
                "name": a.name,
                "status": a.status.value if a.status else None,
                "condition": a.condition.value if a.condition else None,
                "assigned_at": a.assigned_at,
            }
            for a in assets
        ]

        # ---- KB articles authored by user --------------------------------
        kb_result = await db.execute(
            select(KBArticle).where(
                KBArticle.author_id == uid,
                KBArticle.tenant_id == tid,
            )
        )
        kb_articles = kb_result.scalars().all()
        kb_section = [
            {
                "id": str(a.id),
                "title": a.title,
                "slug": a.slug,
                "status": a.status.value if a.status else None,
                "visibility": a.visibility.value if a.visibility else None,
                "created_at": a.created_at,
                "published_at": a.published_at,
            }
            for a in kb_articles
        ]

        # ---- KB article feedback by user ---------------------------------
        # KBArticleFeedback has no tenant_id; filter via article
        kb_feedback_result = await db.execute(
            select(KBArticleFeedback).where(
                KBArticleFeedback.user_id == uid,
                KBArticleFeedback.article_id.in_(
                    select(KBArticle.id).where(KBArticle.tenant_id == tid)
                ),
            )
        )
        kb_feedback = kb_feedback_result.scalars().all()
        kb_feedback_section = [
            {
                "id": str(f.id),
                "article_id": str(f.article_id),
                "is_helpful": f.is_helpful,
                "comment": f.comment,
                "created_at": f.created_at,
            }
            for f in kb_feedback
        ]

        # ---- Virtual agent sessions --------------------------------------
        sessions_result = await db.execute(
            select(VirtualAgentSession).where(
                VirtualAgentSession.user_id == uid,
                VirtualAgentSession.tenant_id == tid,
            )
        )
        sessions = sessions_result.scalars().all()
        sessions_section = []
        for s in sessions:
            # Fetch messages for this session
            msgs_result = await db.execute(
                select(VirtualAgentMessage).where(
                    VirtualAgentMessage.session_id == s.id
                )
            )
            msgs = msgs_result.scalars().all()
            sessions_section.append(
                {
                    "id": str(s.id),
                    "channel": s.channel,
                    "status": s.status,
                    "intent": s.intent,
                    "created_at": s.created_at,
                    "ended_at": s.ended_at,
                    "messages": [
                        {
                            "id": str(m.id),
                            "role": m.role,
                            "content": m.content,
                            "created_at": m.created_at,
                        }
                        for m in msgs
                    ],
                }
            )

        # ---- Notifications -----------------------------------------------
        notifs_result = await db.execute(
            select(Notification).where(
                Notification.user_id == uid,
                Notification.tenant_id == tid,
            )
        )
        notifs = notifs_result.scalars().all()
        notifs_section = [
            {
                "id": str(n.id),
                "type": n.type.value if n.type else None,
                "title": n.title,
                "body": n.body,
                "entity_type": n.entity_type,
                "entity_id": str(n.entity_id) if n.entity_id else None,
                "is_read": n.is_read,
                "read_at": n.read_at,
                "created_at": n.created_at,
            }
            for n in notifs
        ]

        return {
            "export_date": datetime.now(timezone.utc).isoformat(),
            "user": user_section,
            "tickets_created": tickets_section,
            "ticket_comments": comments_section,
            "assets_assigned": assets_section,
            "kb_articles_authored": kb_section,
            "kb_feedback": kb_feedback_section,
            "virtual_agent_sessions": sessions_section,
            "notifications": notifs_section,
        }

    # ------------------------------------------------------------------
    # Erasure
    # ------------------------------------------------------------------

    async def erase_user_data(
        self,
        db: AsyncSession,
        user_id: str,
        tenant_id: str,
        requested_by_id: str,
    ) -> dict:
        """Anonymise all PII for *user_id* within *tenant_id*.

        Never hard-deletes; preserves tickets, history, and audit logs.
        Returns a summary dict with affected row counts.
        """
        uid = UUID(user_id)
        tid = UUID(tenant_id)

        # 1. Anonymise the user row ----------------------------------------
        # Build the anonymised email from the first 8 chars of the UUID string
        # using a pure-SQL concat so func.now() and text ops stay server-side.
        uid_prefix = str(uid)[:8]
        anon_email = f"deleted_{uid_prefix}@gdpr.invalid"
        user_result = await db.execute(
            update(User)
            .where(User.id == uid, User.tenant_id == tid)
            .values(
                email=anon_email,
                first_name="Deleted",
                last_name="User",
                is_active=False,
                updated_at=func.now(),
            )
            .returning(User.id)
        )
        users_affected = len(user_result.all())
        if users_affected == 0:
            raise ResourceNotFoundError("user", str(uid))

        # 2. Anonymise non-internal ticket comments ------------------------
        #    Scope: comments on tickets that belong to this tenant.
        tenant_ticket_ids_subq = (
            select(Ticket.id).where(Ticket.tenant_id == tid).scalar_subquery()
        )
        comments_result = await db.execute(
            update(TicketComment)
            .where(
                TicketComment.author_id == uid,
                TicketComment.ticket_id.in_(tenant_ticket_ids_subq),
                TicketComment.is_internal == False,  # noqa: E712
            )
            .values(body="[Content removed per GDPR erasure request]")
            .returning(TicketComment.id)
        )
        comments_affected = len(comments_result.all())

        # 3. Anonymise virtual agent messages ------------------------------
        session_ids_subq = (
            select(VirtualAgentSession.id)
            .where(
                VirtualAgentSession.user_id == uid,
                VirtualAgentSession.tenant_id == tid,
            )
            .scalar_subquery()
        )
        messages_result = await db.execute(
            update(VirtualAgentMessage)
            .where(VirtualAgentMessage.session_id.in_(session_ids_subq))
            .values(content="[Content removed per GDPR erasure request]")
            .returning(VirtualAgentMessage.id)
        )
        messages_affected = len(messages_result.all())

        # 4. Remove free-text from KB feedback (keep vote) -----------------
        feedback_result = await db.execute(
            update(KBArticleFeedback)
            .where(KBArticleFeedback.user_id == uid)
            .values(comment=None)
            .returning(KBArticleFeedback.id)
        )
        feedback_affected = len(feedback_result.all())

        # 5. Write immutable audit log -------------------------------------
        anonymised_at = datetime.now(timezone.utc)
        audit_entry = PlatformAuditLog(
            actor_id=str(requested_by_id),
            actor_email="system@gdpr.internal",
            platform_role="gdpr_erasure",
            tenant_id=tid,
            http_method="POST",
            endpoint="/api/v1/gdpr/erasure-request",
            request_id="",
            ip_address="",
            action="gdpr_erasure",
            details={
                "erased_user_id": str(uid),
                "items_affected": {
                    "users": users_affected,
                    "ticket_comments": comments_affected,
                    "virtual_agent_messages": messages_affected,
                    "kb_feedback": feedback_affected,
                },
            },
        )
        db.add(audit_entry)

        logger.info(
            "gdpr_erasure_completed",
            extra={
                "erased_user_id": str(uid),
                "tenant_id": str(tid),
                "requested_by_id": str(requested_by_id),
            },
        )

        return {
            "erased_user_id": str(uid),
            "anonymised_at": anonymised_at.isoformat(),
            "items_affected": {
                "users": users_affected,
                "ticket_comments": comments_affected,
                "virtual_agent_messages": messages_affected,
                "kb_feedback": feedback_affected,
            },
        }

    # ------------------------------------------------------------------
    # Erasure history
    # ------------------------------------------------------------------

    async def list_erasure_requests(
        self,
        db: AsyncSession,
        tenant_id: str,
        page: int = 1,
        page_size: int = 50,
    ) -> dict:
        """Return paginated GDPR erasure audit log entries for *tenant_id*."""
        tid = UUID(tenant_id)
        offset = (page - 1) * page_size

        # Total count
        count_result = await db.execute(
            select(func.count(PlatformAuditLog.id)).where(
                PlatformAuditLog.action == "gdpr_erasure",
                PlatformAuditLog.tenant_id == tid,
            )
        )
        total = count_result.scalar_one()

        # Page of rows
        rows_result = await db.execute(
            select(PlatformAuditLog)
            .where(
                PlatformAuditLog.action == "gdpr_erasure",
                PlatformAuditLog.tenant_id == tid,
            )
            .order_by(PlatformAuditLog.accessed_at.desc())
            .offset(offset)
            .limit(page_size)
        )
        rows = rows_result.scalars().all()

        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "items": [
                {
                    "id": str(r.id),
                    "actor_id": r.actor_id,
                    "actor_email": r.actor_email,
                    "details": r.details,
                    "accessed_at": r.accessed_at,
                }
                for r in rows
            ],
        }
