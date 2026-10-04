"""Start an ASGI app with uvicorn on a selector event loop, with graceful drain.

    python infra/local/serve.py app.main:app --port 8001      (cwd = backend dir)

Why not `uvicorn ...` directly:
* On Windows its default loop is the ProactorEventLoop, and psycopg's async mode refuses to run on it.
* proxy_headers=False: client IP resolution is done by app.defence.clientip with an explicit TRUSTED_PROXIES
  list; uvicorn's own X-Forwarded-For handling would rewrite the peer address first.
* Graceful drain. On SIGTERM / Ctrl-Break (or POST /admin/defence/lifecycle/drain) the replica first flips to
  DRAINING: /readyz answers 503 so the gateway stops sending NEW requests, and open SSE streams are told to
  reconnect elsewhere. Only after DRAIN_GRACE_S (default 3 s, longer than the gateway's 0.25 s readiness poll) does
  uvicorn stop accepting; it then waits up to --graceful seconds for in-flight requests before exiting.
  A second signal skips the wait and exits at once.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import selectors
import sys
import threading

import uvicorn


def _lifecycle():
    try:  # the gateway has no defence package; it just behaves like plain uvicorn
        from app.defence import lifecycle

        return lifecycle
    except ImportError:
        return None


class DrainingServer(uvicorn.Server):
    """uvicorn.Server whose first termination signal starts a drain instead of stopping at once."""

    drain_grace_s: float = 3.0

    def handle_exit(self, sig, frame):  # uvicorn calls this for SIGTERM, SIGINT and (Windows) SIGBREAK
        lifecycle = _lifecycle()
        if lifecycle is None or lifecycle.is_draining() or self.should_exit:
            super().handle_exit(sig, frame)  # second signal, or nothing to drain: stop now
            return
        lifecycle.begin_drain(reason=f"signal {sig}")
        threading.Timer(self.drain_grace_s, lambda: uvicorn.Server.handle_exit(self, sig, frame)).start()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("app")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--graceful", type=int, default=25, help="seconds in-flight requests get after the server stops accepting")
    ap.add_argument("--log-level", default="info")
    a = ap.parse_args()

    sys.path.insert(0, os.getcwd())  # the app directory, as `uvicorn` would do
    config = uvicorn.Config(
        a.app, host=a.host, port=a.port, proxy_headers=False, log_level=a.log_level, timeout_graceful_shutdown=a.graceful
    )
    server = DrainingServer(config)
    server.drain_grace_s = float(os.environ.get("DRAIN_GRACE_S", "3"))
    lifecycle = _lifecycle()
    if lifecycle is not None:
        lifecycle.register_exit(lambda: setattr(server, "should_exit", True))
    asyncio.run(server.serve(), loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()))


if __name__ == "__main__":
    main()
