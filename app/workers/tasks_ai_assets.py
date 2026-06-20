"""
Celery beat task: run_maintenance_predictions

Runs nightly at 2:30 am UTC.  For every active tenant it loads all non-
disposed, non-retired, non-deleted assets in batches, calls
MaintenancePredictor.predict() for each one, and records an in-app
notification for any asset whose risk_score exceeds 0.7.

Error-handling contract:
  - AIBudgetExhaustedError → log WARNING, stop processing the current
    tenant, continue to the next tenant (never retry or raise to Celery).
  - Any other exception on a single asset → log ERROR, skip that asset,
    continue processing the rest of the batch.
  - Celery retries (max 2) apply only to transient infrastructure failures
    (e.g. DB connection lost) that surface *before* the per-tenant loop
    begins — not to per-asset failures.
"""

import asyncio
import logging
from uuid import UUID

from app.exceptions import AIBudgetExhaustedError
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(
    name="app.workers.tasks_ai_assets.run_maintenance_predictions",
    queue="low",
    bind=True,
    max_retries=2,
)
def run_maintenance_predictions(self) -> None:  # type: ignore[override]
    """Nightly predictive maintenance scan for all active tenants.

    Delegates to the async implementation to keep the Celery task layer thin.
    """
    asyncio.run(_run_maintenance_predictions_async())


async def _run_maintenance_predictions_async() -> None:
    """Async implementation — called from the sync Celery wrapper."""
    from sqlalchemy import select

    from app.config import get_settings
    from app.database import AsyncSessionLocal
    from app.models.asset import Asset, AssetStatus
    from app.models.identity import Tenant
    from app.models.notification import Notification, NotificationType
    from app.redis_client import redis_client
    from app.services.ai.maintenance_predictor import MaintenancePredictor

    s = get_settings()
    batch_size = s.AI_MAINTENANCE_BATCH_SIZE
    redis = redis_client
    predictor = MaintenancePredictor()

    # Statuses that are considered "active" for maintenance prediction
    _INACTIVE_STATUSES = {AssetStatus.disposed.value, AssetStatus.retired.value}

    async with AsyncSessionLocal() as db:
        # Load all active tenants
        tenant_result = await db.execute(
            select(Tenant.id).where(Tenant.is_active.is_(True))
        )
        tenant_ids: list[UUID] = [row[0] for row in tenant_result.all()]

    for tenant_id in tenant_ids:
        assets_processed = 0
        predictions_updated = 0
        high_risk_count = 0

        async with AsyncSessionLocal() as db:
            try:
                # Count total active assets for this tenant
                asset_query = (
                    select(Asset)
                    .where(
                        Asset.tenant_id == tenant_id,
                        Asset.deleted_at.is_(None),
                        Asset.status.not_in(list(_INACTIVE_STATUSES)),
                    )
                    .order_by(Asset.id)
                )

                result = await db.execute(asset_query)
                all_assets = list(result.scalars().all())

                # Process in batches
                for batch_start in range(0, len(all_assets), batch_size):
                    batch = all_assets[batch_start : batch_start + batch_size]

                    for asset in batch:
                        try:
                            prediction = await predictor.predict(
                                db=db,
                                redis=redis,
                                asset=asset,
                                tenant_id=tenant_id,
                            )
                            predictions_updated += 1
                            assets_processed += 1

                            # Enqueue high-risk notification (risk_score > 0.7)
                            if prediction.risk_score > 0.7:
                                high_risk_count += 1
                                await _enqueue_high_risk_notification(
                                    db=db,
                                    asset=asset,
                                    risk_score=prediction.risk_score,
                                    reason=prediction.reason,
                                    tenant_id=tenant_id,
                                    NotificationType=NotificationType,
                                )

                        except AIBudgetExhaustedError:
                            logger.warning(
                                "maintenance_prediction_budget_exhausted",
                                extra={
                                    "tenant_id": str(tenant_id),
                                    "assets_processed": assets_processed,
                                },
                            )
                            # Stop processing this tenant; commit what we have
                            await db.commit()
                            break  # break out of asset loop

                        except Exception as exc:
                            logger.error(
                                "maintenance_prediction_asset_failed",
                                extra={
                                    "tenant_id": str(tenant_id),
                                    "asset_id": str(asset.id),
                                    "exception_type": type(exc).__name__,
                                    "exception": str(exc),
                                },
                            )
                            assets_processed += 1
                            # Continue with next asset
                            continue

                    else:
                        # Commit after each successful batch
                        await db.commit()
                        continue  # continue outer for loop

                    # Budget exhausted — break outer batch loop too
                    break

            except AIBudgetExhaustedError:
                # Raised before per-asset loop (e.g. budget already exhausted
                # at tenant-level check inside predict())
                logger.warning(
                    "maintenance_prediction_tenant_budget_exhausted_early",
                    extra={"tenant_id": str(tenant_id)},
                )
                await db.commit()
                continue

            except Exception as exc:
                logger.error(
                    "maintenance_prediction_tenant_failed",
                    extra={
                        "tenant_id": str(tenant_id),
                        "exception_type": type(exc).__name__,
                        "exception": str(exc),
                    },
                )
                try:
                    await db.rollback()
                except Exception:
                    pass
                continue

        logger.info(
            "maintenance_prediction_tenant_complete",
            extra={
                "tenant_id": str(tenant_id),
                "assets_processed": assets_processed,
                "predictions_updated": predictions_updated,
                "high_risk_count": high_risk_count,
            },
        )


async def _enqueue_high_risk_notification(
    db,
    asset,
    risk_score: float,
    reason: str,
    tenant_id: UUID,
    NotificationType,
) -> None:
    """Write an in-app notification for asset managers / tenant admins.

    Targets all admin and manager users in the tenant so the right people
    see the alert regardless of asset assignment.
    """
    from sqlalchemy import select

    from app.models.identity import ITSMRole, User, UserITSMRole
    from app.models.notification import Notification

    # Find admin and manager users for this tenant
    role_result = await db.execute(
        select(User.id)
        .join(UserITSMRole, UserITSMRole.user_id == User.id)
        .join(ITSMRole, ITSMRole.id == UserITSMRole.role_id)
        .where(
            User.tenant_id == tenant_id,
            User.is_active.is_(True),
            ITSMRole.name.in_(["admin", "manager"]),
        )
        .distinct()
    )
    target_user_ids = [row[0] for row in role_result.all()]

    if not target_user_ids:
        # Fall back to assigned user if no admin/manager found
        if asset.assigned_to is not None:
            target_user_ids = [asset.assigned_to]

    for user_id in target_user_ids:
        notification = Notification(
            tenant_id=tenant_id,
            user_id=user_id,
            type=NotificationType.asset_maintenance_due,
            title=f"High maintenance risk: {asset.name}",
            body=(
                f"Asset '{asset.name}' (tag: {asset.asset_tag}) has a risk score of "
                f"{risk_score:.2f}. {reason[:150]}"
            ),
            entity_type="asset",
            entity_id=asset.id,
        )
        db.add(notification)

    await db.flush()
