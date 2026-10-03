"""Run the API: `python -m app.serve [--host 0.0.0.0] [--port 8000]`.

Use this instead of the bare `uvicorn` CLI on Windows: uvicorn creates its own
ProactorEventLoop there, which psycopg's async mode cannot use. On Linux this
is equivalent to `uvicorn app.main:app`. Run several processes on different
ports for multiple replicas; they share no state except Postgres.
"""
from __future__ import annotations

import argparse
import asyncio
import selectors
import sys

import uvicorn


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--log-level", default="info")
    args = p.parse_args(argv)

    config = uvicorn.Config("app.main:app", host=args.host, port=args.port,
                            log_level=args.log_level, proxy_headers=True)
    server = uvicorn.Server(config)
    if sys.platform == "win32":
        asyncio.run(server.serve(),
                    loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()))
    else:
        server.run()


if __name__ == "__main__":
    main()
