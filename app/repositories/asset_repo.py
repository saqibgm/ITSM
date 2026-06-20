"""Repository for Asset and related entities."""

import base64
from datetime import date, datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import ResourceNotFoundError
from app.models.asset import (
    Asset,
    AssetCondition,
    AssetHistory,
    AssetHistoryAction,
    AssetStatus,
    AssetStatusTransition,
)
from app.models.identity import TenantSequence
from app.repositories.base import BaseRepository

_AST_PREFIX = "AST"


class AssetRepository(BaseRepository[Asset]):
    model = Asset

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    # ------------------------------------------------------------------
    # get override — exclude soft-deleted by default
    # ------------------------------------------------------------------

    async def get(self, id: UUID, tenant_id: UUID) -> Asset | None:
        result = await self.session.execute(
            select(Asset).where(
                Asset.id == id,
                Asset.tenant_id == tenant_id,
                Asset.deleted_at.is_(None),
            )
        )
        return result.scalar_one_or_none()

    async def get_or_404(self, id: UUID, tenant_id: UUID) -> Asset:
        obj = await self.get(id, tenant_id)
        if obj is None:
            raise ResourceNotFoundError("assets", str(id))
        return obj

    # ------------------------------------------------------------------
    # List with cursor-based pagination
    # ------------------------------------------------------------------

    async def list_assets(
        self,
        tenant_id: UUID | None,
        *,
        status: list[AssetStatus] | None = None,
        condition: list[AssetCondition] | None = None,
        type_id: UUID | None = None,
        category_id: UUID | None = None,
        department_id: UUID | None = None,
        assigned_to: UUID | None = None,
        vendor_id: UUID | None = None,
        search: str | None = None,
        warranty_expiring_before: date | None = None,
        include_deleted: bool = False,
        cursor: str | None = None,
        limit: int = 25,
    ) -> tuple[list[Asset], str | None]:
        """Return a page of assets for the tenant.

        Supports ILIKE search across name, asset_tag, serial_number, model_number.
        Soft-deleted assets are excluded by default.
        """
        from app.models.asset import AssetType  # local import to avoid circular

        q = select(Asset)
        if tenant_id is not None:
            q = q.where(Asset.tenant_id == tenant_id)

        if not include_deleted:
            q = q.where(Asset.deleted_at.is_(None))

        if status:
            q = q.where(Asset.status.in_([s.value for s in status]))

        if condition:
            q = q.where(Asset.condition.in_([c.value for c in condition]))

        if type_id is not None:
            q = q.where(Asset.type_id == type_id)

        if category_id is not None:
            q = q.join(AssetType, AssetType.id == Asset.type_id).where(
                AssetType.category_id == category_id
            )

        if department_id is not None:
            q = q.where(Asset.department_id == department_id)

        if assigned_to is not None:
            q = q.where(Asset.assigned_to == assigned_to)

        if vendor_id is not None:
            q = q.where(Asset.vendor_id == vendor_id)

        if search:
            pattern = f"%{search}%"
            q = q.where(
                sa.or_(
                    Asset.name.ilike(pattern),
                    Asset.asset_tag.ilike(pattern),
                    Asset.serial_number.ilike(pattern),
                    Asset.model_number.ilike(pattern),
                )
            )

        if warranty_expiring_before is not None:
            q = q.where(
                Asset.warranty_expiry_date.isnot(None),
                Asset.warranty_expiry_date <= warranty_expiring_before,
            )

        if cursor:
            try:
                decoded = base64.b64decode(cursor.encode()).decode()
                cursor_created_at_str, cursor_id_str = decoded.split(":", 1)
                cursor_created_at = datetime.fromisoformat(cursor_created_at_str)
                cursor_id = UUID(cursor_id_str)
                q = q.where(
                    sa.or_(
                        Asset.created_at < cursor_created_at,
                        sa.and_(
                            Asset.created_at == cursor_created_at,
                            Asset.id < cursor_id,
                        ),
                    )
                )
            except Exception:
                pass  # ignore malformed cursors — return from the beginning

        q = q.order_by(Asset.created_at.desc(), Asset.id.desc()).limit(limit + 1)

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
    # Asset tag sequencing
    # ------------------------------------------------------------------

    async def next_asset_tag(self, tenant_id: UUID) -> str:
        """Atomically increment the AST sequence and return the next tag.

        Uses SELECT FOR UPDATE on tenant_sequences to prevent duplicate tags
        under concurrent requests.
        """
        result = await self.session.execute(
            select(TenantSequence)
            .where(
                TenantSequence.tenant_id == tenant_id,
                TenantSequence.prefix == _AST_PREFIX,
            )
            .with_for_update()
        )
        seq = result.scalar_one_or_none()

        if seq is None:
            seq = TenantSequence(tenant_id=tenant_id, prefix=_AST_PREFIX, last_value=0)
            self.session.add(seq)
            await self.session.flush()

        seq.last_value += 1
        await self.session.flush()

        return f"{_AST_PREFIX}-{seq.last_value:05d}"

    # ------------------------------------------------------------------
    # State machine
    # ------------------------------------------------------------------

    async def get_valid_transitions(self, from_status: AssetStatus) -> list[str]:
        """Return the list of valid to_status values for the given from_status."""
        result = await self.session.execute(
            select(AssetStatusTransition.to_status).where(
                AssetStatusTransition.from_status == from_status.value
            )
        )
        return [row[0] for row in result.all()]

    # ------------------------------------------------------------------
    # Audit history (INSERT only)
    # ------------------------------------------------------------------

    async def record_history(
        self,
        asset_id: UUID,
        actor_id: UUID,
        action: AssetHistoryAction,
        field: str | None = None,
        old_val: Any = None,
        new_val: Any = None,
        notes: str | None = None,
    ) -> None:
        """Append one row to asset_history.  Never updates an existing row."""
        entry = AssetHistory(
            asset_id=asset_id,
            actor_id=actor_id,
            action=action,
            field_changed=field,
            old_value={"v": old_val} if old_val is not None else None,
            new_value={"v": new_val} if new_val is not None else None,
            notes=notes,
        )
        self.session.add(entry)
        await self.session.flush()

    # ------------------------------------------------------------------
    # Impact Analysis — recursive CTE (max 3 hops)
    # ------------------------------------------------------------------

    async def get_impact_analysis(
        self,
        asset_id: UUID,
        tenant_id: UUID,
    ) -> tuple[list[UUID], list[UUID]]:
        """Traverse the AssetRelationship graph up to 3 hops from *asset_id*.

        Uses a recursive CTE with fully parameterised bindings — never
        interpolates values into the SQL string.

        Returns
        -------
        (affected_asset_ids, affected_ticket_ids)
            Both lists exclude the source asset itself.
            affected_ticket_ids contains only tickets whose status is NOT
            in ('closed', 'cancelled', 'resolved').
        """
        # Recursive CTE: bidirectional traversal up to depth 3.
        # Both source→target and target→source edges are followed so the
        # graph can be queried in either direction.
        cte_sql = text(
            """
            WITH RECURSIVE impact(asset_id, depth) AS (
                SELECT target_asset_id, 1
                FROM   asset_relationships
                WHERE  source_asset_id = :asset_id
                  AND  tenant_id        = :tenant_id

                UNION

                SELECT source_asset_id, 1
                FROM   asset_relationships
                WHERE  target_asset_id = :asset_id
                  AND  tenant_id        = :tenant_id

                UNION ALL

                SELECT ar.target_asset_id, i.depth + 1
                FROM   asset_relationships ar
                JOIN   impact i ON ar.source_asset_id = i.asset_id
                WHERE  i.depth    < 3
                  AND  ar.tenant_id = :tenant_id
            )
            SELECT DISTINCT asset_id
            FROM   impact
            WHERE  asset_id != :asset_id
            """
        )

        result = await self.session.execute(
            cte_sql,
            {"asset_id": str(asset_id), "tenant_id": str(tenant_id)},
        )
        affected_asset_ids: list[UUID] = [UUID(str(row[0])) for row in result.all()]

        if not affected_asset_ids:
            return [], []

        # Find open tickets linked to the affected assets.
        # Use ORM-level query so IN-clause is fully parameterised by SQLAlchemy.
        # terminal / resolved statuses that do NOT count as "impacted"
        from app.models.asset import AssetTicketLink
        from app.models.ticket import Ticket

        terminal_statuses = ("closed", "cancelled", "resolved")
        ticket_result = await self.session.execute(
            select(AssetTicketLink.ticket_id)
            .distinct()
            .join(Ticket, Ticket.id == AssetTicketLink.ticket_id)
            .where(
                AssetTicketLink.asset_id.in_(affected_asset_ids),
                Ticket.status.not_in(terminal_statuses),
                Ticket.tenant_id == tenant_id,
            )
        )
        affected_ticket_ids: list[UUID] = [
            UUID(str(row[0])) for row in ticket_result.all()
        ]

        return affected_asset_ids, affected_ticket_ids
