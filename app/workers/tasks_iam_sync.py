"""IAM user-sync Celery tasks.

Two tasks live here:

1. ``sync_tenant_iam_users(tenant_id)``
   Per-tenant task dispatched by the beat schedule (via ``sync_all_tenants``).
   Calls IAM internal API, upserts local User mirrors, deactivates absent users.
   Queue: "default"   Max retries: 3   Retry delay: 60 s

2. ``sync_all_tenants``
   Beat task — queries all active tenants and fans out per-tenant tasks.
   Runs every 15 min (beat schedule entry already present in celery_app.py).

Security: IAM_INTERNAL_TOKEN is a static bearer token for inbound service calls.
Never log it — pass only to httpx headers at call time.
"""

import logging

from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-tenant sync task
# ---------------------------------------------------------------------------


@celery_app.task(
    name="app.workers.tasks_iam_sync.sync_tenant_iam_users",
    bind=True,
    queue="default",
    max_retries=3,
    default_retry_delay=60,
)
def sync_tenant_iam_users(self, tenant_id: str) -> None:
    """Sync IAM users for a single tenant.

    Steps:
      1. GET {IAM_BASE_URL}/internal/tenants/{tenant_id}/users
      2. Upsert each user returned (INSERT ... ON CONFLICT DO UPDATE).
      3. Set is_active=False for local users NOT present in the IAM response.
    On any HTTP 4xx/5xx or network error: log WARNING and retry via Celery.
    """
    import asyncio

    try:
        asyncio.run(_async_sync_tenant_iam_users(tenant_id))
    except Exception as exc:
        logger.warning(
            "iam_sync_tenant_failed",
            extra={
                "tenant_id": tenant_id,
                "attempt": self.request.retries + 1,
                "error": str(exc),
            },
        )
        raise self.retry(exc=exc)


async def _async_sync_tenant_iam_users(tenant_id: str) -> None:
    """Async implementation for sync_tenant_iam_users."""
    import httpx
    import sqlalchemy as _sa
    from sqlalchemy import update
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker

    from app.config import get_settings
    from app.models.identity import User

    settings = get_settings()

    # ------------------------------------------------------------------ #
    # 1. Fetch users from IAM internal endpoint                            #
    # ------------------------------------------------------------------ #
    url = f"{settings.IAM_BASE_URL.rstrip('/')}/internal/tenants/{tenant_id}/users"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(
                url,
                headers={
                    "Authorization": f"Bearer {settings.IAM_INTERNAL_TOKEN}",
                    "Accept": "application/json",
                },
            )
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(
            f"IAM returned HTTP {exc.response.status_code} for tenant {tenant_id}"
        ) from exc
    except httpx.RequestError as exc:
        raise RuntimeError(
            f"IAM unreachable for tenant {tenant_id}: {exc}"
        ) from exc

    # Normalise response shape (list or {"users": [...]} wrapper)
    if isinstance(payload, list):
        iam_users: list[dict] = payload
    else:
        iam_users = payload.get("users", payload.get("data", []))

    if not iam_users:
        logger.info(
            "iam_sync_tenant_no_users",
            extra={"tenant_id": tenant_id},
        )
        return

    # ------------------------------------------------------------------ #
    # 2. Upsert + deactivate                                               #
    # ------------------------------------------------------------------ #
    engine = create_async_engine(settings.DATABASE_URL, pool_size=3, max_overflow=1)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    try:
        from uuid import UUID as _UUID
        tenant_uuid = _UUID(tenant_id)

        async with async_session() as db:
            upserted = 0
            deactivated = 0
            iam_user_ids: set[str] = set()

            for iam_user in iam_users:
                iam_uid: str = (
                    iam_user.get("idpSub")
                    or iam_user.get("id")
                    or iam_user.get("iam_user_id")
                    or ""
                )
                if not iam_uid:
                    continue

                iam_user_ids.add(iam_uid)

                email: str = iam_user.get("email", "")
                first_name: str = (
                    iam_user.get("firstName")
                    or iam_user.get("first_name")
                    or iam_user.get("given_name")
                    or ""
                )
                last_name: str = (
                    iam_user.get("lastName")
                    or iam_user.get("last_name")
                    or iam_user.get("family_name")
                    or ""
                )
                avatar_url: str | None = (
                    iam_user.get("avatarUrl") or iam_user.get("picture")
                )

                # INSERT ... ON CONFLICT (iam_user_id) DO UPDATE
                # Use the DB column name "metadata" (ORM attr is metadata_)
                stmt = (
                    pg_insert(User)
                    .values(
                        iam_user_id=iam_uid,
                        tenant_id=tenant_uuid,
                        email=email,
                        first_name=first_name,
                        last_name=last_name,
                        avatar_url=avatar_url,
                        is_active=True,
                        metadata={},
                    )
                    .on_conflict_do_update(
                        index_elements=["iam_user_id"],
                        set_={
                            "email": email,
                            "first_name": first_name,
                            "last_name": last_name,
                            "is_active": True,
                            "updated_at": _sa.func.now(),
                        },
                    )
                )
                await db.execute(stmt)
                upserted += 1

            # Deactivate users in DB not present in the IAM response
            if iam_user_ids:
                deactivate_stmt = (
                    update(User)
                    .where(
                        User.tenant_id == tenant_uuid,
                        User.iam_user_id.notin_(iam_user_ids),
                        User.is_active.is_(True),
                    )
                    .values(
                        is_active=False,
                        updated_at=_sa.func.now(),
                    )
                    .execution_options(synchronize_session=False)
                )
                result = await db.execute(deactivate_stmt)
                deactivated = result.rowcount

            await db.commit()

        logger.info(
            "iam_sync_tenant_complete",
            extra={
                "tenant_id": tenant_id,
                "upserted": upserted,
                "deactivated": deactivated,
            },
        )
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Beat task — fan-out across all active tenants
# ---------------------------------------------------------------------------


@celery_app.task(
    name="app.workers.tasks_iam_sync.sync_all_tenants",
    bind=True,
    max_retries=1,
    default_retry_delay=60,
)
def sync_all_tenants(self) -> None:
    """Query all active tenants and dispatch per-tenant sync tasks.

    Beat schedule: every 15 min (900 s) — see celery_app.py 'iam-user-sync'.
    """
    import asyncio

    try:
        asyncio.run(_async_sync_all_tenants())
    except Exception as exc:
        logger.error(
            "iam_sync_all_tenants_failed",
            extra={"attempt": self.request.retries + 1, "error": str(exc)},
        )
        raise self.retry(exc=exc)


async def _async_sync_all_tenants() -> None:
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker

    from app.config import get_settings
    from app.models.identity import Tenant

    settings = get_settings()
    engine = create_async_engine(settings.DATABASE_URL, pool_size=5, max_overflow=2)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    try:
        async with async_session() as db:
            result = await db.execute(
                select(Tenant.id).where(
                    Tenant.is_active.is_(True),
                    Tenant.deleted_at.is_(None),
                )
            )
            tenant_ids = [str(row.id) for row in result.all()]

        dispatched = 0
        for tid in tenant_ids:
            sync_tenant_iam_users.apply_async(args=[tid], queue="default")
            dispatched += 1

        logger.info(
            "iam_sync_all_tenants_dispatched",
            extra={"dispatched": dispatched},
        )
    finally:
        await engine.dispose()
