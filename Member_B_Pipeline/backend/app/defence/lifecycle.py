"""Graceful shutdown ("drain") of one replica.

Sequence on SIGTERM / Ctrl-Break (infra/local/serve.py) or on POST /admin/defence/lifecycle/drain:

  1. begin_drain(): the replica flips to DRAINING. /readyz answers 503, so the gateway (which polls readiness
     every second) stops routing NEW requests to it. It keeps serving: nothing is refused yet.
  2. Open server-sent-event streams are told to reconnect (a final `event: reconnect` plus `retry:` hint) and
     end; the browser's reconnect lands on a healthy replica through the gateway.
  3. After a grace period (longer than the gateway's readiness poll, default 3 s) the server stops accepting
     and uvicorn waits for in-flight requests to finish (timeout_graceful_shutdown), then exits.

If the grace period were shorter than the readiness poll, the gateway could still send a request to a replica that
has already stopped listening: that is the only reason the delay exists.
A's real /readyz must use is_draining() too (docs/INTERFACE_REQUESTS_B.md R8).
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from . import metrics

log = logging.getLogger("fd.lifecycle")

_draining = False
_drain_event: asyncio.Event | None = None
_exit_cb: Callable[[], None] | None = None


def is_draining() -> bool:
    return _draining


def drain_event() -> asyncio.Event:
    """Set when draining starts. Created lazily so it belongs to the running loop."""
    global _drain_event
    if _drain_event is None:
        _drain_event = asyncio.Event()
        if _draining:
            _drain_event.set()
    return _drain_event


def register_exit(cb: Callable[[], None]) -> None:
    """The process launcher registers how to stop the server (uvicorn's should_exit)."""
    global _exit_cb
    _exit_cb = cb


def begin_drain(exit_after_s: float | None = None, reason: str = "") -> bool:
    """Idempotent. Returns True if this call started the drain. `exit_after_s` schedules the actual stop."""
    global _draining
    first = not _draining
    _draining = True
    metrics.DRAINING.set(1)
    try:
        drain_event().set()
    except RuntimeError:  # no running loop (called from a signal handler thread): the flag is enough, streams poll it
        pass
    if first:
        log.warning("DRAINING (%s): readiness is now 503, streams will be told to reconnect", reason or "requested")
    if exit_after_s is not None and _exit_cb is not None:
        try:
            asyncio.get_running_loop().call_later(exit_after_s, _exit_cb)
        except RuntimeError:
            import threading

            threading.Timer(exit_after_s, _exit_cb).start()
    return first


def reset_for_tests() -> None:
    global _draining, _drain_event, _exit_cb
    _draining, _drain_event, _exit_cb = False, None, None
    metrics.DRAINING.set(0)
