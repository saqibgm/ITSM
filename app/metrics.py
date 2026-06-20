"""
Prometheus metrics definitions (S4.2).

All metrics are module-level singletons — they register themselves once with
the default CollectorRegistry when this module is first imported.

Usage:
    from app.metrics import record_request, ai_budget_exhausted_total
    record_request("GET", "/api/v1/tickets", "200", 0.042)
    ai_budget_exhausted_total.labels(tenant_id="abc", feature="classifier").inc()
"""

from prometheus_client import Counter, Histogram

# ---------------------------------------------------------------------------
# HTTP metrics
# ---------------------------------------------------------------------------

http_requests_total = Counter(
    "itsm_http_requests_total",
    "Total HTTP requests",
    ["method", "path_template", "status_code"],
)

http_request_duration = Histogram(
    "itsm_http_request_duration_seconds",
    "HTTP request duration in seconds",
    ["method", "path_template"],
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0],
)

# ---------------------------------------------------------------------------
# AI / budget metrics
# ---------------------------------------------------------------------------

ai_budget_exhausted_total = Counter(
    "itsm_ai_budget_exhausted_total",
    "AI budget exhausted events",
    ["tenant_id", "feature"],
)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def record_request(
    method: str,
    path_template: str,
    status_code: str,
    duration_sec: float,
) -> None:
    """Increment http_requests_total and observe http_request_duration."""
    try:
        http_requests_total.labels(
            method=method,
            path_template=path_template,
            status_code=status_code,
        ).inc()
        http_request_duration.labels(
            method=method,
            path_template=path_template,
        ).observe(duration_sec)
    except Exception:
        # Metrics must never crash the request path.
        pass
