"""Harden RLS tenant_isolation predicate against empty app.tenant_id (asyncpg).

Revision ID: 0032_rls_nullif
Revises: 0031_retro
Create Date: 2026-07-02

Long-standing "unexpected error / Tenant context" bug: the fail-open predicate
cast ``current_setting('app.tenant_id', true)::uuid`` throws
``invalid input syntax for type uuid: ""`` when the GUC is empty. Under psql's
simple protocol the OR short-circuits and hides it, but under asyncpg (the app
driver, extended/prepared protocol) the cast is evaluated even when an earlier
OR branch is already true — so EVERY RLS-policied query fails for a platform
session (which sets bypass_rls='on' but leaves app.tenant_id='').

Fix: wrap the cast in ``NULLIF(..., '')`` so an empty GUC yields NULL (the
comparison becomes NULL, never an error) while the fail-open branches still
apply. Rewrites the tenant_isolation policy on EVERY table that has it. Strictly
safer — same truth table minus the crash.
"""

import sqlalchemy as sa
from alembic import op

revision = "0032_rls_nullif"
down_revision = "0031_retro"
branch_labels = None
depends_on = None

_SAFE = """(
    current_setting('app.tenant_id', true) IS NULL
    OR current_setting('app.tenant_id', true) = ''
    OR current_setting('app.bypass_rls', true) = 'on'
    OR tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
)"""

_RAW = """(
    current_setting('app.tenant_id', true) IS NULL
    OR current_setting('app.tenant_id', true) = ''
    OR current_setting('app.bypass_rls', true) = 'on'
    OR tenant_id = current_setting('app.tenant_id', true)::uuid
)"""


def _rewrite(predicate: str) -> None:
    conn = op.get_bind()
    tables = conn.execute(
        sa.text("SELECT tablename FROM pg_policies WHERE policyname = 'tenant_isolation' ORDER BY tablename")
    ).scalars().all()
    for t in tables:
        op.execute(f"DROP POLICY tenant_isolation ON {t}")
        op.execute(f"CREATE POLICY tenant_isolation ON {t} USING {predicate} WITH CHECK {predicate}")


def upgrade() -> None:
    _rewrite(_SAFE)


def downgrade() -> None:
    _rewrite(_RAW)
