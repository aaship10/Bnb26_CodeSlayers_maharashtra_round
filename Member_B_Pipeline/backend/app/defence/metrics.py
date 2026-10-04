"""Prometheus metrics for the defence package, on a PRIVATE registry (no surprise default collectors).

Every label has bounded cardinality: routes are the route TEMPLATE (`/events/{event_id}/enter`), never the raw
path with ids; `weight` is one of 1.0 / 0.5 / 0.25 / none; reasons and scopes are fixed vocabularies.
Nothing here can need, and nothing here reads, Member C's ground-truth label.

One replica = one process, so plain (non-multiprocess) prometheus_client is correct. Scrape each replica at
its own `/metrics`; the gateway deliberately does NOT expose it publicly (and aggregates it for /ops).
"""
from __future__ import annotations

import time
from typing import Any

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from prometheus_client.exposition import CONTENT_TYPE_LATEST
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REGISTRY = CollectorRegistry(auto_describe=True)

HTTP_REQUESTS = Counter("fd_http_requests_total", "HTTP requests by route template and status", ["method", "route", "status"], registry=REGISTRY)
HTTP_LATENCY = Histogram(
    "fd_http_request_duration_seconds", "HTTP request latency by route template", ["route"], registry=REGISTRY,
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
HTTP_INFLIGHT = Gauge("fd_http_requests_in_flight", "Requests currently being served", registry=REGISTRY)

GATE_DECISIONS = Counter("fd_gate_decisions_total", "entry_gate decisions", ["action", "layer", "weight"], registry=REGISTRY)
RISK_BANDS = Counter("fd_risk_band_total", "Risk assessments by band", ["band"], registry=REGISTRY)
RATE_LIMITED = Counter("fd_rate_limited_total", "429 responses by endpoint and the scope that denied", ["endpoint", "scope"], registry=REGISTRY)
CHALLENGES_ISSUED = Counter("fd_challenges_issued_total", "Challenges issued", ["type"], registry=REGISTRY)
CHALLENGES_SOLVED = Counter("fd_challenges_solved_total", "Challenges solved correctly", ["type"], registry=REGISTRY)
CHALLENGES_FAILED = Counter("fd_challenges_failed_total", "Challenge attempts that did not pass", ["type", "reason"], registry=REGISTRY)

REDIS_ERRORS = Counter("fd_redis_errors_total", "Redis failures by operation", ["op"], registry=REGISTRY)
PG_ERRORS = Counter("fd_pg_errors_total", "Postgres failures by operation", ["op"], registry=REGISTRY)
LIMITER_DEGRADED = Counter("fd_limiter_degraded_total", "Requests handled while Redis was unusable, by policy", ["mode"], registry=REGISTRY)
BREAKER_OPEN = Gauge("fd_redis_breaker_open", "1 while the Redis circuit breaker is open (calls are skipped)", registry=REGISTRY)
BREAKER_TRIPS = Counter("fd_redis_breaker_trips_total", "Times the Redis circuit breaker opened", registry=REGISTRY)
DRAINING = Gauge("fd_draining", "1 once this replica started draining (readiness is 503)", registry=REGISTRY)
SSE_OPEN = Gauge("fd_sse_streams_open", "Open server-sent-event streams", registry=REGISTRY)
SSE_CLOSED_FOR_DRAIN = Counter("fd_sse_streams_closed_for_drain_total", "SSE streams closed with a reconnect hint during drain", registry=REGISTRY)

DECISION_LOG_WRITTEN = Gauge("fd_decision_log_written", "Decisions written to Postgres by this process", registry=REGISTRY)
DECISION_LOG_DROPPED = Gauge("fd_decision_log_dropped", "Decisions dropped because the queue was full", registry=REGISTRY)
DECISION_LOG_FAILED = Gauge("fd_decision_log_failed_batches", "Decision batches dropped after a failed write", registry=REGISTRY)
DECISION_LOG_QUEUE = Gauge("fd_decision_log_queue", "Decisions waiting to be written", registry=REGISTRY)

# Names the committed dashboards query: test_observability.py fails if a query names a metric that is not here.
ALL_METRIC_NAMES = {
    "fd_http_requests_total", "fd_http_request_duration_seconds", "fd_http_requests_in_flight", "fd_gate_decisions_total",
    "fd_risk_band_total", "fd_rate_limited_total", "fd_challenges_issued_total", "fd_challenges_solved_total",
    "fd_challenges_failed_total", "fd_redis_errors_total", "fd_pg_errors_total", "fd_limiter_degraded_total",
    "fd_redis_breaker_open", "fd_redis_breaker_trips_total", "fd_draining", "fd_sse_streams_open",
    "fd_sse_streams_closed_for_drain_total", "fd_decision_log_written", "fd_decision_log_dropped",
    "fd_decision_log_failed_batches", "fd_decision_log_queue",
}


def render() -> tuple[bytes, str]:
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST


def redis_error(op: str) -> None:
    REDIS_ERRORS.labels(op).inc()


def pg_error(op: str) -> None:
    PG_ERRORS.labels(op).inc()


def bind_decision_log(log: Any) -> None:
    """Expose the decision writer's counters without touching its hot path (read at scrape time)."""
    DECISION_LOG_WRITTEN.set_function(lambda: log.written)
    DECISION_LOG_DROPPED.set_function(lambda: log.dropped)
    DECISION_LOG_FAILED.set_function(lambda: log.failed_batches)
    DECISION_LOG_QUEUE.set_function(lambda: log._q.qsize())


class MetricsMiddleware:
    """Pure ASGI (no response buffering, safe for SSE). Records count and latency by route TEMPLATE."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] == "/metrics":
            await self.app(scope, receive, send)
            return
        start = time.perf_counter()
        status = {"code": 500}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                from . import lifecycle  # lazy: lifecycle imports this module

                if lifecycle.is_draining():
                    # Tells the gateway to stop routing here at once, without waiting for its next /readyz poll.
                    message = {**message, "headers": [*message.get("headers", []), (b"x-draining", b"1")]}
            await send(message)

        HTTP_INFLIGHT.inc()
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            HTTP_INFLIGHT.dec()
            route = getattr(scope.get("route"), "path", None) or "unmatched"  # set by the router once matched
            HTTP_REQUESTS.labels(scope["method"], route, str(status["code"])).inc()
            if route != "unmatched":  # 404-scans must not create unbounded label sets
                HTTP_LATENCY.labels(route).observe(time.perf_counter() - start)
