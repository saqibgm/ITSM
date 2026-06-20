"""
DuplicateDetector — pgvector cosine similarity search for duplicate tickets.

Uses parameterised SQL bindings; embedding vectors are NEVER interpolated
into query strings.
"""

import logging
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai_ticket import AIDuplicateCandidate
from app.config import get_settings

logger = logging.getLogger(__name__)

# Model version tag stored on each candidate row
_MODEL_VERSION = "text-embedding-3-small-1536"


class DuplicateDetector:
    """Find near-duplicate tickets via pgvector cosine similarity."""

    async def find_duplicates(
        self,
        db: AsyncSession,
        ticket_id: UUID,
        tenant_id: UUID,
        embedding: list[float],
        threshold: float = 0.85,
        limit: int = 5,
    ) -> list[AIDuplicateCandidate]:
        """Run pgvector cosine similarity against open tickets in the tenant.

        The query uses a parameterised binding for the embedding vector —
        it is never interpolated into the SQL string.

        Only tickets that are not closed/cancelled, belong to the same tenant,
        and have a stored embedding are searched.  Results below *threshold*
        are discarded.  Matching candidates are inserted as
        AIDuplicateCandidate rows and returned.

        Args:
            db:         Async SQLAlchemy session.
            ticket_id:  The newly created ticket's UUID.
            tenant_id:  The tenant UUID (strict isolation — no cross-tenant).
            embedding:  1 536-dimension vector for the new ticket's description.
            threshold:  Minimum cosine similarity to consider a duplicate
                        (default 0.85).
            limit:      Maximum candidates to return (default 5).

        Returns:
            List of persisted AIDuplicateCandidate rows (may be empty).
        """
        s = get_settings()
        effective_threshold = threshold if threshold is not None else s.AI_DUPLICATE_THRESHOLD

        # Build the embedding literal for pgvector.
        # We cast the Python list to a PostgreSQL vector via a bind parameter
        # using sqlalchemy's text() with named params — NEVER string formatting.
        embedding_str = "[" + ",".join(str(v) for v in embedding) + "]"

        similarity_query = text(
            """
            SELECT
                id,
                1 - (description_embedding <=> CAST(:emb AS vector)) AS score
            FROM tickets
            WHERE tenant_id      = :tenant_id
              AND id             != :ticket_id
              AND status NOT IN ('closed', 'cancelled')
              AND description_embedding IS NOT NULL
            ORDER BY description_embedding <=> CAST(:emb AS vector)
            LIMIT :limit
            """
        ).bindparams(
            emb=embedding_str,
            tenant_id=str(tenant_id),
            ticket_id=str(ticket_id),
            limit=limit,
        )

        result = await db.execute(similarity_query)
        rows = result.fetchall()

        candidates: list[AIDuplicateCandidate] = []

        for row in rows:
            candidate_uuid = row[0]
            score = float(row[1])

            if score < effective_threshold:
                continue

            candidate = AIDuplicateCandidate(
                ticket_id=ticket_id,
                candidate_id=candidate_uuid,
                similarity_score=score,
                model_version=_MODEL_VERSION,
                dismissed=False,
            )
            db.add(candidate)
            candidates.append(candidate)

        if candidates:
            await db.flush()
            for c in candidates:
                await db.refresh(c)

        logger.info(
            "duplicate_detection_complete",
            extra={
                "ticket_id": str(ticket_id),
                "tenant_id": str(tenant_id),
                "candidates_found": len(candidates),
                "threshold": effective_threshold,
            },
        )

        return candidates
