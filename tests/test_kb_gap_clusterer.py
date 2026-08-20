"""Pure unit tests for app/services/ai/kb_gap_clusterer.py (no DB, no LLM calls)."""

from app.services.ai.kb_gap_clusterer import cluster_gaps

_SIMILAR_A = [1.0, 0.0, 0.0]
_SIMILAR_B = [0.99, 0.05, 0.0]
_DIFFERENT = [0.0, 1.0, 0.0]


def _gap(id_, query, org_id, embedding):
    return {"id": id_, "query": query, "org_id": org_id, "embedding": embedding}


def test_similar_gaps_cluster_together_and_meet_threshold():
    gaps = [
        _gap(1, "how do refunds work", "org-a", _SIMILAR_A),
        _gap(2, "how does refund work", "org-a", _SIMILAR_B),
        _gap(3, "refund process question", "org-a", _SIMILAR_A),
    ]
    clusters = cluster_gaps(gaps, similarity_threshold=0.9, min_count=3)
    assert len(clusters) == 1
    assert clusters[0]["count"] == 3
    assert set(clusters[0]["gap_ids"]) == {1, 2, 3}


def test_dissimilar_gaps_form_separate_clusters_and_can_miss_threshold():
    gaps = [
        _gap(1, "refund question", "org-a", _SIMILAR_A),
        _gap(2, "shipping question", "org-a", _DIFFERENT),
    ]
    clusters = cluster_gaps(gaps, similarity_threshold=0.9, min_count=2)
    assert clusters == []  # neither cluster reaches min_count=2 on its own


def test_clustering_never_crosses_org_boundary():
    gaps = [
        _gap(1, "refund question", "org-a", _SIMILAR_A),
        _gap(2, "refund question", "org-b", _SIMILAR_A),
        _gap(3, "refund question", "org-a", _SIMILAR_B),
    ]
    clusters = cluster_gaps(gaps, similarity_threshold=0.9, min_count=2)
    assert len(clusters) == 1
    assert clusters[0]["org_id"] == "org-a"
    assert set(clusters[0]["gap_ids"]) == {1, 3}


def test_below_min_count_is_excluded():
    gaps = [
        _gap(1, "refund question", "org-a", _SIMILAR_A),
        _gap(2, "refund question", "org-a", _SIMILAR_B),
    ]
    clusters = cluster_gaps(gaps, similarity_threshold=0.9, min_count=5)
    assert clusters == []


def test_empty_input_returns_empty_list():
    assert cluster_gaps([], similarity_threshold=0.9, min_count=1) == []
