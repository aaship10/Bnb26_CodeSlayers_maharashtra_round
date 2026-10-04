"""Which Fair Drop server a run talks to.

  mock : the in-memory dev double. Uses FD_MOCK_URL if set (e.g. you ran `fdsim mock`, or a
         test started one); otherwise the service spawns `python -m mock_server` on a free
         port, waits for /health, and stops it on shutdown. Never evidence.
  real : A's backend (or nginx) at FD_REAL_URL, default http://127.0.0.1:8000. Probed for
         reachability; if it isn't up the request gets 409 TARGET_UNAVAILABLE (C4) with a
         sentence a UI can show as-is. Credentials come from ADMIN_TOKEN / SIM_KEY.

No Docker anywhere: A and B run plain local processes.
"""
from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys

import httpx

from fairdrop_sim.engine.run import TargetConfig


class TargetUnavailable(RuntimeError):
    """Message is shown to the user verbatim."""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _healthy(base_url: str, paths=("/health", "/healthz"), timeout: float = 1.5) -> bool:
    async with httpx.AsyncClient(base_url=base_url, timeout=timeout) as c:
        for path in paths:
            try:
                if (await c.get(path)).status_code == 200:
                    return True
            except httpx.HTTPError:
                continue
    return False


class Targets:
    def __init__(self) -> None:
        self._mock_proc: subprocess.Popen | None = None
        self._mock_url: str | None = os.environ.get("FD_MOCK_URL")
        self._lock = asyncio.Lock()

    def _creds(self, mock: bool) -> tuple[str, str | None]:
        if mock:  # the mock's own dev defaults unless overridden
            return os.environ.get("MOCK_ADMIN_TOKEN", "dev-admin-token"), os.environ.get("MOCK_SIM_KEY", "dev-sim-key")
        return os.environ.get("ADMIN_TOKEN", ""), os.environ.get("SIM_KEY")

    async def resolve(self, target: str) -> TargetConfig:
        if target == "mock":
            url = await self._ensure_mock()
            token, key = self._creds(True)
            return TargetConfig(base_url=url, admin_token=token, sim_key=key)
        url = os.environ.get("FD_REAL_URL", "http://127.0.0.1:8000")
        if not await _healthy(url):
            raise TargetUnavailable(
                f"The real Fair Drop stack is not reachable at {url}. Start Member A's backend there "
                "(or set FD_REAL_URL), or choose target \"mock\" for a development run.")
        token, key = self._creds(False)
        return TargetConfig(base_url=url, admin_token=token, sim_key=key)

    async def _ensure_mock(self) -> str:
        async with self._lock:
            if self._mock_url and await _healthy(self._mock_url, ("/health",)):
                return self._mock_url
            if self._mock_url and os.environ.get("FD_MOCK_URL"):
                raise TargetUnavailable(f"FD_MOCK_URL={self._mock_url} is set but the mock is not answering there.")
            port = _free_port()
            self._mock_proc = subprocess.Popen(
                [sys.executable, "-m", "mock_server", "--port", str(port)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._mock_url = f"http://127.0.0.1:{port}"
            for _ in range(60):
                if await _healthy(self._mock_url, ("/health",), timeout=0.5):
                    return self._mock_url
                if self._mock_proc.poll() is not None:
                    break
                await asyncio.sleep(0.25)
            self.stop()
            raise TargetUnavailable("Could not start the development mock server (python -m mock_server).")

    def stop(self) -> None:
        if self._mock_proc and self._mock_proc.poll() is None:
            self._mock_proc.terminate()
            try:
                self._mock_proc.wait(5)
            except subprocess.TimeoutExpired:
                self._mock_proc.kill()
        self._mock_proc = None
        if not os.environ.get("FD_MOCK_URL"):
            self._mock_url = None
