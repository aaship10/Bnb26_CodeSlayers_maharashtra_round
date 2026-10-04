"""Measurement store for one shard: HDR histograms, outcome counters and a per-second
timeline. Serializable (`to_dict`) so shards can run in separate processes and be
merged exactly (`merge`).

Keys are "<endpoint>.<class>", e.g. "enter.legit". Times are perf_counter seconds;
histograms store microseconds (1 us .. 120 s, 3 significant digits).
"""
from __future__ import annotations

from collections import Counter
from typing import Any

from hdrh.histogram import HdrHistogram

H_MIN_US, H_MAX_US, H_DIGITS = 1, 120_000_000, 3
FAILURE_OUTCOMES = frozenset({"5xx", "TIMEOUT", "CONN_ERROR"})  # the server did not answer, or broke


def new_hist() -> HdrHistogram:
    return HdrHistogram(H_MIN_US, H_MAX_US, H_DIGITS)


def _us(seconds: float) -> int:
    return min(H_MAX_US, max(H_MIN_US, int(seconds * 1_000_000)))


def outcome_of(status: int, code: str | None) -> str:
    """Bucket a response: ok | 429 | 4xx:<CODE> | 5xx | TIMEOUT | CONN_ERROR | BAD_RESPONSE."""
    if 200 <= status < 300:
        return "ok"
    if status == 429:
        return "429"
    if status >= 500:
        return "5xx"
    if status == 0:
        return code or "CONN_ERROR"
    return f"4xx:{code}"


class Recorder:
    def __init__(self, t0: float = 0.0):
        self.t0 = t0  # perf_counter of window open; timeline buckets are seconds since t0
        self.open_loop: dict[str, HdrHistogram] = {}
        self.service: dict[str, HdrHistogram] = {}
        self.sched_lag = new_hist()
        self.counts: Counter[str] = Counter()  # "<endpoint>.<class>|<outcome>"
        self.timeline: Counter[str] = Counter()  # "<second>|<endpoint>.<class>|<outcome>", by INTENDED send time
        # "<tenth of a second>|ok" / "...|fail" by COMPLETION time: when a failure is observed. This is
        # what an outage is measured on; bucketing by intended time smears failures over the seconds the
        # requests were scheduled in and hides a dip (found by the first real chaos run).
        self.completions: Counter[str] = Counter()
        self.first_intended: float | None = None
        self.last_recv: float | None = None

    def lag(self, seconds: float) -> None:
        self.sched_lag.record_value(_us(max(seconds, 1e-6)))

    def request(self, endpoint: str, cls: str, status: int, code: str | None,
                intended: float, t_send: float, t_recv: float) -> None:
        key = f"{endpoint}.{cls}"
        if key not in self.open_loop:
            self.open_loop[key] = new_hist()
            self.service[key] = new_hist()
        self.open_loop[key].record_value(_us(t_recv - intended))
        self.service[key].record_value(_us(t_recv - t_send))
        out = outcome_of(status, code)
        self.counts[f"{key}|{out}"] += 1
        self.timeline[f"{int(intended - self.t0)}|{key}|{out}"] += 1
        self.completions[f"{int((t_recv - self.t0) * 10)}|{'fail' if out in FAILURE_OUTCOMES else 'ok'}"] += 1
        if self.first_intended is None or intended < self.first_intended:
            self.first_intended = intended
        if self.last_recv is None or t_recv > self.last_recv:
            self.last_recv = t_recv

    # ------------------------------------------------------------------ (de)serialise

    def to_dict(self) -> dict[str, Any]:
        enc = lambda h: h.encode().decode("ascii")  # noqa: E731
        return {
            "t0": self.t0,
            "open_loop": {k: enc(h) for k, h in self.open_loop.items()},
            "service": {k: enc(h) for k, h in self.service.items()},
            "sched_lag": enc(self.sched_lag),
            "counts": dict(self.counts),
            "timeline": dict(self.timeline),
            "completions": dict(self.completions),
            "first_intended": self.first_intended,
            "last_recv": self.last_recv,
        }

    def merge(self, d: dict[str, Any]) -> None:
        for attr in ("open_loop", "service"):
            mine: dict[str, HdrHistogram] = getattr(self, attr)
            for k, encoded in d[attr].items():
                mine.setdefault(k, new_hist()).decode_and_add(encoded)
        self.sched_lag.decode_and_add(d["sched_lag"])
        self.counts.update(d["counts"])
        self.timeline.update(d["timeline"])
        self.completions.update(d.get("completions", {}))  # absent in recorders written before this field
        if d["first_intended"] is not None:
            opts = [v for v in (self.first_intended, d["first_intended"]) if v is not None]
            self.first_intended = min(opts)
        if d["last_recv"] is not None:
            opts = [v for v in (self.last_recv, d["last_recv"]) if v is not None]
            self.last_recv = max(opts)

    @classmethod
    def merged(cls, parts: list[dict[str, Any]], t0: float) -> "Recorder":
        r = cls(t0)
        for p in parts:
            r.merge(p)
        return r

    # ------------------------------------------------------------------ queries

    def hist(self, endpoint: str, cls: str | None = None, kind: str = "open_loop") -> HdrHistogram:
        """Histogram for an endpoint, one class or all classes combined."""
        src: dict[str, HdrHistogram] = getattr(self, kind)
        out = new_hist()
        for k, h in src.items():
            ep, c = k.split(".", 1)
            if ep == endpoint and (cls is None or c == cls):
                out.add(h)
        return out

    def count(self, endpoint: str | None = None, cls: str | None = None, outcome: str | None = None) -> int:
        total = 0
        for k, n in self.counts.items():
            key, out = k.split("|", 1)
            ep, c = key.split(".", 1)
            if (endpoint is None or ep == endpoint) and (cls is None or c == cls) and (
                outcome is None or out == outcome or (outcome == "4xx" and out.startswith("4xx:"))
            ):
                total += n
        return total
