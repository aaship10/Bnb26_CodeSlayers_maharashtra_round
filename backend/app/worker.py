"""Background worker: everything that must happen because time passed.

    python -m app.worker [--interval 1.0] [--once]

Per event, each tick:
  SCHEDULED, opening time reached      -> OPEN
  SCHEDULED/OPEN, closing time reached -> LOTTERY: close + draw (waits for the beacon,
                                          retrying every tick); FCFS: close
  DRAWING                              -> finish the draw (e.g. beacon was late)
  CLAIMING                             -> expire holds, promote the waitlist, end the
                                          claim phase (services/lifecycle.py)

The worker holds no state. Run as many copies as you like, or none (the same
functions can be driven by admin endpoints and tests): every transition locks the
event row and re-checks the phase, so duplicates are harmless no-ops. A failure on
one event is logged and never stops the others.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import selectors
import sys
from collections import Counter

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.config import get_settings
from app.db import dispose_engine, get_engine, transaction
from app.errors import ApiError, ErrorCode
from app.services import draw, lifecycle, phases

log = logging.getLogger("fairdrop.worker")

_DUE = text("""
    SELECT id, mode, phase,
           (phase IN ('SCHEDULED', 'OPEN') AND window_closes_at <= fd_now()) AS closing
      FROM events
     WHERE (phase = 'SCHEDULED' AND window_opens_at <= fd_now())
        OR (phase IN ('SCHEDULED', 'OPEN') AND window_closes_at <= fd_now())
        OR phase IN ('DRAWING', 'CLAIMING')
     ORDER BY window_closes_at, id
""")


async def tick(conn: AsyncConnection) -> Counter:
    """One pass over every event with time-driven work. Returns counts of what happened."""
    done: Counter = Counter()
    due = (await conn.execute(_DUE)).all()
    for ev in due:
        try:
            if ev.phase == "CLAIMING":
                r = await lifecycle.sweep_event(conn, ev.id)
                done["holds_expired"] += r.expired
                done["promoted"] += r.promoted
                done["claim_phase_closed"] += r.closed
            elif ev.mode == "LOTTERY" and (ev.closing or ev.phase == "DRAWING"):
                _, _, changed = await draw.run_draw(conn, ev.id)
                done["drawn"] += changed
            elif ev.closing:   # FCFS
                async with transaction(conn):
                    _, _, changed = await phases.close_event_window(conn, ev.id, manual=False)
                done["closed"] += changed
            elif ev.phase == "SCHEDULED":
                async with transaction(conn):
                    done["opened"] += await phases.auto_open(conn, ev.id)
        except ApiError as exc:
            if exc.code == ErrorCode.BEACON_PENDING:
                done["waiting_for_beacon"] += 1
            else:
                log.warning("worker: event %s: %s", ev.id, exc.message)
        except Exception:
            log.exception("worker: event %s failed", ev.id)
        finally:
            if conn.in_transaction():
                await conn.rollback()
    if conn.in_transaction():
        await conn.commit()
    return +done    # unary plus drops zero counts


async def run_forever(interval: float | None = None) -> None:
    interval = interval if interval is not None else get_settings().worker_interval_seconds
    while True:
        try:
            async with get_engine().connect() as conn:
                done = await tick(conn)
            if done and set(done) != {"waiting_for_beacon"}:
                log.info("worker tick: %s", dict(done))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("worker tick failed")
        await asyncio.sleep(interval)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--interval", type=float, default=None)
    p.add_argument("--once", action="store_true", help="run a single tick and exit")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    async def go() -> None:
        try:
            if args.once:
                async with get_engine().connect() as conn:
                    print(dict(await tick(conn)))
            else:
                await run_forever(args.interval)
        finally:
            await dispose_engine()

    if sys.platform == "win32":   # psycopg async needs a selector loop (see app/serve.py)
        asyncio.run(go(), loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()))
    else:
        asyncio.run(go())


if __name__ == "__main__":
    main()
