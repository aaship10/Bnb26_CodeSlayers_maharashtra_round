"""STUB worker (`python -m app.worker`). A's real worker expires claim holds and
promotes the waitlist; this one only proves the process model: starts, idles,
stops cleanly on SIGTERM, safe to replicate."""
from __future__ import annotations

import asyncio
import logging
import signal

log = logging.getLogger("fd.worker")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows event loops: fall back to the plain signal module
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
    log.info("stub worker started")
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=10)
        except asyncio.TimeoutError:
            log.info("stub worker tick")
    log.info("stub worker stopped")


if __name__ == "__main__":
    asyncio.run(main())
