"""Apply dotted-path overrides to a scenario dict, e.g.
{"event.mode": "FCFS", "attackers.0.request_multiplier": 100}. Lists are indexed by
integer segments. Used to build experiment cells from one base scenario."""
from __future__ import annotations

import copy
from typing import Any


def set_by_path(obj: dict | list, path: str, value: Any) -> None:
    parts = path.split(".")
    cur: Any = obj
    for seg in parts[:-1]:
        cur = cur[int(seg)] if isinstance(cur, list) else cur.setdefault(seg, {})
    last = parts[-1]
    if isinstance(cur, list):
        cur[int(last)] = value
    else:
        cur[last] = value


def get_by_path(obj: Any, path: str) -> Any:
    cur = obj
    for seg in path.split("."):
        if cur is None:
            return None
        cur = cur[int(seg)] if isinstance(cur, list) else cur.get(seg)
    return cur


def apply_overrides(base: dict, overrides: dict[str, Any]) -> dict:
    out = copy.deepcopy(base)
    for path, value in overrides.items():
        set_by_path(out, path, value)
    return out
