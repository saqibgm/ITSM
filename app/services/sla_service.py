"""SLA time calculation and pause/resume helpers.

Rules enforced here:
- NEVER use Python datetime.now() for SLA math — caller supplies created_at
  from the DB record; all "now" expressions use func.now() (DB server clock).
- Business-hours-only: skip non-working hours, nights, weekends, public holidays.
- Pause/resume: accumulate paused duration, push due times forward by that interval.
"""

import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Any

from sqlalchemy import func

from app.models.ticket import BusinessHoursConfig, SLAPolicy, Ticket, TicketStatus, TicketPriority

logger = logging.getLogger(__name__)

# Priority ladder, low → critical. Used to auto-escalate a ticket one level on
# SLA breach (a default escalation — no per-tenant automation rule required).
_PRIORITY_LADDER = [
    TicketPriority.low, TicketPriority.medium, TicketPriority.high, TicketPriority.critical,
]


def escalated_priority(current) -> TicketPriority:
    """Next priority up the ladder; critical (top) stays critical.

    Pure + tolerant of either a TicketPriority or its string value.
    """
    cur = current if isinstance(current, TicketPriority) else TicketPriority(current)
    idx = _PRIORITY_LADDER.index(cur)
    return _PRIORITY_LADDER[min(idx + 1, len(_PRIORITY_LADDER) - 1)]


class SLAService:
    """Stateless service — no DB session held; all DB expressions use func.now()."""

    # ------------------------------------------------------------------
    # Public: calculate initial due times
    # ------------------------------------------------------------------

    def calculate_due_times(
        self,
        created_at: datetime,
        sla_policy: SLAPolicy,
        business_hours: BusinessHoursConfig | None,
    ) -> tuple[datetime, datetime]:
        """Return (sla_response_due, sla_resolve_due).

        If sla_policy.business_hours_only is True and business_hours is
        provided, only elapsed working minutes count toward the SLA clock.
        Nights, weekends, and explicit holiday dates are skipped.

        NEVER call datetime.now() inside this method.  created_at is the DB
        record's created_at value passed in by the caller.
        """
        if sla_policy.business_hours_only and business_hours is not None:
            response_due = self._add_working_minutes(
                start=created_at,
                minutes=sla_policy.response_time_minutes,
                bh=business_hours,
            )
            resolve_due = self._add_working_minutes(
                start=created_at,
                minutes=sla_policy.resolution_time_minutes,
                bh=business_hours,
            )
        else:
            response_due = created_at + timedelta(minutes=sla_policy.response_time_minutes)
            resolve_due = created_at + timedelta(minutes=sla_policy.resolution_time_minutes)

        return response_due, resolve_due

    # ------------------------------------------------------------------
    # Public: pause / resume dict for ticket update
    # ------------------------------------------------------------------

    def calculate_sla_pause_resume(
        self,
        ticket: Ticket,
        new_status: TicketStatus,
    ) -> dict:
        """Return a dict of field expressions to apply to the ticket row.

        On pause (new_status == TicketStatus.pending):
            Sets sla_paused_at = func.now().

        On resume (ticket was pending, new status is not pending):
            - Adds elapsed seconds to sla_paused_duration_sec
            - Pushes sla_response_due forward by the elapsed interval
              (only if sla_response_due is set and not yet breached)
            - Pushes sla_resolve_due forward by the elapsed interval
              (only if sla_resolve_due is set)
            - Clears sla_paused_at = None

        All time math uses func.now() — never Python datetime.

        Returns an empty dict if no SLA-related action is needed.
        """
        if new_status == TicketStatus.pending and ticket.sla_paused_at is None:
            return {"sla_paused_at": func.now()}

        if (
            ticket.status == TicketStatus.pending
            and new_status != TicketStatus.pending
            and ticket.sla_paused_at is not None
        ):
            elapsed_expr = func.extract("epoch", func.now() - ticket.sla_paused_at)

            updates: dict[str, Any] = {
                "sla_paused_duration_sec": (
                    func.coalesce(ticket.sla_paused_duration_sec, 0) + elapsed_expr
                ),
                "sla_paused_at": None,
            }

            # Push response due forward by elapsed pause interval
            if ticket.sla_response_due is not None:
                updates["sla_response_due"] = ticket.sla_response_due + func.make_interval(
                    secs=elapsed_expr
                )

            # Push resolve due forward by elapsed pause interval
            if ticket.sla_resolve_due is not None:
                updates["sla_resolve_due"] = ticket.sla_resolve_due + func.make_interval(
                    secs=elapsed_expr
                )

            return updates

        return {}

    # ------------------------------------------------------------------
    # Private: business-hours arithmetic
    # ------------------------------------------------------------------

    def _add_working_minutes(
        self,
        start: datetime,
        minutes: int,
        bh: BusinessHoursConfig,
    ) -> datetime:
        """Advance `start` by `minutes` of working time, respecting bh config.

        Uses zoneinfo.ZoneInfo for timezone handling.
        Skips hours outside work_start_time..work_end_time,
        days not in work_days (ISO weekday 1=Mon..7=Sun),
        and any date listed in bh.holidays.
        """
        tz = ZoneInfo(bh.timezone)

        # Normalise to aware datetime in the tenant timezone
        if start.tzinfo is None:
            current = start.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)
        else:
            current = start.astimezone(tz)

        holiday_set: set[date] = set(bh.holidays) if bh.holidays else set()
        work_days: set[int] = set(bh.work_days)  # ISO weekday: 1=Mon..7=Sun

        work_start = bh.work_start_time
        work_end = bh.work_end_time

        remaining = minutes

        while remaining > 0:
            # Skip non-working days
            if (
                current.isoweekday() not in work_days
                or current.date() in holiday_set
            ):
                # Jump to start of next calendar day
                current = (current + timedelta(days=1)).replace(
                    hour=work_start.hour,
                    minute=work_start.minute,
                    second=0,
                    microsecond=0,
                )
                continue

            # If we're before the working window, jump to work_start
            day_start = current.replace(
                hour=work_start.hour,
                minute=work_start.minute,
                second=0,
                microsecond=0,
            )
            day_end = current.replace(
                hour=work_end.hour,
                minute=work_end.minute,
                second=0,
                microsecond=0,
            )

            if current < day_start:
                current = day_start

            if current >= day_end:
                # Past end of working day — move to next day
                current = (current + timedelta(days=1)).replace(
                    hour=work_start.hour,
                    minute=work_start.minute,
                    second=0,
                    microsecond=0,
                )
                continue

            # Working minutes available in this window
            available_minutes = int((day_end - current).total_seconds() / 60)

            if remaining <= available_minutes:
                current = current + timedelta(minutes=remaining)
                remaining = 0
            else:
                remaining -= available_minutes
                # Move to start of next working day
                current = (current + timedelta(days=1)).replace(
                    hour=work_start.hour,
                    minute=work_start.minute,
                    second=0,
                    microsecond=0,
                )

        return current
