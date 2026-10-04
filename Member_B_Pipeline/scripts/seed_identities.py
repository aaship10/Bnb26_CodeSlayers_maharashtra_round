"""Deterministic dummy identities for testing signals and the risk engine without A or C.

    python infra/local/run.py exec -- python scripts/seed_identities.py --reset [--users 50000] [--seed 1337]

Writes public.users + defence.identities (COPY, ~seconds for 50k). Same --seed => identical
data (UUIDs, emails, times, IPs) so tests and demos are reproducible.

Population
  * legitimate students (the bulk): name- and roll-number-style emails, registration times over
    14 days weighted towards the drop with day/night rhythm, realistic OTP latency, devices
    (3% shared by 2-3 siblings/lab PCs), and IPs where ~35% sit behind a few CAMPUS NAT
    addresses: thousands of real students on one IP, the false-positive trap for IP signals.
  * planted suspicious clusters (sizes scale down proportionally for small --users):
      sequential      studentbot1..N@ registered over ~20 min, distinct devices
      one_device      200 accounts on ONE device id, varied names, varied IPs, over 2 days
      burst           300 accounts within 2 minutes ~30 min before the drop, one IP, one UA
      random_local    150 random-looking local parts from one subnet
Ground truth for MY tests goes to a manifest JSON (cluster -> user ids). It is not stored in
any table and no defence code reads it (Member C's users.sim_label is untouched and unread).
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import math
import random
import string
import sys
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import psycopg  # noqa: E402

from app.defence.identity import emailchecks  # noqa: E402
from app.defence.migrate import upgrade  # noqa: E402

DOMAIN = "example-college.edu"
ANCHOR = datetime(2026, 10, 15, 9, 0, 0, tzinfo=timezone.utc)  # the drop; fixed => deterministic
FIRST = ("aarav aditi aditya akash ananya anil anjali ankit arjun aryan deepak diya divya gaurav isha ishaan kavya karan "
         "kunal lakshmi manish meera mohit neha nikhil pooja pranav priya rahul riya rohan sagar sanjay shreya siddharth "
         "sneha suresh tanvi uday varun vikram vivek yash zoya amit bhavna chetan dhruv esha farhan gita harsh").split()
LAST = ("sharma patel singh kumar gupta mehta shah joshi nair iyer reddy rao desai kulkarni deshmukh patil pawar jadhav "
        "more chavan shinde thakur yadav mishra pandey verma agarwal bansal chopra malhotra kapoor khanna sethi bhatia "
        "menon pillai das ghosh bose sen roy dutta banerjee mukherjee chatterjee fernandes dsouza pereira dias gomes "
        "rodrigues almeida").split()
BRANCH = ("cs", "it", "ec", "me", "ce", "ee")
UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0", "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) Safari/605.1.15",
    "Mozilla/5.0 (Linux; Android 13; SM-A135F) Chrome/125.0 Mobile", "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5) Safari/604.1",
    "Mozilla/5.0 (X11; Linux x86_64; rv:127.0) Gecko/20100101 Firefox/127.0", "Mozilla/5.0 (Linux; Android 11; Redmi Note 9) Chrome/120.0 Mobile",
    "Mozilla/5.0 (Windows NT 10.0) Edg/126.0", "Mozilla/5.0 (Linux; Android 14; Pixel 7) Chrome/126.0 Mobile",
]
# (network, weight, "campus NAT" -> few addresses). Documentation + private ranges only (see data/asn_table.csv).
NETS = [("10.30.0.0/16", 15), ("172.16.0.0/12", 25), ("100.64.0.0/10", 10), ("192.168.0.0/16", 10),
        ("192.0.2.0/24", 3), ("198.51.100.0/24", 3), ("203.0.113.0/24", 3)]
CAMPUS_NAT = [str(ipaddress.ip_address("10.20.0.0") + i) for i in range(5, 17)]  # 12 shared addresses
CAMPUS_SHARE = 0.35
BASE = {"sequential": 400, "one_device": 200, "burst": 300, "random_local": 150}


def ua_hash(ua: str) -> str:
    return hashlib.sha256(ua.encode()).hexdigest()[:16]


class Gen:
    def __init__(self, seed: int) -> None:
        self.r = random.Random(seed)
        self.rows: list[dict] = []
        self.emails: set[str] = set()
        self.manifest: dict[str, list[str]] = defaultdict(list)

    def uid(self) -> uuid.UUID:
        return uuid.UUID(int=self.r.getrandbits(128), version=4)

    def ip_in(self, cidr: str) -> str:
        n = ipaddress.ip_network(cidr)
        return str(n.network_address + self.r.randrange(2, n.num_addresses - 1))

    def public_ip(self) -> str:
        net = self.r.choices([n for n, _ in NETS], [w for _, w in NETS])[0]
        return self.ip_in(net)

    def unique_email(self, local: str) -> str:
        e, i = f"{local}@{DOMAIN}", 1
        while e in self.emails:
            i += 1
            e = f"{local}{i}@{DOMAIN}"
        self.emails.add(e)
        return e

    def reg_time(self) -> datetime:
        days_back = min(13, int(self.r.expovariate(1 / 4.0)))  # most people register close to the drop
        hour = self.r.choices(range(24), [1, 1, 1, 1, 1, 2, 3, 5, 8, 10, 10, 9, 8, 8, 9, 9, 9, 8, 8, 9, 9, 8, 5, 2])[0]
        base = (ANCHOR - timedelta(days=days_back + 1)).replace(hour=hour, minute=0, second=0)
        return base + timedelta(seconds=self.r.randrange(3600))

    def otp_latency_ms(self) -> int:
        return int(min(600, max(5, self.r.lognormvariate(math.log(25), 0.6))) * 1000)

    def add(self, *, cluster: str, local_or_email: str, name: str, ip: str, device: str, ua: str,
            registered: datetime, latency_ms: int | None = None, exact_email: bool = False) -> None:
        email = local_or_email if exact_email else self.unique_email(local_or_email)
        if exact_email:
            self.emails.add(email)
        lat = latency_ms if latency_ms is not None else self.otp_latency_ms()
        uid = self.uid()
        net = ipaddress.ip_address(ip)
        subnet = str(ipaddress.ip_network(f"{ip}/{24 if net.version == 4 else 64}", strict=False))
        local, domain = email.split("@")
        pattern = emailchecks.pattern_key(local, domain)
        self.rows.append(dict(
            user_id=uid, email=email, name=name, ip=ip, subnet=subnet, device=device, ua=ua_hash(ua),
            registered=registered, verified=registered + timedelta(milliseconds=lat), latency=lat,
            pattern=pattern, random_local=emailchecks.looks_random(local), cluster=cluster,
        ))
        self.manifest[cluster].append(str(uid))


def build(users: int, seed: int) -> Gen:
    g = Gen(seed)
    r = g.r
    scale = min(1.0, users / 50_000)
    planted = {k: max(10, int(v * scale)) for k, v in BASE.items()}
    legit_n = users - sum(planted.values())
    if legit_n < 100:
        raise SystemExit("--users is too small for the planted clusters (need at least ~150)")

    # --- legitimate students
    pool_devices: list[str] = []
    for _ in range(legit_n):
        first, last = r.choice(FIRST), r.choice(LAST)
        name = f"{first.title()} {last.title()}"
        if r.random() < 0.3:  # roll-number style: must NOT look like a pattern cluster
            local = f"{r.choice((2021, 2022, 2023, 2024))}{r.choice(BRANCH)}{r.randrange(1, 9999):04d}"
        else:
            local = f"{first}.{last}" + (str(r.randrange(1, 99)) if r.random() < 0.15 else "")
        if pool_devices and r.random() < 0.03:  # shared family PC / lab machine
            device = r.choice(pool_devices[-200:])
        else:
            device = str(g.uid())
            pool_devices.append(device)
        ip = r.choice(CAMPUS_NAT) if r.random() < CAMPUS_SHARE else g.public_ip()
        g.add(cluster="legit", local_or_email=local, name=name, ip=ip, device=device, ua=r.choice(UAS), registered=g.reg_time())

    # --- planted: sequential emails (typical script)
    t0 = ANCHOR - timedelta(days=2, hours=3)
    for i in range(1, planted["sequential"] + 1):
        g.add(cluster="sequential", local_or_email=f"studentbot{i}", name=f"Student Bot {i}", ip=g.ip_in("203.0.113.0/24"),
              device=str(g.uid()), ua=UAS[0], registered=t0 + timedelta(seconds=i * 1200 / planted["sequential"]),
              latency_ms=r.randrange(900, 2500))
    # --- planted: one device, many accounts
    shared_dev = str(g.uid())
    for _ in range(planted["one_device"]):
        first, last = r.choice(FIRST), r.choice(LAST)
        g.add(cluster="one_device", local_or_email=f"{first}{last}{r.choice(string.ascii_lowercase)}", name=f"{first.title()} {last.title()}",
              ip=g.ip_in("172.16.0.0/12"), device=shared_dev, ua=UAS[4],
              registered=ANCHOR - timedelta(days=3) + timedelta(seconds=r.randrange(2 * 86400)))
    # --- planted: registration burst right before the drop
    burst_ip, burst_t = "198.51.100.77", ANCHOR - timedelta(minutes=32)
    for i in range(planted["burst"]):
        first, last = r.choice(FIRST), r.choice(LAST)
        g.add(cluster="burst", local_or_email=f"{first}.{last}", name=f"{first.title()} {last.title()}", ip=burst_ip,
              device=str(g.uid()), ua=UAS[6], registered=burst_t + timedelta(seconds=r.randrange(120)),
              latency_ms=r.randrange(700, 2000))
    # --- planted: random-looking local parts from one subnet
    for _ in range(planted["random_local"]):
        local = "".join(r.choice(string.ascii_lowercase + string.digits) for _ in range(12))
        while not emailchecks.looks_random(local):
            local = "".join(r.choice(string.ascii_lowercase + string.digits) for _ in range(12))
        g.add(cluster="random_local", local_or_email=local, name="Anon User", ip=g.ip_in("192.0.2.0/24"),
              device=str(g.uid()), ua=UAS[1], registered=ANCHOR - timedelta(hours=r.randrange(1, 72)))
    return g


def sequential_flags(rows: list[dict]) -> None:
    """Same rule as identity.service.start_registration: at registration time, count distinct
    other addresses with the same pattern inside the window, plus this one."""
    by_pattern: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if row["pattern"]:
            by_pattern[row["pattern"]].append(row)
    for items in by_pattern.values():
        items.sort(key=lambda x: x["registered"])
        window: deque[dict] = deque()
        for row in items:
            while window and (row["registered"] - window[0]["registered"]).total_seconds() > emailchecks.SEQ_WINDOW_S:
                window.popleft()
            row["cluster_size"] = len({w["email"] for w in window} - {row["email"]}) + 1
            window.append(row)
    for row in rows:
        size = row.get("cluster_size", 0)
        row["flags"] = {
            "sequential_pattern": bool(row["pattern"]) and size >= emailchecks.SEQ_THRESHOLD,
            "pattern_cluster_size": size if row["pattern"] else 0,
            "random_local_part": row["random_local"],
        }


def write(dsn: str, rows: list[dict], reset: bool, create_users: bool) -> None:
    upgrade(dsn)
    with psycopg.connect(dsn) as conn:
        if create_users:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS public.users (id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                       email text NOT NULL UNIQUE, display_name text NOT NULL, created_at timestamptz NOT NULL DEFAULT now())"""
            )
        if reset:
            conn.execute("TRUNCATE defence.identities, defence.pending_registrations, public.users CASCADE")
        n = conn.execute("SELECT count(*) FROM defence.identities").fetchone()[0]
        if n:
            raise SystemExit(f"defence.identities already has {n} rows; re-run with --reset to replace them")
        with conn.cursor() as cur:
            with cur.copy("COPY public.users (id, email, display_name) FROM STDIN") as cp:
                for x in rows:
                    cp.write_row((x["user_id"], x["email"], x["name"]))
            with cur.copy(
                """COPY defence.identities (user_id, email_canonical, email_original, email_domain, email_pattern, email_flags,
                       display_name, registration_ip, registration_subnet, device_id, user_agent_hash,
                       registered_at, verified_at, otp_latency_ms) FROM STDIN"""
            ) as cp:
                for x in rows:
                    cp.write_row((x["user_id"], x["email"], x["email"], DOMAIN, x["pattern"], json.dumps(x["flags"]), x["name"],
                                  x["ip"], x["subnet"], x["device"], x["ua"], x["registered"], x["verified"], x["latency"]))
        conn.commit()


def main() -> int:
    import os

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--users", type=int, default=50_000)
    ap.add_argument("--seed", type=int, default=1337)
    ap.add_argument("--reset", action="store_true", help="TRUNCATE users + defence tables first (localhost only)")
    ap.add_argument("--create-users-table", action="store_true", help="create a stub-compatible public.users if missing")
    ap.add_argument("--manifest", default=str(ROOT / ".local" / "seed_manifest.json"))
    ap.add_argument("--dry-run", action="store_true", help="generate and print stats, write nothing")
    a = ap.parse_args()

    g = build(a.users, a.seed)
    sequential_flags(g.rows)
    rows = g.rows

    dsn = os.environ.get("DATABASE_URL", "")
    if not a.dry_run:
        if not dsn:
            raise SystemExit("DATABASE_URL not set (run via `python infra/local/run.py exec -- python scripts/seed_identities.py ...`)")
        host = psycopg.conninfo.conninfo_to_dict(dsn).get("host", "")
        if a.reset and host not in ("localhost", "127.0.0.1", "::1"):
            raise SystemExit(f"refusing --reset on non-local host {host!r}")
        write(dsn, rows, a.reset, a.create_users_table)
        Path(a.manifest).parent.mkdir(parents=True, exist_ok=True)
        Path(a.manifest).write_text(json.dumps({"seed": a.seed, "clusters": g.manifest}, indent=1))

    by_cluster: dict[str, list[dict]] = defaultdict(list)
    for x in rows:
        by_cluster[x["cluster"]].append(x)
    print(f"{'wrote' if not a.dry_run else 'generated (dry run)'} {len(rows)} identities, seed={a.seed}")
    for name, items in by_cluster.items():
        seq = sum(i["flags"]["sequential_pattern"] for i in items)
        rnd = sum(i["flags"]["random_local_part"] for i in items)
        print(f"  {name:<13} {len(items):>6}   seq-flagged {seq:>5}   random-local-flagged {rnd:>5}")
    legit = by_cluster["legit"]
    top_ip = max(((ip, sum(1 for i in legit if i["ip"] == ip)) for ip in CAMPUS_NAT), key=lambda t: t[1])
    print(f"  busiest legitimate campus-NAT IP {top_ip[0]}: {top_ip[1]} students (must not be treated as an attacker)")
    dev_counts = defaultdict(int)
    for x in rows:
        dev_counts[x["device"]] += 1
    print(f"  largest accounts-per-device: {max(dev_counts.values())}")
    if not a.dry_run:
        print("manifest:", a.manifest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
