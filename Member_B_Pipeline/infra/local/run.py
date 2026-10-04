"""Local (no-Docker) stack runner.

    python infra/local/run.py up [--replicas 3] [--backend stub|real]
    python infra/local/run.py status | logs [name] | scale N | down [--keep-db]
    python infra/local/run.py exec -- <command...>     # run a command with the stack's environment

Processes (all detached, logs in .local/logs, pids in .local/run/pids.json):
    postgres  private cluster on POSTGRES_PORT (see pg.py)
    redis     your REDIS_URL if reachable, else fakeredis on REDIS_FAKE_PORT
    api-<port> N replicas of the backend on API_BASE_PORT..
    gateway   http://127.0.0.1:HTTP_PORT   (/api -> replicas, /sim, frontend)
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pg  # noqa: E402
from common import LOCAL, ROOT, parse_env_file, port_open, runtime_env, venv_python  # noqa: E402

RUN = LOCAL / "run"
LOGS = LOCAL / "logs"
PIDS = RUN / "pids.json"
REPLICAS = RUN / "replicas"
WIN = os.name == "nt"


def load_pids() -> dict:
    return json.loads(PIDS.read_text()) if PIDS.exists() else {}


def save_pids(p: dict) -> None:
    RUN.mkdir(parents=True, exist_ok=True)
    PIDS.write_text(json.dumps(p, indent=2))


def pid_alive(pid: int) -> bool:
    if WIN:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)  # POSIX only: on Windows signal 0 would TERMINATE the process
        return True
    except OSError:
        return False


def kill_tree(pid: int) -> None:
    if WIN:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        import signal

        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


def spawn(name: str, cmd: list[str], cwd: Path, env: dict[str, str], port: int) -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    log = open(LOGS / f"{name}.log", "wb")  # fresh logs per start: old runs must not look like current faults
    flags = (subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS) if WIN else 0
    p = subprocess.Popen(
        cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
        creationflags=flags, start_new_session=not WIN,
    )
    pids = load_pids()
    pids[name] = {"pid": p.pid, "port": port}
    save_pids(pids)
    print(f"started {name} (pid {p.pid}, :{port})")


def http_ok(url: str, timeout: float = 1.5) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def wait_for(url: str, what: str, seconds: int = 60) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if http_ok(url):
            return
        time.sleep(0.4)
    raise SystemExit(f"{what} did not become healthy at {url}; see {LOGS}")


def backend_dir(kind: str) -> Path:
    return ROOT / ("backend" if kind == "real" else "infra/stub_api")


def start_redis(env: dict[str, str], file_env: dict[str, str]) -> str:
    url = file_env.get("REDIS_URL", "")
    if url:
        u = urlparse(url)
        if port_open(u.port or 6379, u.hostname or "127.0.0.1"):
            print(f"using redis at {url}")
            return url
        print(f"WARNING: REDIS_URL={url} unreachable; falling back to fakeredis")
    port = int(file_env.get("REDIS_FAKE_PORT", "6380"))
    real = LOCAL / "redis" / "redis-server.exe"
    if not port_open(port):
        if real.exists():
            # Real Redis (downloaded by get_redis.py): no persistence (it is a rebuildable cache),
            # evict only keys that carry a TTL (every key we write has one).
            spawn("redis", [str(real), "--port", str(port), "--bind", "127.0.0.1", "--save", "", "--appendonly", "no",
                            "--maxmemory", "200mb", "--maxmemory-policy", "volatile-lru"], real.parent, env, port)
        else:
            spawn("redis", [venv_python(), str(ROOT / "infra/local/fake_redis.py"), str(port)], ROOT, env, port)
        for _ in range(50):
            if port_open(port):
                break
            time.sleep(0.2)
        if not real.exists():
            print("NOTE: using fakeredis: fine for functional runs, too slow to flood-test (rate limiting then fails open)."
                  " For realistic tests run: python infra/local/get_redis.py")
    return f"redis://127.0.0.1:{port}/0"


def write_replicas(ports: list[int]) -> None:
    RUN.mkdir(parents=True, exist_ok=True)
    REPLICAS.write_text("\n".join(f"127.0.0.1:{p}" for p in ports) + "\n")
    refresh_edge(ports)


def start_replica(port: int, env: dict[str, str], backend: str) -> None:
    spawn(
        f"api-{port}",
        [venv_python(), str(ROOT / "infra/local/serve.py"), "app.main:app", "--port", str(port)],
        backend_dir(backend), env, port,
    )


MAIL_MODE_FILE = RUN / "mail_mode"
SIM_MARKER = RUN / "sim_mode"
EDGE_MARKER = RUN / "edge_mode"  # "gateway" (default, Python) or "nginx"
NGINX_DIR = LOCAL / "nginx"
NGINX_EXE = NGINX_DIR / "nginx.exe"
NGINX_CONF = "conf/fd/nginx.conf"  # relative to the nginx prefix (-p)


def edge_mode() -> str:
    return EDGE_MARKER.read_text().strip() if EDGE_MARKER.exists() else "gateway"


def frontend_root(file_env: dict[str, str]) -> str:
    """FRONTEND_DIST, else the sibling monorepo's built frontend/dist, else the placeholder page."""
    if file_env.get("FRONTEND_DIST"):
        return Path(file_env["FRONTEND_DIST"]).resolve().as_posix()
    sibling = ROOT.parent / "frontend" / "dist"
    return (sibling if (sibling / "index.html").is_file() else ROOT / "infra" / "nginx" / "placeholder").resolve().as_posix()


def nginx_render(ports: list[int], http: int, ops_port: int, sim_key: str | None, file_env: dict[str, str], base: Path = NGINX_DIR) -> None:
    """Fill the placeholders in infra/nginx/*.conf and write the result where nginx reads it (`base` = nginx prefix)."""
    conf = base / "conf" / "fd"
    conf.mkdir(parents=True, exist_ok=True)
    for sub in ("cache", "logs", "temp"):
        (base / sub).mkdir(exist_ok=True)
    upstream = "\n        ".join(f"server 127.0.0.1:{p} max_fails=1 fail_timeout=2s;" for p in ports)
    sim_map = f'"{sim_key}" "";' if sim_key else ""
    subst = {
        "__UPSTREAM_API__": upstream, "__SIM_KEY_MAP__": sim_map, "__OPS_PORT__": str(ops_port),
        "__LISTEN__": str(http), "__FRONTEND_ROOT__": frontend_root(file_env),
        # same knobs as the dev gateway: generous by default (campus NATs), tests tighten them
        "__EDGE_RATE__": str(int(float(os.environ.get("EDGE_RPS", "400")))), "__EDGE_BURST__": str(int(float(os.environ.get("EDGE_BURST", "1500")))),
    }
    for name in ("nginx.conf", "fairdrop.conf", "fd-proxy.conf", "fd-security-headers.conf"):
        text = (ROOT / "infra" / "nginx" / name).read_text(encoding="utf-8")
        for k, v in subst.items():
            text = text.replace(k, v)
        (conf / name).write_text(text, encoding="utf-8")
    shutil.copy(NGINX_DIR / "conf" / "mime.types", conf / "mime.types")  # the downloaded build ships it


def _nginx(*args: str, base: Path = NGINX_DIR) -> subprocess.CompletedProcess:
    return subprocess.run([str(NGINX_EXE), "-p", base.as_posix() + "/", "-c", NGINX_CONF, *args],
                          capture_output=True, text=True, cwd=base)


def nginx_test(base: Path = NGINX_DIR) -> tuple[bool, str]:
    r = _nginx("-t", base=base)
    return r.returncode == 0, (r.stdout + r.stderr).strip()


def nginx_reload() -> None:
    _nginx("-s", "reload")


def refresh_edge(ports: list[int]) -> None:
    """After the replica list changed: re-render the nginx upstreams and reload (no-op for the Python gateway,
    which re-reads .local/run/replicas by itself)."""
    if edge_mode() != "nginx" or not pid_alive(load_pids().get("nginx", {}).get("pid", -1)):
        return
    file_env = parse_env_file()
    http = int(file_env["HTTP_PORT"])
    nginx_render(ports, http, http + 10, file_env.get("SIM_KEY") if SIM_MARKER.exists() else None, file_env)
    ok, out = nginx_test()
    if not ok:
        raise SystemExit(f"rendered nginx config is invalid:\n{out}")
    nginx_reload()


def start_worker(env: dict[str, str], backend: str) -> None:
    spawn("worker", [venv_python(), "-m", "app.worker"], backend_dir(backend), env, 0)


def apply_mail_mode(env: dict[str, str], mail: str) -> None:
    """mail=file forces the file outbox even when SMTP is configured in infra/.env. The test scripts
    register made-up addresses, which must never be emailed for real (they would bounce off the
    account whose credentials are in .env)."""
    if mail == "file":
        env["SMTP_HOST"] = env["SMTP_USER"] = env["SMTP_PASSWORD"] = ""
    RUN.mkdir(parents=True, exist_ok=True)
    MAIL_MODE_FILE.write_text("file outbox" if (mail == "file" or not env.get("SMTP_HOST")) else f"real email via {env['SMTP_HOST']}")


def up(replicas: int, backend: str, mail: str = "auto", sim: bool = False, edge: str = "gateway") -> None:
    file_env = parse_env_file()
    if backend == "real" and not (ROOT / "backend" / "app" / "main.py").exists():
        raise SystemExit(
            "--backend real: backend/app/main.py is not here. Member A's backend has to be merged into backend/ first "
            "(B's package lives in backend/app/defence/); see docs/INTEGRATION_WITH_A.md for the checklist. "
            "Nothing was started. (The default stub backend needs no merge.)"
        )
    if edge == "nginx" and not NGINX_EXE.exists():
        raise SystemExit("nginx not found: run  python infra/local/get_nginx.py  first (official nginx.org build, no installer)")
    RUN.mkdir(parents=True, exist_ok=True)
    EDGE_MARKER.write_text(edge)
    pg.start()
    env = runtime_env()
    apply_mail_mode(env, mail)
    # The local fallback limiter divides its limits by the replica count (used only while Redis is down).
    env["FD_REPLICA_COUNT"] = str(replicas)
    RUN.mkdir(parents=True, exist_ok=True)
    if sim:
        # Simulation mode: the simulator/load tools may use X-Sim-Key headers and POST /admin/sim/tokens.
        # NEVER leave this on for a real event (the .env default is false).
        env["SIMULATION_MODE"] = "true"
        SIM_MARKER.write_text("on")
    else:
        SIM_MARKER.unlink(missing_ok=True)
    env["REDIS_URL"] = start_redis(env, file_env)
    base = int(file_env["API_BASE_PORT"])
    ports = [base + i for i in range(replicas)]
    for port in ports:
        if port_open(port):
            print(f"api :{port} already running")
        else:
            start_replica(port, env, backend)
    if "worker" not in load_pids() or not pid_alive(load_pids()["worker"]["pid"]):
        start_worker(env, backend)  # A's worker (`python -m app.worker`) is safe to replicate; the stub's is a stand-in
    write_replicas(ports)
    http = int(file_env["HTTP_PORT"])
    start_edge(env, file_env, ports, edge, http)
    for port in ports:
        wait_for(f"http://127.0.0.1:{port}/healthz", f"api :{port}")
    wait_for(f"http://127.0.0.1:{http}/api/healthz", "edge")
    print(f"\nstack up: http://127.0.0.1:{http}   ({replicas} api replicas on {ports[0]}..{ports[-1]}, edge: {edge})")
    print(f"logs: {LOGS}   stop: python infra/local/run.py down")


def start_edge(env: dict[str, str], file_env: dict[str, str], ports: list[int], edge: str, http: int) -> None:
    """The edge answers on HTTP_PORT. With nginx the Python gateway moves to HTTP_PORT+10 and only serves /ops/."""
    ops_port = http + 10
    gw_port = ops_port if edge == "nginx" else http
    if not port_open(gw_port):
        genv = dict(env)
        genv["FRONTEND_DIST"] = file_env.get("FRONTEND_DIST", "")
        spawn("gateway", [venv_python(), str(ROOT / "infra/local/serve.py"), "gateway:app", "--port", str(gw_port)],
              ROOT / "infra/local", genv, gw_port)
    if edge != "nginx":
        return
    sim_key = env.get("SIM_KEY") if env.get("SIMULATION_MODE", "").lower() == "true" else None
    nginx_render(ports, http, ops_port, sim_key, file_env)
    ok, out = nginx_test()
    if not ok:
        raise SystemExit(f"nginx -t failed:\n{out}")
    if not port_open(http):
        spawn("nginx", [str(NGINX_EXE), "-p", NGINX_DIR.as_posix() + "/", "-c", NGINX_CONF], NGINX_DIR, env, http)
        for _ in range(50):
            if port_open(http):
                break
            time.sleep(0.1)


def current_env(n: int) -> tuple[dict[str, str], dict[str, str]]:
    """The environment the running stack was started with (mail mode, simulation mode, replica count)."""
    file_env = parse_env_file()
    env = runtime_env()
    apply_mail_mode(env, "file" if MAIL_MODE_FILE.exists() and MAIL_MODE_FILE.read_text() == "file outbox" else "auto")
    env["FD_REPLICA_COUNT"] = str(n)
    if SIM_MARKER.exists():
        env["SIMULATION_MODE"] = "true"  # keep the mode the stack was started in
    env["REDIS_URL"] = start_redis(env, file_env)
    return env, file_env


def kill_proc(name: str) -> bool:
    """Hard-kill one managed process ('api-8002', 'worker', 'redis', 'gateway', 'nginx'): a fault, not a shutdown."""
    pids = load_pids()
    if name.isdigit():
        name = f"api-{name}"
    info = pids.get(name)
    if info is None:
        return False
    kill_tree(info["pid"])
    return True


def heal(backend: str) -> None:
    """Restart whatever died (replicas, worker, Redis, edge) without touching what is healthy."""
    ports = [int(ln.rsplit(":", 1)[1]) for ln in REPLICAS.read_text().split() if ln.strip()] if REPLICAS.exists() else []
    if not ports:
        raise SystemExit("no replica list: nothing was started (run `up` first)")
    env, file_env = current_env(len(ports))
    pids = load_pids()
    if "worker" not in pids or not pid_alive(pids["worker"]["pid"]):
        start_worker(env, backend)
    for port in ports:
        if not port_open(port):
            start_replica(port, env, backend)
    start_edge(env, file_env, ports, edge_mode(), int(file_env["HTTP_PORT"]))
    for port in ports:
        wait_for(f"http://127.0.0.1:{port}/healthz", f"api :{port}")
    print("healed")


def scale(n: int, backend: str) -> None:
    env, file_env = current_env(n)
    base = int(file_env["API_BASE_PORT"])
    want = [base + i for i in range(n)]
    pids = load_pids()
    for name, info in list(pids.items()):
        if name.startswith("api-") and info["port"] not in want:
            kill_tree(info["pid"])
            pids.pop(name)
            print("stopped", name)
    save_pids(pids)
    for port in want:
        if not port_open(port):
            start_replica(port, env, backend)
    write_replicas(want)
    for port in want:
        wait_for(f"http://127.0.0.1:{port}/healthz", f"api :{port}")
    print(f"now {n} replicas")


def down(keep_db: bool) -> None:
    if NGINX_EXE.exists() and "nginx" in load_pids():
        _nginx("-s", "stop")  # graceful; the kill below is the backstop
    pids = load_pids()
    for name, info in pids.items():
        if pid_alive(info["pid"]):
            kill_tree(info["pid"])
            print("stopped", name)
    save_pids({})
    REPLICAS.unlink(missing_ok=True)
    EDGE_MARKER.unlink(missing_ok=True)
    if not keep_db:
        pg.stop()


def status() -> None:
    file_env = parse_env_file()
    pg.status()
    for name, info in load_pids().items():
        alive = pid_alive(info["pid"])
        health = ""
        if name.startswith("api-"):
            health = "healthy" if http_ok(f"http://127.0.0.1:{info['port']}/healthz") else "unhealthy"
        print(f"{name:<12} pid {info['pid']:<7} :{info['port']:<5} {'running' if alive else 'DEAD':<8} {health}")
    http = int(file_env["HTTP_PORT"])
    if MAIL_MODE_FILE.exists():
        print("mail:", MAIL_MODE_FILE.read_text())
    if SIM_MARKER.exists():
        print("SIMULATION MODE IS ON (X-Sim-Key headers, /admin/sim/tokens): never for a real event")
    print(f"edge: {edge_mode()}")
    print("edge /api/healthz:", "ok" if http_ok(f"http://127.0.0.1:{http}/api/healthz") else "down")


def logs(name: str | None) -> None:
    files = sorted(LOGS.glob("*.log")) if not name else [LOGS / f"{name}.log"]
    for f in files:
        print(f"===== {f.name} =====")
        if f.exists():
            print("\n".join(f.read_text(errors="replace").splitlines()[-40:]))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("up")
    p.add_argument("--replicas", type=int, default=3)
    p.add_argument("--mail", choices=["auto", "file"], default="auto", help="file = never send real email (used by the checks)")
    p.add_argument("--sim", action="store_true", help="SIMULATION_MODE=true for this run (scale tests, Member C's simulator)")
    p.add_argument("--backend", choices=["stub", "real"], default="stub")
    p.add_argument("--edge", choices=["gateway", "nginx"], default="gateway", help="nginx needs: python infra/local/get_nginx.py")
    p = sub.add_parser("kill", help="hard-kill one process: a replica port (8002), worker, redis, gateway or nginx")
    p.add_argument("name")
    p = sub.add_parser("heal", help="restart whatever died, leave healthy processes alone")
    p.add_argument("--backend", choices=["stub", "real"], default="stub")
    p = sub.add_parser("scale")
    p.add_argument("n", type=int)
    p.add_argument("--backend", choices=["stub", "real"], default="stub")
    p = sub.add_parser("down")
    p.add_argument("--keep-db", action="store_true")
    sub.add_parser("status")
    p = sub.add_parser("logs")
    p.add_argument("name", nargs="?")
    p = sub.add_parser("exec")
    p.add_argument("argv", nargs=argparse.REMAINDER)
    a = ap.parse_args()

    if a.cmd == "up":
        up(a.replicas, a.backend, a.mail, a.sim, a.edge)
    elif a.cmd == "kill":
        print("killed" if kill_proc(a.name) else f"no managed process called {a.name!r}")
    elif a.cmd == "heal":
        heal(a.backend)
    elif a.cmd == "scale":
        scale(a.n, a.backend)
    elif a.cmd == "down":
        down(a.keep_db)
    elif a.cmd == "status":
        status()
    elif a.cmd == "logs":
        logs(a.name)
    elif a.cmd == "exec":
        argv = [x for x in a.argv if x != "--"]
        if not argv:
            raise SystemExit("usage: run.py exec -- <command...>")
        if argv[0] == "python":
            argv[0] = venv_python()
        env = runtime_env()
        file_env = parse_env_file()
        if not file_env.get("REDIS_URL"):
            env["REDIS_URL"] = f"redis://127.0.0.1:{file_env.get('REDIS_FAKE_PORT', '6380')}/0"
        raise SystemExit(subprocess.call(argv, env=env, cwd=ROOT))


if __name__ == "__main__":
    main()
