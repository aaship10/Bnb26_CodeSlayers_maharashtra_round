"""Download the official nginx Windows build into .local/nginx (no installer, nothing system-wide).

    python infra/local/get_nginx.py

Source: https://nginx.org/download/ (the project's own site; the 1.28 stable line, the same line Member D
tested their config on). nginx.org publishes PGP signatures rather than checksums, so the SHA-256 of what was
downloaded is printed for you to record or compare. On Linux/macOS install nginx with your package manager and
use infra/nginx/ directly.
"""
from __future__ import annotations

import hashlib
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import LOCAL  # noqa: E402

VERSION = "1.28.3"
URL = f"https://nginx.org/download/nginx-{VERSION}.zip"
DEST = LOCAL / "nginx"


def main() -> None:
    if (DEST / "nginx.exe").exists():
        print("already present:", DEST / "nginx.exe")
        return
    DEST.mkdir(parents=True, exist_ok=True)
    zpath = DEST / "nginx.zip"
    print("downloading", URL)
    with urllib.request.urlopen(URL, timeout=60) as r, open(zpath, "wb") as f:
        f.write(r.read())
    print("sha256:", hashlib.sha256(zpath.read_bytes()).hexdigest())
    with zipfile.ZipFile(zpath) as z:
        for m in z.namelist():  # refuse path traversal in the archive
            if m.startswith(("/", "\\")) or ".." in Path(m).parts:
                raise SystemExit(f"unsafe path in archive: {m}")
        z.extractall(DEST)
    inner = DEST / f"nginx-{VERSION}"
    for item in inner.iterdir():  # flatten nginx-1.28.3/* into .local/nginx/
        shutil.move(str(item), str(DEST / item.name))
    inner.rmdir()
    zpath.unlink()
    print("extracted to", DEST)


if __name__ == "__main__":
    main()
