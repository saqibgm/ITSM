"""
EmbedderService — wraps OpenAI text-embedding-3-small with Redis caching.

Cache key: ``emb:{sha256(text)[:16]}``  TTL: 3 600 s
Batch calls are split into chunks of at most 100 texts per API request.
Raises ExternalServiceError("openai") on any OpenAI failure.
"""

import hashlib
import json
import logging
from typing import TYPE_CHECKING

from app.exceptions import ExternalServiceError

if TYPE_CHECKING:
    import openai

logger = logging.getLogger(__name__)

_EMBEDDING_MODEL = "text-embedding-3-small"
_EMBEDDING_DIMENSIONS = 1536
_CACHE_TTL = 3600          # seconds
_BATCH_MAX = 100           # max texts per OpenAI API call


class EmbedderService:
    """Generate and cache text embeddings using OpenAI text-embedding-3-small."""

    def __init__(self, openai_client: "openai.AsyncOpenAI", redis) -> None:
        self._openai = openai_client
        self._redis = redis

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _cache_key(text: str) -> str:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return f"emb:{digest[:16]}"

    async def _fetch_from_cache(self, key: str) -> list[float] | None:
        raw = await self._redis.get(key)
        if raw is None:
            return None
        return json.loads(raw)

    async def _store_in_cache(self, key: str, embedding: list[float]) -> None:
        await self._redis.set(key, json.dumps(embedding), ex=_CACHE_TTL)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def embed_text(self, text: str) -> list[float]:
        """Return a 1 536-dimension embedding for *text*.

        Checks Redis first; on miss calls the OpenAI Embeddings API and
        caches the result for 3 600 s.

        Raises:
            ExternalServiceError("openai"): on any OpenAI API failure.
        """
        key = self._cache_key(text)
        cached = await self._fetch_from_cache(key)
        if cached is not None:
            return cached

        try:
            response = await self._openai.embeddings.create(
                model=_EMBEDDING_MODEL,
                input=text,
                dimensions=_EMBEDDING_DIMENSIONS,
            )
            embedding: list[float] = response.data[0].embedding
        except Exception as exc:
            logger.error(
                "embedder_api_failure",
                extra={"exception_type": type(exc).__name__},
            )
            raise ExternalServiceError("openai") from exc

        await self._store_in_cache(key, embedding)
        return embedding

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Return embeddings for a list of texts.

        Checks Redis for each text individually; sends uncached texts to
        OpenAI in batches of up to 100.  Results are cached per-text.

        Args:
            texts: List of strings to embed (may be empty).

        Returns:
            A list of 1 536-dimension float vectors, one per input text,
            preserving input order.

        Raises:
            ExternalServiceError("openai"): on any OpenAI API failure.
        """
        if not texts:
            return []

        results: list[list[float] | None] = [None] * len(texts)
        uncached_indices: list[int] = []

        # Phase 1: serve from cache where possible
        for idx, text in enumerate(texts):
            key = self._cache_key(text)
            cached = await self._fetch_from_cache(key)
            if cached is not None:
                results[idx] = cached
            else:
                uncached_indices.append(idx)

        # Phase 2: batch-fetch uncached texts from OpenAI
        if uncached_indices:
            uncached_texts = [texts[i] for i in uncached_indices]

            # Split into chunks of _BATCH_MAX
            all_embeddings: list[list[float]] = []
            for chunk_start in range(0, len(uncached_texts), _BATCH_MAX):
                chunk = uncached_texts[chunk_start : chunk_start + _BATCH_MAX]
                try:
                    response = await self._openai.embeddings.create(
                        model=_EMBEDDING_MODEL,
                        input=chunk,
                        dimensions=_EMBEDDING_DIMENSIONS,
                    )
                    # API preserves input order
                    all_embeddings.extend([d.embedding for d in response.data])
                except Exception as exc:
                    logger.error(
                        "embedder_batch_api_failure",
                        extra={
                            "chunk_size": len(chunk),
                            "exception_type": type(exc).__name__,
                        },
                    )
                    raise ExternalServiceError("openai") from exc

            # Phase 3: store each in cache and fill results
            for list_pos, original_idx in enumerate(uncached_indices):
                embedding = all_embeddings[list_pos]
                results[original_idx] = embedding
                key = self._cache_key(texts[original_idx])
                await self._store_in_cache(key, embedding)

        # All slots must be filled by now
        return results  # type: ignore[return-value]
