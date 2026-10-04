"""The seeder is test infrastructure for stages 5+: its planted ground truth and its
false-positive behaviour must be right, or every later measurement is built on sand."""
import importlib.util
from collections import Counter, defaultdict
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "seed_identities.py"


@pytest.fixture(scope="module")
def seed():
    spec = importlib.util.spec_from_file_location("seed_identities", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def data(seed):
    g = seed.build(5000, 7)
    seed.sequential_flags(g.rows)
    return g


def test_same_seed_gives_identical_data(seed, data):
    g2 = seed.build(5000, 7)
    seed.sequential_flags(g2.rows)
    key = lambda g: [(r["user_id"], r["email"], r["ip"], r["device"], r["registered"]) for r in g.rows]  # noqa: E731
    assert key(g2) == key(data)
    g3 = seed.build(5000, 8)
    assert key(g3) != key(data)


def test_counts_and_uniqueness(data):
    assert len(data.rows) == 5000
    assert len({r["email"] for r in data.rows}) == 5000
    assert len({r["user_id"] for r in data.rows}) == 5000


def test_planted_clusters_exist_with_expected_shape(data):
    by = defaultdict(list)
    for r in data.rows:
        by[r["cluster"]].append(r)
    assert {"legit", "sequential", "one_device", "burst", "random_local"} <= set(by)

    # one device carries every account of its cluster
    assert len({r["device"] for r in by["one_device"]}) == 1

    # burst: one IP, all inside two minutes
    assert len({r["ip"] for r in by["burst"]}) == 1
    span = max(r["registered"] for r in by["burst"]) - min(r["registered"] for r in by["burst"])
    assert span.total_seconds() <= 120

    # sequential: the 5th and later addresses in the window are flagged, none of the legit ones are
    flagged = sum(r["flags"]["sequential_pattern"] for r in by["sequential"])
    assert flagged >= len(by["sequential"]) - 4
    assert all(r["random_local"] for r in by["random_local"])


def test_legitimate_population_is_not_flagged(data):
    legit = [r for r in data.rows if r["cluster"] == "legit"]
    assert sum(r["flags"]["sequential_pattern"] for r in legit) == 0
    assert sum(r["flags"]["random_local_part"] for r in legit) == 0


def test_campus_nat_creates_a_large_legitimate_shared_ip(seed, data):
    legit = [r for r in data.rows if r["cluster"] == "legit"]
    per_ip = Counter(r["ip"] for r in legit)
    ip, n = per_ip.most_common(1)[0]
    assert ip in seed.CAMPUS_NAT and n > 100  # the false-positive trap for IP-based signals exists


def test_legit_devices_are_mostly_one_to_one(data):
    legit = [r for r in data.rows if r["cluster"] == "legit"]
    per_dev = Counter(r["device"] for r in legit)
    assert max(per_dev.values()) <= 6
    assert sum(1 for c in per_dev.values() if c == 1) / len(per_dev) > 0.9


def test_only_documentation_and_private_ranges_are_used(data):
    import ipaddress

    allowed = [ipaddress.ip_network(n) for n in
               ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")]
    assert all(any(ipaddress.ip_address(r["ip"]) in n for n in allowed) for r in data.rows)


def test_otp_latency_is_plausible_for_humans_and_fast_for_scripts(data):
    legit = sorted(r["latency"] for r in data.rows if r["cluster"] == "legit")
    median = legit[len(legit) // 2]
    assert 10_000 < median < 60_000
    assert all(r["latency"] < 3000 for r in data.rows if r["cluster"] in ("sequential", "burst"))


def test_registration_times_precede_verification_and_the_drop(seed, data):
    assert all(r["verified"] > r["registered"] for r in data.rows)
    assert all(r["registered"] < seed.ANCHOR for r in data.rows)


def test_too_small_population_is_refused(seed):
    with pytest.raises(SystemExit):
        seed.build(120, 1)


@pytest.mark.asyncio(loop_scope="session")
async def test_write_to_database_round_trips(seed, pg_dsn):
    import asyncio

    import psycopg

    g = seed.build(400, 3)
    seed.sequential_flags(g.rows)
    await asyncio.to_thread(seed.write, pg_dsn, g.rows, True, True)
    with psycopg.connect(pg_dsn) as c:
        assert c.execute("SELECT count(*) FROM defence.identities").fetchone()[0] == 400
        assert c.execute("SELECT count(*) FROM public.users").fetchone()[0] == 400
        assert c.execute("SELECT count(*) FROM defence.identities i JOIN public.users u ON u.id = i.user_id").fetchone()[0] == 400
        # a second run without --reset must refuse rather than duplicate
        with pytest.raises(SystemExit):
            await asyncio.to_thread(seed.write, pg_dsn, g.rows, False, False)
        c.execute("TRUNCATE defence.identities, defence.pending_registrations, public.users CASCADE")
        c.commit()
