"""
Read-only state machine definition for ticket status transitions.

Rows are seeded once in the initial migration and never modified by the
application. The service layer queries this table before accepting a
status-change request — invalid transitions return HTTP 409.

ticket_type convention
----------------------
- ``""``       (empty string) — transition applies to ALL ticket types.
- ``"change"`` — transition applies only to change-type tickets.

Using empty string instead of NULL avoids the PostgreSQL restriction on
NULL values in primary key columns.

Application query pattern::

    SELECT 1 FROM ticket_status_transitions
    WHERE from_status = :from AND to_status = :to
    AND (ticket_type = '' OR ticket_type = :current_type)
"""

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class TicketStatusTransition(Base):
    """Valid (from_status, to_status) pairs for the ticket state machine."""

    __tablename__ = "ticket_status_transitions"

    from_status: Mapped[str] = mapped_column(
        sa.VARCHAR(50),
        primary_key=True,
        nullable=False,
    )
    to_status: Mapped[str] = mapped_column(
        sa.VARCHAR(50),
        primary_key=True,
        nullable=False,
    )
    ticket_type: Mapped[str] = mapped_column(
        sa.VARCHAR(50),
        primary_key=True,
        nullable=False,
        server_default=sa.text("''"),
        comment="'' = all types; 'change' = change tickets only",
    )
