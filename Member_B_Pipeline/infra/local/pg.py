"""Private Postgres cluster for local development (no Docker, no admin rights).

Uses an installed PostgreSQL's binaries (initdb/pg_ctl) but its own data directory under
.local/pg and its own port (default 5433), so an existing Postgres on 5432 is never touched.

    python infra/local/pg.py init|start|stop|status
"""
from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import LOCAL, parse_env_file, port_open  # noqa: E402

DATA = LOCAL / "pg" / "data"
LOG = LOCAL / "pg" / "postgres.log"
PWFILE = LOCAL / "pg" / ".pw"
EXE = ".exe" if os.name == "nt" else ""


def find_bin() -> Path:
    if os.environ.get("PG_BIN"):
        return Path(os.environ["PG_BIN"])
    on_path = shutil.which("pg_ctl")
    if on_path:
        return Path(on_path).parent
    pats = [r"C:\Program Files\PostgreSQL\*\bin", "/usr/lib/postgresql/*/bin", "/opt/homebrew/opt/postgresql@*/bin"]
    hits = sorted((Path(p) for pat in pats for p in glob.glob(pat)), reverse=True)
    if hits:
        return hits[0]
    raise SystemExit("PostgreSQL binaries not found. Install PostgreSQL or set PG_BIN to its bin directory.")


def _run(args: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, **kw)


def init() -> None:
    if (DATA / "PG_VERSION").exists():
        return
    env = parse_env_file()
    DATA.parent.mkdir(parents=True, exist_ok=True)
    PWFILE.write_text(env["POSTGRES_PASSWORD"], encoding="utf-8")
    r = _run(
        [
            str(find_bin() / f"initdb{EXE}"), "-D", str(DATA), "-U", env["POSTGRES_USER"],
            "--auth=scram-sha-256", f"--pwfile={PWFILE}", "-E", "UTF8", "--locale=C",
        ]
    )
    PWFILE.unlink(missing_ok=True)  # don't leave the password lying around next to the data
    if r.returncode != 0:
        raise SystemExit(f"initdb failed:\n{r.stdout}\n{r.stderr}")
    print("initialised private Postgres cluster in", DATA)


def start() -> None:
    env = parse_env_file()
    port = int(env["POSTGRES_PORT"])
    init()
    if port_open(port):
        print(f"postgres already listening on {port}")
    else:
        opts = f"-p {port} -c listen_addresses=127.0.0.1 -c max_connections=100 -c shared_buffers=128MB"
        # DEVNULL, not capture_output: the long-lived postgres process inherits pg_ctl's
        # pipes, so a captured run() would never see EOF and would hang. Output goes to LOG.
        r = subprocess.run(
            [str(find_bin() / f"pg_ctl{EXE}"), "-D", str(DATA), "-o", opts, "-l", str(LOG), "-w", "-t", "60", "start"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if r.returncode != 0:
            raise SystemExit(f"pg_ctl start failed; see {LOG}")
        print(f"postgres started on 127.0.0.1:{port}")
    ensure_databases(env)


def ensure_databases(env: dict[str, str]) -> None:
    import psycopg

    dsn = f"postgresql://{env['POSTGRES_USER']}:{env['POSTGRES_PASSWORD']}@127.0.0.1:{env['POSTGRES_PORT']}/postgres"
    with psycopg.connect(dsn, autocommit=True) as conn:
        for db in (env["POSTGRES_DB"], env["POSTGRES_DB"] + "_test"):
            if not conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db,)).fetchone():
                conn.execute(f'CREATE DATABASE "{db}"')
                print("created database", db)


def stop() -> None:
    if not (DATA / "PG_VERSION").exists():
        return
    r = _run([str(find_bin() / f"pg_ctl{EXE}"), "-D", str(DATA), "-m", "fast", "-w", "stop"])
    print((r.stdout or r.stderr).strip().splitlines()[-1] if (r.stdout or r.stderr).strip() else "stopped")


def status() -> bool:
    port = int(parse_env_file()["POSTGRES_PORT"])
    up = port_open(port)
    print(f"postgres (private, :{port}):", "up" if up else "down")
    return up


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    {"init": init, "start": start, "stop": stop, "status": status}[cmd]()
