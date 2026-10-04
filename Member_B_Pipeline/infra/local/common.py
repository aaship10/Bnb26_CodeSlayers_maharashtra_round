"""Shared helpers for the local (no-Docker) runtime."""
from __future__ import annotations

import os
import secrets
import socket
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOCAL = ROOT / ".local"
ENV_FILE = ROOT / "infra" / ".env"
ENV_EXAMPLE = ROOT / "infra" / ".env.example"
PLACEHOLDER = "__GENERATE__"


def ensure_env() -> None:
    if ENV_FILE.exists():
        return
    lines = []
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        if line.rstrip().endswith(PLACEHOLDER) and "=" in line and not line.lstrip().startswith("#"):
            key = line.split("=", 1)[0]
            line = f"{key}={secrets.token_urlsafe(36)}"
        lines.append(line)
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"created {ENV_FILE} with generated secrets")


def parse_env_file() -> dict[str, str]:
    ensure_env()
    out: dict[str, str] = {}
    for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        # strip trailing inline comments ("  # ...") but keep '#' inside values
        if " #" in v:
            v = v.split(" #", 1)[0]
        out[k.strip()] = v.strip()
    return out


def runtime_env(redis_url: str | None = None) -> dict[str, str]:
    """Process env + infra/.env (the file wins over the shell) + derived URLs."""
    env = dict(os.environ)
    file_env = parse_env_file()
    env.update(file_env)
    env["DATABASE_URL"] = (
        f"postgresql://{file_env['POSTGRES_USER']}:{file_env['POSTGRES_PASSWORD']}"
        f"@127.0.0.1:{file_env['POSTGRES_PORT']}/{file_env['POSTGRES_DB']}"
    )
    if redis_url:
        env["REDIS_URL"] = redis_url
    elif not env.get("REDIS_URL"):
        env["REDIS_URL"] = f"redis://127.0.0.1:{file_env.get('REDIS_FAKE_PORT', '6380')}/0"
    env["PYTHONUNBUFFERED"] = "1"
    # Absolute: replicas run with different working directories.
    env.setdefault("OUTBOX_DIR", str(LOCAL / "outbox"))
    return env


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def venv_python() -> str:
    cand = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return str(cand) if cand.exists() else "python"
