"""System metrics from the recorder (METRICS.md §3): pooled latency percentiles and
per-run error/throughput rates."""
from __future__ import annotations

from fairdrop_sim.engine.recorder import Recorder
from fairdrop_sim.models.results import Percentiles

ENDPOINTS = ("enter", "status", "claim")


def percentiles(rec: Recorder, endpoint: str, cls: str | None = None) -> Percentiles:
    h = rec.hist(endpoint, cls)
    n = h.get_total_count()
    if n == 0:
        return Percentiles(p50=None, p95=None, p99=None, n=0)
    ms = lambda q: round(h.get_value_at_percentile(q) / 1000, 3)  # noqa: E731
    return Percentiles(p50=ms(50), p95=ms(95), p99=ms(99), n=n)


def latency_block(rec: Recorder) -> dict[str, Percentiles]:
    """enter/status/claim pooled, plus per-class keys where that class sent requests."""
    out: dict[str, Percentiles] = {}
    for ep in ENDPOINTS:
        out[ep] = percentiles(rec, ep)
        for cls in ("legit", "bot"):
            p = percentiles(rec, ep, cls)
            if p.n:
                out[f"{ep}.{cls}"] = p
    return out


def error_rates_per_run(rec: Recorder) -> dict[str, float]:
    """Fractions of requests, one run. 429 split by class; 5xx/timeout over all requests."""
    total = rec.count()
    legit = rec.count(cls="legit")
    bot = rec.count(cls="bot")
    conn = rec.count(outcome="CONN_ERROR")
    return {
        "http_429_legit": rec.count(cls="legit", outcome="429") / legit if legit else 0.0,
        "http_429_bot": rec.count(cls="bot", outcome="429") / bot if bot else 0.0,
        "http_5xx": rec.count(outcome="5xx") / total if total else 0.0,
        "timeout": (rec.count(outcome="TIMEOUT") + conn) / total if total else 0.0,
    }


def throughput_per_run(rec: Recorder) -> float | None:
    total = rec.count()
    dur = (rec.last_recv - rec.first_intended) if rec.first_intended is not None and rec.last_recv is not None else 0.0
    return round(total / dur, 2) if dur else None
