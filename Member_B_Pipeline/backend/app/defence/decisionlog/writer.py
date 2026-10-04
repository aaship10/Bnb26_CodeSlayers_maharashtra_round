"""Decision log: every gate decision (ALLOW, REJECT and CHALLENGE) goes to defence.decisions.

Design constraints, all deliberate:
* Never block the hot path: record() is a non-blocking put on a bounded queue.
* Analytics, not state: if the process crashes, the not-yet-flushed batch is LOST. That is acceptable
  because nothing depends on the log for correctness; it feeds Member C's precision/recall and the
  dashboards. Allocation never reads it.
* Overload protection: the queue is bounded; when full, NEW records are dropped and counted
  (`dropped`), rather than growing memory or slowing requests.
* A failing batch (database down) is dropped and counted (`failed_batches`) after one retry; the gate
  keeps working.
"""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from typing import Any

log = logging.getLogger("fd.decisions")

COLUMNS = "event_id, user_id, ts, action, weight, score, signals, layer, ip, device, reason"


class DecisionLog:
    def __init__(self, pool, *, max_queue: int = 20_000, batch: int = 500, interval_s: float = 0.5) -> None:
        self._pool = pool
        self._q: asyncio.Queue[tuple] = asyncio.Queue(maxsize=max_queue)
        self._batch, self._interval = batch, interval_s
        self._task: asyncio.Task | None = None
        self.written = self.dropped = self.failed_batches = 0

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="fd-decision-log")

    def record(self, row: dict[str, Any]) -> bool:
        """Non-blocking. Returns False if the record was dropped because the queue is full."""
        try:
            self._q.put_nowait((
                row["event_id"], row["user_id"], row["ts"], row["action"], row.get("weight"), row.get("score"),
                json.dumps(row["signals"]) if row.get("signals") is not None else None,
                row.get("layer"), row.get("ip"), row.get("device"), row.get("reason", ""),
            ))
            return True
        except asyncio.QueueFull:
            self.dropped += 1
            return False

    def _drain(self) -> list[tuple]:
        out: list[tuple] = []
        while len(out) < self._batch:
            try:
                out.append(self._q.get_nowait())
            except asyncio.QueueEmpty:
                break
        return out

    async def _write(self, rows: list[tuple]) -> None:
        for attempt in (1, 2):
            try:
                async with self._pool.connection() as conn:
                    async with conn.cursor() as cur:
                        await cur.executemany(
                            f"INSERT INTO defence.decisions ({COLUMNS}) VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s::inet,%s,%s)", rows
                        )
                self.written += len(rows)
                return
            except Exception as exc:  # noqa: BLE001 - analytics must never take the service down
                from .. import metrics

                metrics.pg_error("decision_log")
                if attempt == 2:
                    self.failed_batches += 1
                    log.error("decision log: dropped a batch of %d after retry (%r)", len(rows), exc)
                else:
                    await asyncio.sleep(0.2)

    async def flush(self) -> None:
        while not self._q.empty():
            rows = self._drain()
            if rows:
                await self._write(rows)

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            rows = self._drain()
            if rows:
                await self._write(rows)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self.flush()  # best effort on a clean shutdown
