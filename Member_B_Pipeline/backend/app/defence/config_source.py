"""Where the gate gets an event's defence config, with a short cache.

Config lives in A's events table (events.config -> defences). Reading it on every request
would put Postgres on the hot path of exactly the traffic we are trying to shed, so each
process caches it for CONFIG_TTL_S. Consequences, deliberately accepted:
  * a toggle made mid-demo takes effect within ~CONFIG_TTL_S on every replica;
  * one request per event per replica per TTL reaches Postgres (single-flight: concurrent
    callers wait for the one lookup instead of stampeding);
  * it is a cache of durable state, not state: losing it costs one lookup.

Failure policy: never silently drop to "no defences". If the lookup fails or the stored
config is invalid we keep serving the last good value; with none we fall back to the
`rate_limit` preset (cheap, safe) and log loudly.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid

from . import metrics
from .config import DefenceConfigError, effective_config
from .config_schema import DefencesConfig
from .presets import preset_config

log = logging.getLogger("fd.config")

CONFIG_TTL_S = 1.5
_cache: dict[str, tuple[float, DefencesConfig]] = {}
_locks: dict[str, asyncio.Lock] = {}


def clear_cache() -> None:
    _cache.clear()
    _locks.clear()


async def _load(event_id: str) -> DefencesConfig | None:
    """None = event does not exist."""
    from .runtime import get_runtime

    rt = await get_runtime()
    async with rt.pg.connection() as conn:
        row = await (await conn.execute("SELECT config FROM public.events WHERE id = %s", (event_id,))).fetchone()
    if row is None:
        return None
    return effective_config(row["config"])


async def get_defences(event_id: uuid.UUID | str) -> DefencesConfig:
    key = str(event_id)
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CONFIG_TTL_S:
        return hit[1]

    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        hit = _cache.get(key)  # someone else refreshed while we waited
        if hit and time.monotonic() - hit[0] < CONFIG_TTL_S:
            return hit[1]
        try:
            cfg = await _load(key)
            if cfg is None:
                cfg = effective_config(None)  # unknown event: the route will 404; nothing to defend
        except DefenceConfigError as exc:
            log.error("stored defences config for %s is invalid (%s); keeping last good value", key, exc.errors)
            cfg = hit[1] if hit else preset_config("rate_limit")
        except Exception as exc:  # noqa: BLE001 - DB down, pool exhausted, ...
            metrics.pg_error("config_lookup")
            log.error("could not load defences config for %s (%r); keeping last good value", key, exc)
            cfg = hit[1] if hit else preset_config("rate_limit")
        _cache[key] = (time.monotonic(), cfg)
        return cfg
