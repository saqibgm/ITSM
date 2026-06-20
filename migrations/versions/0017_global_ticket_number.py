"""System-wide ticket numbering: single global sequence, no type prefix.

Revision ID: 0017_global_ticket_number
Revises: 0016_kb_article_author_nullable
Create Date: 2026-06-09

Replaces the per-tenant, type-prefixed ticket numbers (``INC-00001``) with a
single global, zero-padded sequence (``00001``) that is unique system-wide.

  - Creates sequence ``ticket_number_seq``.
  - Renumbers existing tickets globally, ordered by creation time, to
    ``LPAD(n, 5, '0')`` and advances the sequence past the highest value.
  - Adds a global UNIQUE index on tickets.ticket_number.

Asset numbering (AST- via tenant_sequences) is unchanged.
"""

from alembic import op

revision = "0017_global_ticket_number"
down_revision = "0016_kb_article_author_nullable"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SEQUENCE IF NOT EXISTS ticket_number_seq")

    # Renumber every existing ticket into one global 5-digit sequence and set
    # the sequence so the next nextval() continues after the highest number.
    op.execute(
        """
        DO $$
        DECLARE n bigint;
        BEGIN
            WITH ordered AS (
                SELECT id, row_number() OVER (ORDER BY created_at, id) AS rn
                FROM tickets
            )
            UPDATE tickets t
               SET ticket_number = lpad(o.rn::text, 5, '0')
              FROM ordered o
             WHERE t.id = o.id;

            SELECT count(*) INTO n FROM tickets;
            IF n > 0 THEN
                PERFORM setval('ticket_number_seq', n, true);
            ELSE
                PERFORM setval('ticket_number_seq', 1, false);
            END IF;
        END $$;
        """
    )

    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_tickets_ticket_number "
        "ON tickets (ticket_number)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_tickets_ticket_number")
    op.execute("DROP SEQUENCE IF EXISTS ticket_number_seq")
    # Old per-tenant/prefixed numbers are not restored.
