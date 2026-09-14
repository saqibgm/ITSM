"""Per-tenant system identity for marketplace-triggered tickets/comments.

Resolves the TODO left in ingestion.py: TicketService.create_ticket needs a
requester_id, TicketComment.author_id is a required FK to a real User row —
neither has an "external/system" concept, so this provisions (once) an
actual User row per tenant to attribute marketplace-originated activity to,
rather than leaving requester_id/author_id as a caller-supplied blind spot.

iam_user_id is a synthesized, stable, non-IAM-issued value
('marketplace-bot') — safe because User's uniqueness is on
(iam_user_id, tenant_id) together (migration 0018), not iam_user_id alone,
so this can't collide with a real IAM identity across tenants. This mirrors
how the rest of the codebase already treats User as "local mirror of an IAM
identity, synced on login" — a marketplace bot has no IAM login to sync
from, so it's provisioned directly instead, the same shape as the IAM
webhook's tenant-provisioning seed logic (app/api/v1/webhooks.py
_provision_tenant()) creates rows without a prior login triggering them.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.identity import User

_BOT_IAM_USER_ID = "marketplace-bot"


async def get_or_create_marketplace_bot_user(db: AsyncSession, tenant_id: UUID) -> UUID:
    existing = (
        await db.execute(
            select(User.id).where(User.tenant_id == tenant_id, User.iam_user_id == _BOT_IAM_USER_ID)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    user = User(
        iam_user_id=_BOT_IAM_USER_ID,
        tenant_id=tenant_id,
        email="marketplace-bot@system.internal",
        first_name="Marketplace",
        last_name="Sync",
        is_active=True,
        metadata_={"system_account": True, "purpose": "marketplace-integration ticket/comment attribution"},
    )
    db.add(user)
    await db.flush()
    return user.id


__all__ = ["get_or_create_marketplace_bot_user"]
