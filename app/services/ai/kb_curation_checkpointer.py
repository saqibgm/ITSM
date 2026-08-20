"""
Sync Postgres connection for LangGraph's checkpointer (KB curation graph,
KB_WIKI_CURATION_RAG_PLAN Phase 2).

langgraph-checkpoint-postgres needs its own sync psycopg (v3) connection —
separate from the app's async SQLAlchemy engine (app/database.py, which uses
asyncpg via a `postgresql+asyncpg://` DSN). psycopg needs the plain
`postgresql://` form, which isn't stored anywhere else in this codebase, so
it's derived here rather than duplicated in config.

The checkpointer's own tables (checkpoints, checkpoint_writes, etc.) are NOT
Alembic-managed — they're owned by langgraph-checkpoint-postgres, created via
.setup() (idempotent — CREATE TABLE IF NOT EXISTS internally), called on
every use rather than as a separate one-time operational step.
"""

from contextlib import asynccontextmanager
from typing import AsyncIterator

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from app.config import get_settings


def _sync_dsn() -> str:
    return get_settings().DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://", 1)


@asynccontextmanager
async def get_checkpointer() -> AsyncIterator[AsyncPostgresSaver]:
    """Yields a ready-to-use AsyncPostgresSaver, tables created if missing."""
    async with AsyncPostgresSaver.from_conn_string(_sync_dsn()) as checkpointer:
        await checkpointer.setup()
        yield checkpointer
