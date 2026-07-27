"""Repository for Ticket and related entities."""

import base64
from datetime import datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import ResourceNotFoundError
from app.models.ticket import (
    Ticket,
    TicketHistory,
    TicketPriority,
    TicketStatus,
    TicketStatusTransition,
    TicketType,
    TicketWatcher,
)
from app.repositories.base import BaseRepository


class TicketRepository(BaseRepository[Ticket]):
    model = Ticket

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    async def get(self, id: UUID, tenant_id: UUID | None) -> Ticket | None:
        """Fetch a ticket, excluding soft-deleted rows (deleted_at IS NOT NULL)."""
        q = select(Ticket).where(Ticket.id == id, Ticket.deleted_at.is_(None))
        if tenant_id is not None:
            q = q.where(Ticket.tenant_id == tenant_id)
        result = await self.session.execute(q)
        return result.scalar_one_or_none()

    async def get_by_number(self, ticket_number: str, tenant_id: UUID | None) -> Ticket | None:
        """Resolve a human-facing ticket number (globally unique, see
        ticket_number_seq) to the full ticket row."""
        q = select(Ticket).where(
            Ticket.ticket_number == ticket_number, Ticket.deleted_at.is_(None)
        )
        if tenant_id is not None:
            q = q.where(Ticket.tenant_id == tenant_id)
        result = await self.session.execute(q)
        return result.scalar_one_or_none()

    # ------------------------------------------------------------------
    # List with cursor-based pagination
    # ------------------------------------------------------------------

    async def list_tickets(
        self,
        tenant_id: UUID | None,
        *,
        status: list[TicketStatus] | None = None,
        priority: list[TicketPriority] | None = None,
        type: list[TicketType] | None = None,
        assignee_id: UUID | None = None,
        requester_id: UUID | None = None,
        team_id: UUID | None = None,
        category_id: UUID | None = None,
        product_id: UUID | None = None,
        search: str | None = None,
        cursor: str | None = None,
        limit: int = 25,
    ) -> tuple[list[Ticket], str | None]:
        q = select(Ticket).where(Ticket.deleted_at.is_(None))
        if tenant_id is not None:
            q = q.where(Ticket.tenant_id == tenant_id)

        if status:
            q = q.where(Ticket.status.in_([s.value for s in status]))
        if priority:
            q = q.where(Ticket.priority.in_([p.value for p in priority]))
        if type:
            q = q.where(Ticket.type.in_([t.value for t in type]))
        if assignee_id is not None:
            q = q.where(Ticket.assignee_id == assignee_id)
        if requester_id is not None:
            q = q.where(Ticket.requester_id == requester_id)
        if team_id is not None:
            q = q.where(Ticket.team_id == team_id)
        if category_id is not None:
            q = q.where(Ticket.category_id == category_id)
        if product_id is not None:
            q = q.where(Ticket.product_id == product_id)
        if search:
            fts_query = func.plainto_tsquery("english", search)
            q = q.where(
                func.to_tsvector("english", Ticket.title + " " + Ticket.description).op("@@")(
                    fts_query
                )
            )

        if cursor:
            try:
                decoded = base64.b64decode(cursor.encode()).decode()
                cursor_created_at_str, cursor_id_str = decoded.split(":", 1)
                cursor_created_at = datetime.fromisoformat(cursor_created_at_str)
                cursor_id = UUID(cursor_id_str)
                q = q.where(
                    sa.or_(
                        Ticket.created_at < cursor_created_at,
                        sa.and_(
                            Ticket.created_at == cursor_created_at,
                            Ticket.id < cursor_id,
                        ),
                    )
                )
            except Exception:
                pass

        q = q.order_by(Ticket.created_at.desc(), Ticket.id.desc()).limit(limit + 1)

        result = await self.session.execute(q)
        rows = list(result.scalars().all())

        next_cursor: str | None = None
        if len(rows) > limit:
            rows = rows[:limit]
            last = rows[-1]
            raw = f"{last.created_at.isoformat()}:{last.id}"
            next_cursor = base64.b64encode(raw.encode()).decode()

        return rows, next_cursor

    # ------------------------------------------------------------------
    # State machine
    # ------------------------------------------------------------------

    async def get_valid_transitions(
        self, from_status: TicketStatus, ticket_type: TicketType
    ) -> list[str]:
        result = await self.session.execute(
            select(TicketStatusTransition.to_status).where(
                TicketStatusTransition.from_status == from_status.value,
                sa.or_(
                    TicketStatusTransition.ticket_type == "",
                    TicketStatusTransition.ticket_type == ticket_type.value,
                ),
            )
        )
        return [row[0] for row in result.all()]

    # ------------------------------------------------------------------
    # Ticket numbering
    # ------------------------------------------------------------------

    async def next_ticket_number(self) -> str:
        """Return the next system-wide ticket number, e.g. ``00001``.

        Backed by the global ``ticket_number_seq`` sequence (migration 0017),
        so numbers are unique across all tenants and have no type prefix.
        nextval is atomic/concurrency-safe; numbers may have gaps on rollback,
        which is fine for human-facing references.
        """
        value = await self.session.scalar(sa.text("SELECT nextval('ticket_number_seq')"))
        return f"{int(value):05d}"

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------

    async def record_history(
        self,
        ticket_id: UUID,
        actor_id: UUID | None,
        field: str,
        old_val: Any,
        new_val: Any,
    ) -> None:
        entry = TicketHistory(
            ticket_id=ticket_id,
            actor_id=actor_id,
            field_changed=field,
            old_value=old_val if old_val is None else {"v": old_val},
            new_value=new_val if new_val is None else {"v": new_val},
        )
        self.session.add(entry)
        await self.session.flush()

    # ------------------------------------------------------------------
    # Watcher helpers
    # ------------------------------------------------------------------

    async def add_watcher(self, ticket_id: UUID, user_id: UUID) -> None:
        existing = await self.session.execute(
            select(TicketWatcher).where(
                TicketWatcher.ticket_id == ticket_id,
                TicketWatcher.user_id == user_id,
            )
        )
        if existing.scalar_one_or_none() is None:
            self.session.add(TicketWatcher(ticket_id=ticket_id, user_id=user_id))
            await self.session.flush()
