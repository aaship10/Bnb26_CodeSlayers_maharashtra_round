"""Real-target database adapter: A's Postgres schema (migrations 0001/0002 at origin/main),
and NOTHING else in the simulator knows these table or column names.

Three jobs:
  * provisioning: bulk-create the simulated users A's dev auth requires (unknown id -> 401).
    `sim_label` is written (ground truth for C's analytics) and never read back by this code:
    labels are tracked locally from the ids C generated.
  * the server's view of an event, normalised to the columns the metrics expect.
  * an INDEPENDENT integrity check (A's invariants I1-I7, as plain SQL), so a run does not rely only
    on the engine's own checker, and still has one before A ships /invariants.

Every function takes a DSN. Use a read-only role for views/invariants and a separate provisioning
role for inserts (docs/INTERFACE_REQUESTS_C.md A-C7); the same DSN works for local development.
`validate_schema` fails loudly at startup if the columns we read are not there.
"""
from __future__ import annotations

import json
from typing import Any, Iterable, Sequence

import pandas as pd

try:  # optional extra: pip install -e ".[db]"
    import psycopg
except ImportError:  # pragma: no cover - exercised only without the extra
    psycopg = None  # type: ignore[assignment]

# table -> columns this adapter reads or writes
REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "users": ("id", "email", "display_name", "sim_label"),
    "events": ("id", "inventory", "mode", "phase", "drawn_at", "claim_ttl_seconds"),
    "entries": ("id", "event_id", "user_id", "arrival_seq", "entered_at", "weight", "risk", "state",
                "draw_rank", "waitlist_position"),
    "seats": ("id", "event_id", "seat_no"),
    "allocations": ("id", "event_id", "entry_id", "seat_id", "status", "hold_expires_at"),
}
INSERT_BATCH = 5_000
HOLD_GRACE_S = 5  # a hold this long past its expiry, with a sweeper running, is an orphan


class DbUnavailable(RuntimeError):
    """Message is showable as-is."""


def _connect(dsn: str, autocommit: bool = False):
    if psycopg is None:
        raise DbUnavailable("psycopg is not installed: pip install -e \".[db]\" in simulator/")
    try:
        return psycopg.connect(dsn, autocommit=autocommit, connect_timeout=10)
    except psycopg.Error as e:
        raise DbUnavailable(f"cannot connect to the target database: {str(e).splitlines()[0]}") from None


def validate_schema(dsn: str) -> list[str]:
    """Missing 'table.column' strings; empty means our assumptions hold."""
    missing: list[str] = []
    with _connect(dsn) as db:
        cols = {(t, c) for t, c in db.execute(
            "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'public'")}
    for table, names in REQUIRED_COLUMNS.items():
        for name in names:
            if (table, name) not in cols:
                missing.append(f"{table}.{name}")
    return missing


def insert_users(dsn: str, users: Iterable[tuple[str, str]]) -> int:
    """users = (uuid, label) with label in {'human','bot'}. Idempotent (ON CONFLICT DO NOTHING),
    so repeated runs and repeated experiment cells re-use the same fixed population. Returns rows
    that were new."""
    rows = [(uid, f"{uid}@sim.local", "sim", label) for uid, label in users]
    new = 0
    with _connect(dsn) as db:
        with db.cursor() as cur:
            for i in range(0, len(rows), INSERT_BATCH):
                batch = rows[i:i + INSERT_BATCH]
                cur.executemany(
                    "INSERT INTO users (id, email, display_name, sim_label) VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT DO NOTHING", batch)
                new += max(cur.rowcount, 0) if cur.rowcount != -1 else 0
        db.commit()
    return new


def count_sim_users(dsn: str) -> int:
    with _connect(dsn) as db:
        return db.execute("SELECT count(*) FROM users WHERE email LIKE '%@sim.local'").fetchone()[0]


def entries_frame(dsn: str, event_id: str) -> pd.DataFrame:
    """Normalised entries (db_adapter.ENTRY_COLUMNS).

    draw_state is what the entry was classified as AT the draw, from its rank, so later
    promotions do not change it: WON = rank <= inventory, WAITLISTED = rank > inventory,
    LOST = drawn but unranked (weight 0). None until the event has been drawn.
    client_ip is not stored by A (None)."""
    sql = """
        SELECT e.user_id::text                                  AS user_id,
               e.arrival_seq                                    AS arrival_seq,
               e.entered_at                                     AS entered_at,
               e.state                                          AS state,
               CASE WHEN ev.drawn_at IS NULL THEN NULL
                    WHEN e.draw_rank IS NULL THEN 'LOST'
                    WHEN e.draw_rank <= ev.inventory THEN 'WON'
                    ELSE 'WAITLISTED' END                       AS draw_state,
               e.weight::float8                                 AS weight,
               e.risk                                           AS risk,
               NULL::text                                       AS client_ip,
               s.seat_no                                        AS seat_no
          FROM entries e
          JOIN events ev ON ev.id = e.event_id
          LEFT JOIN allocations a ON a.entry_id = e.id AND a.status = 'CONFIRMED'
          LEFT JOIN seats s ON s.id = a.seat_id
         WHERE e.event_id = %s
         ORDER BY e.arrival_seq"""
    with _connect(dsn) as db:
        cur = db.execute(sql, (event_id,))
        cols = [d.name for d in cur.description]
        rows = cur.fetchall()
    df = pd.DataFrame(rows, columns=cols)
    df["risk"] = df["risk"].map(lambda v: json.dumps(v, separators=(",", ":")) if v is not None else None)
    df["seat_no"] = df["seat_no"].astype("Int64")
    return df


def state_counts(dsn: str, event_id: str) -> dict[str, Any]:
    with _connect(dsn) as db:
        phase = db.execute("SELECT phase FROM events WHERE id = %s", (event_id,)).fetchone()
        rows = db.execute("SELECT state, count(*) FROM entries WHERE event_id = %s GROUP BY state",
                          (event_id,)).fetchall()
    states = {s: int(n) for s, n in rows}
    return {"phase": phase[0] if phase else None, "entries": sum(states.values()), "states": states}


def invariants(dsn: str, event_id: str, hold_grace_s: int = HOLD_GRACE_S) -> dict[str, Any]:
    """A's invariants I1-I7 recomputed from the tables (read-only). Counts of violations; 0 is good.
    Not covered here: I8 (audit chain), I9 (recomputing the draw), I10 (window): those need A's code."""
    q = {
        # I1: active allocations may not exceed inventory
        "oversold": """SELECT GREATEST(0, (SELECT count(*) FROM allocations WHERE event_id = %(e)s
                                           AND status IN ('HELD','CONFIRMED')) - ev.inventory)
                         FROM events ev WHERE ev.id = %(e)s""",
        # I2: at most one active allocation per seat
        "duplicate_seats": """SELECT COALESCE(SUM(c - 1), 0) FROM (
                                SELECT count(*) c FROM allocations
                                 WHERE event_id = %(e)s AND status IN ('HELD','CONFIRMED')
                                 GROUP BY seat_id HAVING count(*) > 1) t""",
        # I3: one entry per user, and at most one active allocation per entry
        "duplicate_users": """SELECT (SELECT COALESCE(SUM(c - 1), 0) FROM (
                                        SELECT count(*) c FROM entries WHERE event_id = %(e)s
                                         GROUP BY user_id HAVING count(*) > 1) u)
                                   + (SELECT COALESCE(SUM(c - 1), 0) FROM (
                                        SELECT count(*) c FROM allocations
                                         WHERE event_id = %(e)s AND status IN ('HELD','CONFIRMED')
                                         GROUP BY entry_id HAVING count(*) > 1) a)""",
        # I4: CLAIMED <=> exactly one CONFIRMED allocation
        "state_mismatch": """SELECT count(*) FROM entries e
                              WHERE e.event_id = %(e)s
                                AND ((e.state = 'CLAIMED') <> (SELECT count(*) = 1 FROM allocations a
                                      WHERE a.entry_id = e.id AND a.status = 'CONFIRMED'))""",
        # I5: holds past expiry (plus grace) that nobody swept
        "orphaned_holds": """SELECT count(*) FROM allocations
                              WHERE event_id = %(e)s AND status = 'HELD'
                                AND hold_expires_at < fd_now() - make_interval(secs => %(g)s)""",
        # I6: every WON entry holds exactly one HELD allocation
        "won_without_hold": """SELECT count(*) FROM entries e
                                WHERE e.event_id = %(e)s AND e.state = 'WON'
                                  AND (SELECT count(*) FROM allocations a
                                        WHERE a.entry_id = e.id AND a.status = 'HELD') <> 1""",
        # I7: waitlist positions unique and contiguous from 1
        "waitlist_gaps": """SELECT CASE WHEN count(*) = 0 THEN 0
                                        WHEN count(DISTINCT waitlist_position) <> count(*) THEN 1
                                        WHEN min(waitlist_position) <> 1 OR max(waitlist_position) <> count(*) THEN 1
                                        ELSE 0 END
                              FROM entries WHERE event_id = %(e)s AND waitlist_position IS NOT NULL""",
    }
    out: dict[str, int] = {}
    with _connect(dsn) as db:
        for name, sql in q.items():
            row = db.execute(sql, {"e": event_id, "g": hold_grace_s}).fetchone()
            out[name] = int(row[0] or 0) if row else 0
    return {"passed": all(v == 0 for v in out.values()), "checks": out, "source": "c_sql(I1-I7)"}


def event_phase(dsn: str, event_id: str) -> str | None:
    with _connect(dsn) as db:
        r = db.execute("SELECT phase FROM events WHERE id = %s", (event_id,)).fetchone()
    return r[0] if r else None


def seats_confirmed(dsn: str, event_id: str) -> Sequence[int]:
    with _connect(dsn) as db:
        return [r[0] for r in db.execute(
            "SELECT s.seat_no FROM allocations a JOIN seats s ON s.id = a.seat_id "
            "WHERE a.event_id = %s AND a.status = 'CONFIRMED' ORDER BY s.seat_no", (event_id,))]
