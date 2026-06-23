"""Default SLA-breach escalation: the priority-ladder helper (pure, no DB)."""
import pytest

from app.models.ticket import TicketPriority
from app.services.sla_service import escalated_priority


def test_bumps_one_level_toward_critical():
    assert escalated_priority(TicketPriority.low) is TicketPriority.medium
    assert escalated_priority(TicketPriority.medium) is TicketPriority.high
    assert escalated_priority(TicketPriority.high) is TicketPriority.critical


def test_critical_stays_critical():
    assert escalated_priority(TicketPriority.critical) is TicketPriority.critical


def test_accepts_string_value():
    assert escalated_priority("low") is TicketPriority.medium
    assert escalated_priority("critical") is TicketPriority.critical
