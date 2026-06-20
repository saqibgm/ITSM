"""Automation Rules Engine service (S6A).

Evaluates AutomationRule rows against Ticket / Asset entities and executes
the configured action sequence.  Errors in individual rule actions are always
caught and logged — they never propagate to the caller.

Condition operators:
  eq       field == value
  ne       field != value
  in       field in value_list
  gt       field > value
  lt       field < value
  contains tag name exists on ticket (TicketTagAssignment join)

Supported ticket fields: priority, status, ticket_type, category_id,
  assigned_team_id (maps to ticket.team_id), assigned_to_id (ticket.assignee_id), tag

Supported asset fields: status, asset_type_id (maps to asset.type_id), assigned_to_id
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.automation import AutomationLog, AutomationRule
from app.models.ticket import Ticket, TicketPriority, TicketStatus, TicketTagAssignment

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Ticket field accessor  (maps condition field name → ticket attribute value)
# ---------------------------------------------------------------------------

_TICKET_FIELD_MAP: dict[str, str] = {
    "priority": "priority",
    "status": "status",
    "ticket_type": "type",
    "category_id": "category_id",
    "assigned_team_id": "team_id",
    "assigned_to_id": "assignee_id",
}

_ASSET_FIELD_MAP: dict[str, str] = {
    "status": "status",
    "asset_type_id": "type_id",
    "assigned_to_id": "assigned_to",
}


def _coerce(value: Any) -> str | None:
    """Convert enum members or UUIDs to their string/value representation."""
    if value is None:
        return None
    if hasattr(value, "value"):
        return str(value.value)
    return str(value)


async def _get_ticket_tag_names(db: AsyncSession, ticket_id: UUID) -> set[str]:
    """Return the set of tag names currently assigned to the ticket."""
    from app.models.ticket import TicketTag

    result = await db.execute(
        select(TicketTag.name)
        .join(TicketTagAssignment, TicketTagAssignment.tag_id == TicketTag.id)
        .where(TicketTagAssignment.ticket_id == ticket_id)
    )
    return {row[0] for row in result.all()}


def _evaluate_op(actual: Any, op: str, expected: Any) -> bool:
    """Compare actual to expected using op.  Returns False on any error."""
    try:
        actual_s = _coerce(actual)
        if op == "eq":
            return actual_s == str(expected)
        if op == "ne":
            return actual_s != str(expected)
        if op == "in":
            if not isinstance(expected, list):
                return False
            return actual_s in [str(v) for v in expected]
        if op == "gt":
            return float(actual_s) > float(expected)  # type: ignore[arg-type]
        if op == "lt":
            return float(actual_s) < float(expected)  # type: ignore[arg-type]
        # "contains" is handled separately for tags
        logger.warning("automation_unknown_op", extra={"op": op})
        return False
    except Exception as exc:
        logger.warning(
            "automation_condition_eval_error",
            extra={"op": op, "actual": str(actual), "expected": str(expected), "error": str(exc)},
        )
        return False


# ---------------------------------------------------------------------------
# AutomationService
# ---------------------------------------------------------------------------


class AutomationService:
    """Evaluates and executes automation rules for tickets and assets."""

    # ------------------------------------------------------------------
    # Public: ticket rules
    # ------------------------------------------------------------------

    async def evaluate_ticket_rules(
        self,
        db: AsyncSession,
        redis: Any,
        trigger: str,
        ticket: Ticket,
        changed_fields: list[str] | None = None,
    ) -> None:
        """Load active rules for tenant+trigger and execute matching ones."""
        try:
            rules = await self._load_rules(db, ticket.tenant_id, trigger)
        except Exception as exc:
            logger.error(
                "automation_load_rules_failed",
                extra={"trigger": trigger, "ticket_id": str(ticket.id), "error": str(exc)},
            )
            return

        tag_names: set[str] | None = None  # lazy loaded once per evaluation

        for rule in rules:
            actions_executed: list[dict] = []
            error_msg: str | None = None

            try:
                # --- Evaluate conditions ---
                all_pass = True
                for cond in (rule.conditions or []):
                    field = cond.get("field", "")
                    op = cond.get("op", "")
                    value = cond.get("value")

                    if field == "tag" and op == "contains":
                        if tag_names is None:
                            tag_names = await _get_ticket_tag_names(db, ticket.id)
                        result = str(value) in tag_names
                    elif field in _TICKET_FIELD_MAP:
                        attr = _TICKET_FIELD_MAP[field]
                        actual = getattr(ticket, attr, None)
                        result = _evaluate_op(actual, op, value)
                    else:
                        # Unknown field — condition fails gracefully
                        logger.warning(
                            "automation_unknown_field",
                            extra={"field": field, "rule_id": str(rule.id)},
                        )
                        result = False

                    if not result:
                        all_pass = False
                        break

                if not all_pass:
                    continue

                # --- Execute actions ---
                for action in (rule.actions or []):
                    action_type = action.get("type", "")
                    try:
                        await self._execute_ticket_action(
                            db, redis, ticket, action, rule
                        )
                        actions_executed.append(action)
                    except Exception as exc:
                        logger.warning(
                            "automation_action_failed",
                            extra={
                                "rule_id": str(rule.id),
                                "action_type": action_type,
                                "ticket_id": str(ticket.id),
                                "error": str(exc),
                            },
                        )
                        error_msg = f"action={action_type}: {exc}"

            except Exception as exc:
                logger.error(
                    "automation_rule_eval_failed",
                    extra={"rule_id": str(rule.id), "ticket_id": str(ticket.id), "error": str(exc)},
                )
                error_msg = str(exc)

            # --- Always write log + update counters ---
            await self._write_log(
                db=db,
                rule=rule,
                entity_type="ticket",
                entity_id=ticket.id,
                actions_executed=actions_executed,
                error=error_msg,
            )

    # ------------------------------------------------------------------
    # Public: asset rules
    # ------------------------------------------------------------------

    async def evaluate_asset_rules(
        self,
        db: AsyncSession,
        redis: Any,
        trigger: str,
        asset: Any,  # Asset
    ) -> None:
        """Load active rules for tenant+trigger and execute matching ones."""
        try:
            rules = await self._load_rules(db, asset.tenant_id, trigger)
        except Exception as exc:
            logger.error(
                "automation_load_rules_failed",
                extra={"trigger": trigger, "asset_id": str(asset.id), "error": str(exc)},
            )
            return

        for rule in rules:
            actions_executed: list[dict] = []
            error_msg: str | None = None

            try:
                # --- Evaluate conditions ---
                all_pass = True
                for cond in (rule.conditions or []):
                    field = cond.get("field", "")
                    op = cond.get("op", "")
                    value = cond.get("value")

                    if field in _ASSET_FIELD_MAP:
                        attr = _ASSET_FIELD_MAP[field]
                        actual = getattr(asset, attr, None)
                        result = _evaluate_op(actual, op, value)
                    else:
                        logger.warning(
                            "automation_unknown_field",
                            extra={"field": field, "rule_id": str(rule.id)},
                        )
                        result = False

                    if not result:
                        all_pass = False
                        break

                if not all_pass:
                    continue

                # --- Execute actions ---
                for action in (rule.actions or []):
                    action_type = action.get("type", "")
                    try:
                        await self._execute_asset_action(db, redis, asset, action, rule)
                        actions_executed.append(action)
                    except Exception as exc:
                        logger.warning(
                            "automation_action_failed",
                            extra={
                                "rule_id": str(rule.id),
                                "action_type": action_type,
                                "asset_id": str(asset.id),
                                "error": str(exc),
                            },
                        )
                        error_msg = f"action={action_type}: {exc}"

            except Exception as exc:
                logger.error(
                    "automation_rule_eval_failed",
                    extra={"rule_id": str(rule.id), "asset_id": str(asset.id), "error": str(exc)},
                )
                error_msg = str(exc)

            await self._write_log(
                db=db,
                rule=rule,
                entity_type="asset",
                entity_id=asset.id,
                actions_executed=actions_executed,
                error=error_msg,
            )

    # ------------------------------------------------------------------
    # Dry-run / test
    # ------------------------------------------------------------------

    async def test_rule_against_ticket(
        self,
        db: AsyncSession,
        rule: AutomationRule,
        ticket: Ticket,
    ) -> dict:
        """Evaluate conditions only — no writes, no actions executed."""
        condition_results = []
        tag_names: set[str] | None = None
        all_pass = True

        for cond in (rule.conditions or []):
            field = cond.get("field", "")
            op = cond.get("op", "")
            value = cond.get("value")

            if field == "tag" and op == "contains":
                if tag_names is None:
                    tag_names = await _get_ticket_tag_names(db, ticket.id)
                result = str(value) in tag_names
            elif field in _TICKET_FIELD_MAP:
                attr = _TICKET_FIELD_MAP[field]
                actual = getattr(ticket, attr, None)
                result = _evaluate_op(actual, op, value)
            else:
                result = False

            condition_results.append({"field": field, "op": op, "value": value, "result": result})
            if not result:
                all_pass = False

        return {
            "conditions_pass": all_pass,
            "condition_results": condition_results,
            "actions_would_execute": list(rule.actions or []) if all_pass else [],
        }

    async def test_rule_against_asset(
        self,
        db: AsyncSession,
        rule: AutomationRule,
        asset: Any,
    ) -> dict:
        """Evaluate conditions only — no writes, no actions executed."""
        condition_results = []
        all_pass = True

        for cond in (rule.conditions or []):
            field = cond.get("field", "")
            op = cond.get("op", "")
            value = cond.get("value")

            if field in _ASSET_FIELD_MAP:
                attr = _ASSET_FIELD_MAP[field]
                actual = getattr(asset, attr, None)
                result = _evaluate_op(actual, op, value)
            else:
                result = False

            condition_results.append({"field": field, "op": op, "value": value, "result": result})
            if not result:
                all_pass = False

        return {
            "conditions_pass": all_pass,
            "condition_results": condition_results,
            "actions_would_execute": list(rule.actions or []) if all_pass else [],
        }

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _load_rules(
        self, db: AsyncSession, tenant_id: UUID, trigger: str
    ) -> list[AutomationRule]:
        result = await db.execute(
            select(AutomationRule)
            .where(
                AutomationRule.tenant_id == tenant_id,
                AutomationRule.trigger == trigger,
                AutomationRule.is_active.is_(True),
            )
            .order_by(AutomationRule.run_order.asc(), AutomationRule.created_at.asc())
        )
        return list(result.scalars().all())

    async def _write_log(
        self,
        *,
        db: AsyncSession,
        rule: AutomationRule,
        entity_type: str,
        entity_id: UUID,
        actions_executed: list[dict],
        error: str | None,
    ) -> None:
        try:
            log = AutomationLog(
                rule_id=rule.id,
                tenant_id=rule.tenant_id,
                entity_type=entity_type,
                entity_id=entity_id,
                actions_executed=actions_executed,
                error=error,
            )
            db.add(log)

            # Update rule counters using DB func.now()
            rule.trigger_count = AutomationRule.trigger_count + 1  # type: ignore[assignment]
            rule.last_triggered_at = func.now()  # type: ignore[assignment]
            db.add(rule)

            await db.flush()
        except Exception as exc:
            logger.error(
                "automation_write_log_failed",
                extra={"rule_id": str(rule.id), "error": str(exc)},
            )

    # ------------------------------------------------------------------
    # Ticket action execution
    # ------------------------------------------------------------------

    async def _execute_ticket_action(
        self,
        db: AsyncSession,
        redis: Any,
        ticket: Ticket,
        action: dict,
        rule: AutomationRule,
    ) -> None:
        action_type = action.get("type", "")

        if action_type == "assign_team":
            team_id = action.get("team_id")
            if team_id:
                ticket.team_id = UUID(str(team_id))  # type: ignore[assignment]
                db.add(ticket)
                await db.flush()

        elif action_type == "assign_user":
            user_id = action.get("user_id")
            if user_id:
                ticket.assignee_id = UUID(str(user_id))  # type: ignore[assignment]
                db.add(ticket)
                await db.flush()

        elif action_type == "set_priority":
            priority_raw = action.get("priority", "")
            try:
                new_priority = TicketPriority(priority_raw)
                ticket.priority = new_priority  # type: ignore[assignment]
                db.add(ticket)
                await db.flush()
            except ValueError:
                logger.warning(
                    "automation_invalid_priority",
                    extra={"priority": priority_raw, "rule_id": str(rule.id)},
                )
                raise ValueError(f"Invalid priority value: {priority_raw!r}")

        elif action_type == "add_tag":
            tag_name = action.get("tag", "")
            if tag_name:
                await self._add_ticket_tag(db, ticket, tag_name)

        elif action_type == "send_notification":
            await self._send_ticket_notification(db, ticket, action)

        elif action_type == "auto_close":
            resolution_note = action.get("resolution_note", "Auto-closed by automation rule")
            await self._auto_close_ticket(db, ticket, resolution_note)

        elif action_type == "escalate":
            await self._escalate_ticket(db, ticket, rule)

        else:
            logger.warning(
                "automation_unknown_action_type",
                extra={"action_type": action_type, "rule_id": str(rule.id)},
            )

    async def _add_ticket_tag(
        self, db: AsyncSession, ticket: Ticket, tag_name: str
    ) -> None:
        """Find or create the tag, then idempotently assign it to the ticket."""
        from app.models.ticket import TicketTag

        # Find existing tag for this tenant
        result = await db.execute(
            select(TicketTag).where(
                TicketTag.tenant_id == ticket.tenant_id,
                TicketTag.name == tag_name,
            )
        )
        tag = result.scalar_one_or_none()

        if tag is None:
            tag = TicketTag(
                tenant_id=ticket.tenant_id,
                name=tag_name,
                color="#888888",
            )
            db.add(tag)
            await db.flush()

        # Idempotent assignment — ignore if already exists
        existing = await db.execute(
            select(TicketTagAssignment).where(
                TicketTagAssignment.ticket_id == ticket.id,
                TicketTagAssignment.tag_id == tag.id,
            )
        )
        if existing.scalar_one_or_none() is None:
            assignment = TicketTagAssignment(
                ticket_id=ticket.id,
                tag_id=tag.id,
            )
            db.add(assignment)
            await db.flush()

    async def _send_ticket_notification(
        self, db: AsyncSession, ticket: Ticket, action: dict
    ) -> None:
        from app.models.notification import Notification, NotificationType

        message = action.get("message", "Automation rule triggered")
        target = action.get("target", "assigned_user")

        recipient_ids: list[UUID] = []

        if target == "assigned_user" and ticket.assignee_id:
            recipient_ids.append(ticket.assignee_id)
        elif target == "reporter":
            recipient_ids.append(ticket.requester_id)
        elif target == "team" and ticket.team_id:
            from app.models.identity import TeamMember
            result = await db.execute(
                select(TeamMember.user_id).where(TeamMember.team_id == ticket.team_id)
            )
            recipient_ids = [row[0] for row in result.all()]

        for user_id in recipient_ids:
            notif = Notification(
                tenant_id=ticket.tenant_id,
                user_id=user_id,
                type=NotificationType.system,
                title=f"Automation: {message[:200]}",
                body=message,
                entity_type="ticket",
                entity_id=ticket.id,
            )
            db.add(notif)
        if recipient_ids:
            await db.flush()

    async def _auto_close_ticket(
        self, db: AsyncSession, ticket: Ticket, resolution_note: str
    ) -> None:
        """Close the ticket if the state machine permits it; log WARNING and skip if not."""
        from app.exceptions import InvalidStateTransitionError
        from app.repositories.ticket_repo import TicketRepository
        from app.services.ticket_service import TicketService

        try:
            svc = TicketService()
            await svc.update_ticket(
                ticket=ticket,
                updates={"status": TicketStatus.closed.value},
                actor_id=ticket.requester_id,  # system action, use requester as proxy
                db=db,
            )
        except InvalidStateTransitionError as exc:
            logger.warning(
                "automation_auto_close_blocked",
                extra={
                    "ticket_id": str(ticket.id),
                    "current_status": ticket.status.value,
                    "error": str(exc),
                },
            )

    async def _escalate_ticket(
        self, db: AsyncSession, ticket: Ticket, rule: AutomationRule
    ) -> None:
        from app.models.ticket import TicketEscalation

        escalation = TicketEscalation(
            ticket_id=ticket.id,
            escalated_by=ticket.requester_id,  # system action
            escalated_to=ticket.assignee_id,
            reason=f"Escalated by automation rule: {rule.name}",
        )
        db.add(escalation)
        await db.flush()

    # ------------------------------------------------------------------
    # Asset action execution
    # ------------------------------------------------------------------

    async def _execute_asset_action(
        self,
        db: AsyncSession,
        redis: Any,
        asset: Any,
        action: dict,
        rule: AutomationRule,
    ) -> None:
        action_type = action.get("type", "")

        if action_type == "assign_user":
            user_id = action.get("user_id")
            if user_id:
                asset.assigned_to = UUID(str(user_id))
                db.add(asset)
                await db.flush()

        elif action_type == "send_notification":
            await self._send_asset_notification(db, asset, action)

        else:
            logger.warning(
                "automation_unknown_action_type",
                extra={"action_type": action_type, "rule_id": str(rule.id)},
            )

    async def _send_asset_notification(
        self, db: AsyncSession, asset: Any, action: dict
    ) -> None:
        from app.models.notification import Notification, NotificationType

        message = action.get("message", "Automation rule triggered")
        target = action.get("target", "assigned_user")

        recipient_ids: list[UUID] = []

        if target == "assigned_user" and asset.assigned_to:
            recipient_ids.append(asset.assigned_to)

        for user_id in recipient_ids:
            notif = Notification(
                tenant_id=asset.tenant_id,
                user_id=user_id,
                type=NotificationType.system,
                title=f"Automation: {message[:200]}",
                body=message,
                entity_type="asset",
                entity_id=asset.id,
            )
            db.add(notif)
        if recipient_ids:
            await db.flush()


# Module-level singleton used by tasks
automation_service = AutomationService()
