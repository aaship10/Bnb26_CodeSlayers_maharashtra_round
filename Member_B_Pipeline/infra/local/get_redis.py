"""Download a Windows Redis build into .local/redis (no installer, nothing system-wide).

    python infra/local/get_redis.py

Source: the tporadowski/redis Windows port (Redis 5.0.14.1, GitHub releases). It is a community
port, not an official Redis release, and 5.x is old: fine for local development (Lua, TIME,
EVALSHA all work), not something to expose or run in production. The SHA-256 of what was
downloaded is printed so you can compare it with the release page yourself.
On Linux/macOS install Redis with your package manager instead and set REDIS_URL.
"""
from __future__ import annotations

import hashlib
import sys
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import LOCAL  # noqa: E402

URL = "https://github.com/tporadowski/redis/releases/download/v5.0.14.1/Redis-x64-5.0.14.1.zip"
DEST = LOCAL / "redis"


def main() -> None:
    if (DEST / "redis-server.exe").exists():
        print("already present:", DEST / "redis-server.exe")
        return
    DEST.mkdir(parents=True, exist_ok=True)
    zpath = DEST / "redis.zip"
    print("downloading", URL)
    with urllib.request.urlopen(URL, timeout=60) as r, open(zpath, "wb") as f:
        f.write(r.read())
    print("sha256:", hashlib.sha256(zpath.read_bytes()).hexdigest())
    with zipfile.ZipFile(zpath) as z:
        for m in z.namelist():  # refuse path traversal in the archive
            if m.startswith(("/", "\\")) or ".." in Path(m).parts:
                raise SystemExit(f"unsafe path in archive: {m}")
        z.extractall(DEST)
    zpath.unlink()
    print("extracted to", DEST)


if __name__ == "__main__":
    main()
