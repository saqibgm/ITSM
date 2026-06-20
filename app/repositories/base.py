"""Generic async CRUD repository base class."""

from typing import Any, Generic, TypeVar
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import ResourceNotFoundError
from app.models.base import Base

T = TypeVar("T", bound=Base)


class BaseRepository(Generic[T]):
    model: type[T]
    session: AsyncSession

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, id: UUID, tenant_id: UUID | None) -> T | None:
        """Fetch by id, scoped to tenant_id — or cross-tenant when tenant_id
        is None (only meaningful for platform users in the "all tenants" view;
        route handlers gate who may pass None)."""
        q = select(self.model).where(self.model.id == id)
        if tenant_id is not None:
            q = q.where(self.model.tenant_id == tenant_id)
        result = await self.session.execute(q)
        return result.scalar_one_or_none()

    async def get_or_404(self, id: UUID, tenant_id: UUID | None) -> T:
        obj = await self.get(id, tenant_id)
        if obj is None:
            raise ResourceNotFoundError(self.model.__tablename__, str(id))
        return obj

    async def create(self, obj: T) -> T:
        self.session.add(obj)
        await self.session.flush()
        await self.session.refresh(obj)
        return obj

    async def update(self, obj: T) -> T:
        self.session.add(obj)
        await self.session.flush()
        await self.session.refresh(obj)
        return obj

    async def delete(self, obj: T) -> None:
        await self.session.delete(obj)
        await self.session.flush()
