"""Read-only analytics source: the server's view of entries, allocations and defence
decisions, as a pandas DataFrame with ONE normalised column set regardless of target.

  mock : GET /__mock/events/{id}/export          (available now)
  real : SELECT on Postgres (users/entries/allocations/defence.decisions) - Stage 7,
         pending A's schema confirmation (docs/INTERFACE_REQUESTS_C.md A-C6, A-C7).

Normalised entry columns (validated by `validate_entries`):
  user_id, arrival_seq, entered_at, state, draw_state, weight, risk, client_ip, seat_no
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from fairdrop_sim.adapters.api_adapter import AdminApi

ENTRY_COLUMNS = ["user_id", "arrival_seq", "entered_at", "state", "draw_state", "weight", "risk", "client_ip", "seat_no"]
STATES = {"ENTERED", "WON", "WAITLISTED", "LOST", "CLAIMED", "EXPIRED"}


class SchemaMismatch(RuntimeError):
    pass


def validate_entries(df: pd.DataFrame) -> pd.DataFrame:
    """Fail loudly (not silently wrong metrics) if the source doesn't match our assumptions."""
    missing = [c for c in ENTRY_COLUMNS if c not in df.columns]
    if missing:
        raise SchemaMismatch(f"entries missing columns {missing}")
    bad = set(df["state"].dropna().unique()) - STATES
    if bad:
        raise SchemaMismatch(f"unknown entry states {sorted(bad)}")
    if df["user_id"].duplicated().any():
        raise SchemaMismatch("duplicate user_id in entries (one entry per identity expected)")
    if not set(df["weight"].dropna().unique()) <= {0.0, 0.25, 0.5, 1.0}:
        raise SchemaMismatch(f"weights outside {{0, 0.25, 0.5, 1.0}}: {sorted(df['weight'].unique())[:10]}")
    return df


def entries_from_mock_export(export: dict[str, Any]) -> pd.DataFrame:
    rows = export["entries"]
    df = pd.DataFrame(rows, columns=list(rows[0].keys()) if rows else ENTRY_COLUMNS)
    for c in ENTRY_COLUMNS:
        if c not in df.columns:
            df[c] = None
    return validate_entries(df[ENTRY_COLUMNS + [c for c in df.columns if c not in ENTRY_COLUMNS]])


async def load_server_view(target: str, admin: AdminApi, event_id: str) -> dict[str, Any]:
    """{entries: DataFrame, decisions: list, verify: dict | None}"""
    if target == "mock":
        export = await admin.mock_export(event_id)
        return {
            "entries": entries_from_mock_export(export),
            "decisions": export.get("decisions", []),
            "verify": await admin.mock_verify(event_id),
        }
    raise NotImplementedError(
        "Real-target analytics arrive in Stage 7 (read-only Postgres + `python -m app.verify_draw`); "
        "waiting on A-C6/A-C7 in docs/INTERFACE_REQUESTS_C.md"
    )
