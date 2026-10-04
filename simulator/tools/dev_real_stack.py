"""Stand up Member A's REAL backend for development, with no PostgreSQL install and on Python 3.11.

    # once: a venv that has A's backend dependencies plus a bundled Postgres
    py -3.11 -m venv .venv_a
    .venv_a\\Scripts\\python -m pip install --ignore-requires-python -e ..\\backend[dev] pgserver psycopg[binary]
    # then
    .venv_a\\Scripts\\python tools\\dev_real_stack.py up --backend-dir ..\\backend [--replicas 1] [--api-port 8000]
    .venv_a\\Scripts\\python tools\\dev_real_stack.py status
    .venv_a\\Scripts\\python tools\\dev_real_stack.py down

`up` starts a private Postgres (bundled by the `pgserver` wheel), creates the `fairdrop` database, applies A's
Alembic migrations, and launches A's UNMODIFIED app on 127.0.0.1:<api-port> (more replicas on the next ports).
It prints the two variables the simulator needs:

    FD_REAL_URL, FD_REAL_DSN

What this is NOT: it is not what A and B ship (A's own `app.serve` needs Python 3.12; B's launcher and gateway
are separate), the bundled Postgres has default tuning, and everything shares one laptop with the load generator.
Numbers from it are dev-stack numbers; say so wherever they are quoted.

Windows limit worth knowing: this launcher, like A's `app.serve` and B's `serve.py`, runs on a selector event loop
(psycopg's async mode requires one), and Windows' select() is capped at 512 sockets, so ONE replica dies (it does
not degrade) above roughly 500 concurrent connections. Keep load.max_in_flight (summed over shards) near or under
300 per replica, or put several replicas behind a balancer.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import selectors
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE_DIR = HERE.parent / ".dev_real"
STATE = STATE_DIR / "state.json"
DB_NAME = "fairdrop"


def _load_state() -> dict:
    return json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}


def _up(url: str) -> bool:
    try:
        return urllib.request.urlopen(f"{url}/healthz", timeout=1).status == 200
    except Exception:
        return False


def cmd_up(a: argparse.Namespace) -> int:
    try:
        import pgserver
        import psycopg
    except ImportError:
        print("missing pgserver/psycopg: pip install pgserver 'psycopg[binary]' in this venv", file=sys.stderr)
        return 2
    backend = Path(a.backend_dir).resolve()
    if not (backend / "app" / "main.py").exists():
        print(f"{backend} does not look like Member A's backend (no app/main.py)", file=sys.stderr)
        return 2
    if _load_state().get("pids") and any(_up(u) for u in _load_state().get("urls", [])):
        print("already running; use `status` or `down` first", file=sys.stderr)
        return 2
    STATE_DIR.mkdir(exist_ok=True)

    srv = pgserver.get_server(str(STATE_DIR / "pg"), cleanup_mode=None)  # leave it running after we exit
    uri = srv.get_uri()
    with psycopg.connect(uri, autocommit=True) as c:
        if not c.execute("SELECT 1 FROM pg_database WHERE datname = %s", (DB_NAME,)).fetchone():
            c.execute(f'CREATE DATABASE "{DB_NAME}"')
    dsn = uri.rsplit("/", 1)[0] + f"/{DB_NAME}"
    env = dict(os.environ, DATABASE_URL=dsn.replace("postgresql://", "postgresql+psycopg://", 1), APP_ENV="dev",
               AUTH_MODE="dev", ADMIN_TOKEN=os.environ.get("ADMIN_TOKEN", "dev-admin-token"), BEACON_PROVIDER="mock",
               SIMULATION_MODE=os.environ.get("SIMULATION_MODE", "false"))
    mig = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=backend, env=env,
                         capture_output=True, text=True)
    if mig.returncode != 0:
        print("alembic upgrade failed:\n" + (mig.stderr or mig.stdout)[-1500:], file=sys.stderr)
        return 3

    pids, urls = [], []
    for i in range(a.replicas):
        port = a.api_port + i
        p = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "_serve", str(port)], cwd=backend, env=env,
                             stdout=open(STATE_DIR / f"api-{port}.log", "w"), stderr=subprocess.STDOUT,
                             creationflags=(0x00000008 | 0x00000200) if os.name == "nt" else 0)
        pids.append(p.pid)
        urls.append(f"http://127.0.0.1:{port}")
    deadline = time.time() + 60
    while time.time() < deadline and not all(_up(u) for u in urls):
        time.sleep(0.3)
    if not all(_up(u) for u in urls):
        print(f"API did not come up; see {STATE_DIR}/api-*.log", file=sys.stderr)
        return 4
    STATE.write_text(json.dumps({"pids": pids, "urls": urls, "dsn": dsn, "backend": str(backend)}, indent=2),
                     encoding="utf-8")
    print(f"A's backend is up on {', '.join(urls)} (Postgres at {dsn.split('@')[-1]})\n")
    print("PowerShell:")
    print(f'  $env:FD_REAL_URL = "{urls[0]}"')
    print(f'  $env:FD_REAL_DSN = "{dsn}"')
    print("  fdsim doctor --base-url $env:FD_REAL_URL")
    return 0


def cmd_status(_: argparse.Namespace) -> int:
    st = _load_state()
    if not st:
        print("not started (no state file)")
        return 1
    for u in st["urls"]:
        print(f"{u}  {'UP' if _up(u) else 'DOWN'}")
    print(f"dsn: {st['dsn']}")
    return 0 if all(_up(u) for u in st["urls"]) else 1


def cmd_down(_: argparse.Namespace) -> int:
    st = _load_state()
    for pid in st.get("pids", []):
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)] if os.name == "nt" else ["kill", "-9", str(pid)],
                       capture_output=True)
    try:
        import pgserver

        pgserver.get_server(str(STATE_DIR / "pg"), cleanup_mode="stop").cleanup()
    except Exception as e:  # a Postgres that is already gone is fine
        print(f"(postgres stop: {e})")
    if STATE.exists():
        STATE.unlink()
    print("stopped")
    return 0


def cmd_serve(a: argparse.Namespace) -> int:
    """Internal: A's app on a selector loop (what A's app.serve does, without Python 3.12's loop_factory)."""
    import uvicorn

    server = uvicorn.Server(uvicorn.Config("app.main:app", host="127.0.0.1", port=a.port, log_level="warning",
                                           proxy_headers=True))
    loop = asyncio.SelectorEventLoop(selectors.SelectSelector())
    asyncio.set_event_loop(loop)
    loop.run_until_complete(server.serve())
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    up = sub.add_parser("up")
    up.add_argument("--backend-dir", required=True)
    up.add_argument("--api-port", type=int, default=8000)
    up.add_argument("--replicas", type=int, default=1)
    up.set_defaults(fn=cmd_up)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("down").set_defaults(fn=cmd_down)
    sv = sub.add_parser("_serve")
    sv.add_argument("port", type=int)
    sv.set_defaults(fn=cmd_serve)
    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
