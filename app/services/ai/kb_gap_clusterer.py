"""
Greedy embedding-similarity clustering for KB search gaps
(KB_WIKI_CURATION_RAG_PLAN Phase 5).

Pure Python/math, no DB or LLM calls — testable in isolation. Given a flat
list of gap rows (each with a precomputed embedding), groups them into
same-tenant clusters and returns only the clusters that meet the minimum
volume threshold, the input to a curation draft.
"""

import math
from typing import TypedDict


class GapRow(TypedDict):
    id: object  # opaque row identifier (int/str), just carried through
    query: str
    org_id: str
    embedding: list[float]


class GapCluster(TypedDict):
    org_id: str
    gap_ids: list
    queries: list[str]
    count: int


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _average(vectors: list[list[float]]) -> list[float]:
    n = len(vectors)
    return [sum(v[i] for v in vectors) / n for i in range(len(vectors[0]))]


def cluster_gaps(
    gaps: list[GapRow],
    similarity_threshold: float = 0.85,
    min_count: int = 5,
) -> list[GapCluster]:
    """Greedily cluster gaps by embedding cosine similarity, never crossing
    org_id boundaries. Returns only clusters with count >= min_count."""
    by_org: dict[str, list[GapRow]] = {}
    for gap in gaps:
        by_org.setdefault(gap["org_id"], []).append(gap)

    results: list[GapCluster] = []
    for org_id, org_gaps in by_org.items():
        # Each internal cluster: {"gap_ids": [...], "queries": [...], "embeddings": [...]}
        clusters: list[dict] = []
        for gap in org_gaps:
            best = None
            best_score = 0.0
            for cluster in clusters:
                centroid = _average(cluster["embeddings"])
                score = _cosine(gap["embedding"], centroid)
                if score >= similarity_threshold and score > best_score:
                    best, best_score = cluster, score
            if best is not None:
                best["gap_ids"].append(gap["id"])
                best["queries"].append(gap["query"])
                best["embeddings"].append(gap["embedding"])
            else:
                clusters.append({
                    "gap_ids": [gap["id"]],
                    "queries": [gap["query"]],
                    "embeddings": [gap["embedding"]],
                })

        for cluster in clusters:
            if len(cluster["gap_ids"]) >= min_count:
                results.append({
                    "org_id": org_id,
                    "gap_ids": cluster["gap_ids"],
                    "queries": cluster["queries"],
                    "count": len(cluster["gap_ids"]),
                })

    return results
