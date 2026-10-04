"""What does the target server actually implement?

Detected from the server's own OpenAPI document (`GET /openapi.json`, which FastAPI serves by
default and which both A's backend and the mock expose), so we never guess from a 404 whose
meaning is ambiguous. If a server exposes no OpenAPI document we fall back to probing and say so.

The point is to turn "Member A is not finished" into a precise, showable list, and to stop a run
from publishing fairness numbers that an incomplete engine could not have produced (a missing
draw would otherwise show up as a convincing 0% human win rate).
"""
from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx

# capability -> (HTTP method, path suffix after an {event_id}-style segment, or an absolute path)
_EVENT_ROUTES: dict[str, tuple[str, str]] = {
    "draw": ("post", "/draw"),
    "claim": ("post", "/claim"),
    "reset": ("post", "/reset"),
    "stats": ("get", "/stats"),
    "invariants": ("get", "/invariants"),
    "stream": ("get", "/stream"),
    "enter": ("post", "/enter"),
    "status": ("get", "/status"),
    "open": ("post", "/open"),
    "close": ("post", "/close"),
    "schedule": ("post", "/schedule"),
}
_ABSOLUTE_ROUTES: dict[str, tuple[str, str]] = {
    "readyz": ("get", "/readyz"),
    "defence_presets": ("get", "/admin/defence/presets"),
    "defence_decisions": ("get", "/admin/defence/decisions"),
    "sim_tokens": ("post", "/admin/sim/tokens"),
    "defence_challenge": ("post", "/defence/challenge"),
}

# What a complete, honest lottery run needs from the engine. Defences are optional (a run
# with defence preset "none" is valid); the rest are not.
REQUIRED_FOR_FULL_RUN = ("schedule", "open", "close", "enter", "status", "draw", "claim")
REQUIRED_FOR_REPEATS = ("reset",)  # or a fresh event per run, which the real driver does instead


@dataclass
class Capabilities:
    kind: str = "unknown"  # "mock" | "real"
    reachable: bool = False
    base_url: str = ""
    source: str = "openapi"  # openapi | probe
    routes: dict[str, bool] = field(default_factory=dict)
    openapi_title: str | None = None
    notes: list[str] = field(default_factory=list)

    def has(self, name: str) -> bool:
        return bool(self.routes.get(name))

    @property
    def draw(self) -> bool:
        return self.has("draw")

    def missing(self, names: tuple[str, ...] = REQUIRED_FOR_FULL_RUN) -> list[str]:
        return [n for n in names if not self.has(n)]

    @property
    def complete(self) -> bool:
        return self.reachable and not self.missing()

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["missing_for_full_run"] = self.missing()
        d["complete"] = self.complete
        return d


def _match(paths: dict[str, Any], method: str, suffix: str, event_scoped: bool) -> bool:
    for path, ops in paths.items():
        if method not in ops:
            continue
        if event_scoped:
            if re.search(r"/events/\{[^}/]+\}" + re.escape(suffix) + r"$", path):
                return True
        elif path == suffix:
            return True
    return False


def from_openapi(doc: dict[str, Any]) -> dict[str, bool]:
    paths = doc.get("paths", {})
    out = {name: _match(paths, m, suf, True) for name, (m, suf) in _EVENT_ROUTES.items()}
    out.update({name: _match(paths, m, suf, False) for name, (m, suf) in _ABSOLUTE_ROUTES.items()})
    return out


async def probe(base_url: str, timeout: float = 5.0) -> Capabilities:
    """Reachability + routes. Never raises: an unreachable server is reported as such."""
    caps = Capabilities(base_url=base_url)
    async with httpx.AsyncClient(base_url=base_url, timeout=timeout) as c:
        health: dict[str, Any] | None = None
        for path in ("/health", "/healthz"):
            try:
                r = await c.get(path)
            except httpx.HTTPError:
                continue
            if r.status_code == 200:
                caps.reachable = True
                try:
                    health = r.json()
                except ValueError:
                    health = {}
                break
        if not caps.reachable:
            caps.notes.append(f"nothing answered /health or /healthz at {base_url}")
            return caps
        caps.kind = "mock" if (health or {}).get("mock") is True else "real"
        try:
            r = await c.get("/openapi.json")
            if r.status_code == 200:
                doc = r.json()
                caps.routes = from_openapi(doc)
                caps.openapi_title = (doc.get("info") or {}).get("title")
            else:
                caps.source = "probe"
                caps.notes.append(f"/openapi.json answered {r.status_code}; capabilities are unknown")
        except (httpx.HTTPError, ValueError):
            caps.source = "probe"
            caps.notes.append("no readable /openapi.json; capabilities are unknown")
    if caps.kind == "real":
        verifier = bool(os.environ.get("FD_VERIFY_CMD"))
        caps.routes["verifier"] = verifier
        if not verifier:
            caps.notes.append("no draw verifier configured (set FD_VERIFY_CMD, e.g. "
                              "'python -m app.verify_draw {event_id}' with FD_VERIFY_CWD=<backend dir>): "
                              "draw_verified will be null")
    else:
        caps.routes["verifier"] = True  # the mock verifies in-process
    return caps


def describe_gap(caps: Capabilities) -> str:
    """One sentence a UI can show as-is."""
    if not caps.reachable:
        return f"The real Fair Drop stack is not reachable at {caps.base_url}."
    miss = caps.missing()
    if not miss:
        return ""
    return (f"The real engine at {caps.base_url} is reachable but incomplete: it has no "
            f"{', '.join(miss)} endpoint(s) yet. Member A's backend must implement them before a "
            "full run can produce fairness numbers.")
