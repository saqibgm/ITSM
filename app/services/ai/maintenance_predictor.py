"""
MaintenancePredictor — uses Claude to predict when an asset needs maintenance.

Security rules (§7A.11):
  - The system prompt is fully static — no tenant-supplied or asset data here.
  - All asset context (type, age, condition, maintenance history, etc.) is
    placed in the ``user`` turn so it is never interpolated into the system
    prompt.

Upsert behaviour:
  - One prediction row per asset.  When a new prediction is generated the
    existing row (if any) is deleted first, then the new one is inserted.
    This keeps the table tidy and avoids accumulating stale rows.

Raises:
    AIBudgetExhaustedError — budget exhausted; callers (Celery task) catch
        this silently, log a WARNING, and skip the rest of the tenant.
    ExternalServiceError   — Claude unavailable after retries; propagated to
        Celery task for standard retry handling.
"""

import json
import logging
from datetime import date, datetime, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.ai_asset import AIPredictiveMaintenance
from app.models.asset import Asset, AssetMaintenance, AssetType
from app.services.ai.ai_service import AIService

logger = logging.getLogger(__name__)

# Static system prompt — zero user/tenant content here
_SYSTEM_PROMPT = (
    "You are a predictive maintenance analyst for IT assets. "
    "Analyse the asset data and return a JSON risk assessment. "
    "Return JSON only — no prose, no markdown fences. "
    "The JSON must have exactly these keys: "
    '{"risk_score": float between 0.0 and 1.0, '
    '"predicted_due_date": "YYYY-MM-DD", '
    '"reason": string max 200 characters}. '
    "risk_score 1.0 means maintenance is critically overdue; "
    "0.0 means no maintenance needed in the foreseeable future."
)

_MODEL_VERSION_PREFIX = "claude-maintenance-"


class MaintenancePredictor:
    """Predict maintenance need for a single asset using Claude."""

    def __init__(self, ai_service: AIService | None = None) -> None:
        self._ai = ai_service or AIService()

    async def predict(
        self,
        db: AsyncSession,
        redis,
        asset: Asset,
        tenant_id: UUID,
    ) -> AIPredictiveMaintenance:
        """Generate (or refresh) a predictive maintenance record for *asset*.

        Builds an asset context string from:
          - asset type name
          - age in days (purchase_date → today)
          - condition
          - manufacturer
          - last maintenance date (most recent completed AssetMaintenance)
          - warranty_expiry_date

        All of this dynamic content is placed in the **user** message.
        The system prompt stays static.

        Args:
            db:        Async SQLAlchemy session.
            redis:     Redis client (for budget tracking).
            asset:     The Asset ORM object to analyse.
            tenant_id: Tenant UUID (for budget gating).

        Returns:
            The newly persisted AIPredictiveMaintenance record.

        Raises:
            AIBudgetExhaustedError: if the tenant budget is exhausted.
            ExternalServiceError:   if Claude is unavailable after retries.
        """
        s = get_settings()

        # ------------------------------------------------------------------
        # Build asset context (all dynamic — user turn only)
        # ------------------------------------------------------------------

        # Resolve asset type name
        type_name: str = "Unknown"
        if asset.type_id is not None:
            type_result = await db.execute(
                select(AssetType.name).where(AssetType.id == asset.type_id)
            )
            row = type_result.scalar_one_or_none()
            if row:
                type_name = row

        # Age in days from purchase_date
        today = date.today()
        age_days: Optional[int] = None
        if asset.purchase_date is not None:
            age_days = (today - asset.purchase_date).days

        # Last completed maintenance date
        last_maint_result = await db.execute(
            select(AssetMaintenance.completed_date)
            .where(
                AssetMaintenance.asset_id == asset.id,
                AssetMaintenance.completed_date.isnot(None),
            )
            .order_by(AssetMaintenance.completed_date.desc())
            .limit(1)
        )
        last_maint_row = last_maint_result.scalar_one_or_none()
        last_maint_date: Optional[date] = last_maint_row if last_maint_row else None

        # Compose the user message (all dynamic content here)
        context_lines = [
            f"Asset name: {asset.name}",
            f"Asset type: {type_name}",
            f"Condition: {asset.condition.value if asset.condition else 'unknown'}",
            f"Manufacturer: {asset.manufacturer or 'unknown'}",
            f"Age: {f'{age_days} days' if age_days is not None else 'unknown (no purchase date)'}",
            f"Purchase date: {asset.purchase_date.isoformat() if asset.purchase_date else 'unknown'}",
            f"Warranty expiry: {asset.warranty_expiry_date.isoformat() if asset.warranty_expiry_date else 'none'}",
            f"Last maintenance completed: {last_maint_date.isoformat() if last_maint_date else 'never'}",
            f"Current status: {asset.status.value}",
            f"Today's date: {today.isoformat()}",
        ]
        user_content = "\n".join(context_lines)

        messages = [{"role": "user", "content": user_content}]

        # ------------------------------------------------------------------
        # Call Claude (budget-gated, retried on failure)
        # ------------------------------------------------------------------
        raw_response = await self._ai.generate(
            tenant_id=str(tenant_id),
            redis=redis,
            messages=messages,
            system=_SYSTEM_PROMPT,
            feature="predictive_maintenance",
        )

        # ------------------------------------------------------------------
        # Parse JSON response
        # ------------------------------------------------------------------
        parsed = self._parse_response(raw_response, today)

        risk_score = float(parsed.get("risk_score", 0.5))
        risk_score = max(0.0, min(1.0, risk_score))

        predicted_due_date = parsed["predicted_due_date"]
        reason = str(parsed.get("reason", ""))[:200]  # cap at 200 chars

        model_version = _MODEL_VERSION_PREFIX + s.CLAUDE_MODEL

        # ------------------------------------------------------------------
        # Upsert: delete existing prediction for this asset, insert fresh one
        # ------------------------------------------------------------------
        await db.execute(
            delete(AIPredictiveMaintenance).where(
                AIPredictiveMaintenance.asset_id == asset.id
            )
        )
        await db.flush()

        prediction = AIPredictiveMaintenance(
            asset_id=asset.id,
            predicted_due_date=predicted_due_date,
            risk_score=risk_score,
            reason=reason,
            model_version=model_version,
            acknowledged=False,
            acknowledged_by=None,
        )
        db.add(prediction)
        await db.flush()
        await db.refresh(prediction)

        logger.info(
            "maintenance_prediction_complete",
            extra={
                "asset_id": str(asset.id),
                "tenant_id": str(tenant_id),
                "risk_score": risk_score,
                "predicted_due_date": predicted_due_date.isoformat(),
            },
        )

        return prediction

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _parse_response(self, raw: str, today: date) -> dict:
        """Attempt to parse Claude's JSON response.

        Falls back to a safe default on any parse error so that prediction
        failures do not crash the Celery task loop.
        """
        from datetime import timedelta

        default_due = today + timedelta(days=90)

        try:
            cleaned = raw.strip()
            if cleaned.startswith("```"):
                lines = cleaned.split("\n")
                lines = [ln for ln in lines if not ln.strip().startswith("```")]
                cleaned = "\n".join(lines).strip()

            data = json.loads(cleaned)

            # Parse predicted_due_date string to date
            raw_date = data.get("predicted_due_date", "")
            try:
                data["predicted_due_date"] = date.fromisoformat(str(raw_date))
            except (ValueError, TypeError):
                data["predicted_due_date"] = default_due

            return data

        except (json.JSONDecodeError, ValueError, KeyError):
            logger.warning(
                "maintenance_prediction_json_parse_failed",
                extra={"raw_response_excerpt": raw[:200]},
            )
            return {
                "risk_score": 0.5,
                "predicted_due_date": default_due,
                "reason": "Unable to parse AI response",
            }
