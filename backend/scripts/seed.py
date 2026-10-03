"""Deterministic dummy data: N users (some labelled as bots) and a few events.

    python -m scripts.seed                      # 50,000 users, 10% sim_label='bot'
    python -m scripts.seed --users 1000 --bot-fraction 0.3 --seed 7
    python -m scripts.seed --reset              # wipe users/events first (dev only)

The same --seed always produces the same user ids, emails and labels, so
experiments are reproducible. Re-running without --reset is a no-op for rows
that already exist (ON CONFLICT DO NOTHING).

Display names and emails deliberately do NOT reveal sim_label: the label is
ground truth for analytics only and must not leak into anything the engine
could read.

Events are created in phase DRAFT; scheduling (seed commitment, beacon round,
seats) happens through the admin API from stage 2 onwards.
"""
from __future__ import annotations

import argparse
import random
import sys
import time
import uuid
from dataclasses import dataclass

import psycopg

from app.config import get_settings, sync_url

ADMIN_ID = uuid.UUID("00000000-0000-4000-8000-00000000a0a0")

# Fixed ids so scripts and teammates can refer to the demo events directly.
SMALL_EVENT_ID = uuid.UUID("00000000-0000-4000-8000-000000000005")
MAIN_EVENT_ID = uuid.UUID("00000000-0000-4000-8000-000000000500")
FCFS_EVENT_ID = uuid.UUID("00000000-0000-4000-8000-0000000f0500")


@dataclass(frozen=True)
class SeedUser:
    id: uuid.UUID
    email: str
    display_name: str
    sim_label: str


def generate_users(n: int, bot_fraction: float, seed: int) -> list[SeedUser]:
    """Pure function: same arguments -> identical list."""
    if not 0.0 <= bot_fraction <= 1.0:
        raise ValueError("bot_fraction must be within [0, 1]")
    rng = random.Random(seed)
    ids = [uuid.UUID(int=rng.getrandbits(128), version=4) for _ in range(n)]
    bots = set(rng.sample(range(n), round(n * bot_fraction)))
    return [
        SeedUser(
            id=ids[i],
            email=f"user{i:06d}@sim.fairdrop.test",
            display_name=f"User {i:06d}",
            sim_label="bot" if i in bots else "human",
        )
        for i in range(n)
    ]


def demo_events() -> list[dict]:
    return [
        dict(id=SMALL_EVENT_ID, name="Small Drop (5 seats)", inventory=5, mode="LOTTERY",
             claim_ttl_seconds=60, claim_phase_seconds=600),
        dict(id=MAIN_EVENT_ID, name="Main Drop (500 seats)", inventory=500, mode="LOTTERY",
             claim_ttl_seconds=300, claim_phase_seconds=1800),
        dict(id=FCFS_EVENT_ID, name="FCFS Baseline (500 seats)", inventory=500, mode="FCFS",
             claim_ttl_seconds=300, claim_phase_seconds=1800),
    ]


def reset(conn: psycopg.Connection) -> None:
    with conn.transaction():
        # Transaction-local bypass of the audit_log append-only trigger (dev only).
        conn.execute("SET LOCAL fairdrop.allow_audit_reset = 'on'")
        conn.execute("DELETE FROM audit_log")
        conn.execute("TRUNCATE idempotency_keys, allocations, entries, seats, "
                     "event_secrets, events, users")
        conn.execute("UPDATE dev_clock SET offset_seconds = 0 WHERE id")


def seed_users(conn: psycopg.Connection, users: list[SeedUser]) -> int:
    """Bulk load via COPY into a temp table, then insert the missing rows."""
    with conn.transaction():
        conn.execute(
            "CREATE TEMP TABLE seed_users (id uuid, email text, display_name text, "
            "sim_label text) ON COMMIT DROP"
        )
        with conn.cursor() as cur:
            with cur.copy("COPY seed_users (id, email, display_name, sim_label) FROM STDIN") as cp:
                for u in users:
                    cp.write_row((u.id, u.email, u.display_name, u.sim_label))
        inserted = conn.execute(
            "INSERT INTO users (id, email, display_name, sim_label) "
            "SELECT id, email, display_name, sim_label FROM seed_users "
            "ON CONFLICT DO NOTHING"
        ).rowcount
        conn.execute(
            "INSERT INTO users (id, email, display_name, is_admin) "
            "VALUES (%s, 'admin@fairdrop.test', 'Admin', true) ON CONFLICT DO NOTHING",
            (ADMIN_ID,),
        )
    return inserted


def seed_events(conn: psycopg.Connection) -> int:
    inserted = 0
    with conn.transaction():
        for ev in demo_events():
            # Placeholder window (+1h .. +1h10m). Admins re-time events when scheduling.
            inserted += conn.execute(
                """
                INSERT INTO events (id, name, inventory, mode, window_opens_at,
                                    window_closes_at, claim_ttl_seconds, claim_phase_seconds)
                VALUES (%(id)s, %(name)s, %(inventory)s, %(mode)s,
                        fd_now() + interval '1 hour', fd_now() + interval '70 minutes',
                        %(claim_ttl_seconds)s, %(claim_phase_seconds)s)
                ON CONFLICT (id) DO NOTHING
                """,
                ev,
            ).rowcount
    return inserted


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--users", type=int, default=50_000)
    p.add_argument("--bot-fraction", type=float, default=0.10)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--reset", action="store_true", help="wipe users/events/entries first (dev only)")
    p.add_argument("--database-url", default=None, help="defaults to DATABASE_URL")
    args = p.parse_args(argv)

    settings = get_settings()
    url = sync_url(args.database_url or settings.database_url)
    if args.reset and not settings.is_dev:
        print("refusing --reset outside APP_ENV=dev", file=sys.stderr)
        return 2

    t0 = time.perf_counter()
    users = generate_users(args.users, args.bot_fraction, args.seed)
    with psycopg.connect(url) as conn:
        if args.reset:
            reset(conn)
        n_users = seed_users(conn, users)
        n_events = seed_events(conn)
    bots = sum(u.sim_label == "bot" for u in users)
    print(f"seeded users: {n_users} new / {len(users)} requested ({bots} labelled bot), "
          f"events: {n_events} new, in {time.perf_counter() - t0:.2f}s")
    print(f"  small event: {SMALL_EVENT_ID}\n  main event:  {MAIN_EVENT_ID}\n  fcfs event:  {FCFS_EVENT_ID}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
