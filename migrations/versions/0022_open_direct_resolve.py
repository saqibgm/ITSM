"""Allow direct open -> resolved / open -> closed ticket transitions.

Operators were forced through open -> in_progress -> resolved; the UI also
offered statuses the state machine rejected (409 "Cannot transition from
'open' to 'resolved'"). Per product decision, an operator may resolve (or
close) an open ticket in one step. These rows extend the state machine in
``ticket_status_transitions`` (applies to all ticket types: ticket_type='').

Idempotent: ON CONFLICT DO NOTHING so re-runs / already-seeded envs are safe.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0022_open_direct_resolve"
down_revision = "0021_product_source"
branch_labels = None
depends_on = None


_NEW_TRANSITIONS = [
    ("open", "resolved", ""),
    ("open", "closed", ""),
]


def upgrade() -> None:
    for from_status, to_status, ticket_type in _NEW_TRANSITIONS:
        op.execute(
            "INSERT INTO ticket_status_transitions "
            "(from_status, to_status, ticket_type) "
            f"VALUES ('{from_status}', '{to_status}', '{ticket_type}') "
            "ON CONFLICT DO NOTHING"
        )


def downgrade() -> None:
    for from_status, to_status, ticket_type in _NEW_TRANSITIONS:
        op.execute(
            "DELETE FROM ticket_status_transitions "
            f"WHERE from_status = '{from_status}' "
            f"AND to_status = '{to_status}' "
            f"AND ticket_type = '{ticket_type}'"
        )
