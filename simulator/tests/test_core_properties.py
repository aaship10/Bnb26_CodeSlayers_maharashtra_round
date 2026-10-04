"""Property tests on the mock's domain logic: whatever the sequence of enters,
claims and clock moves, the invariants hold and seats are never oversold."""
from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from mock_server.core import MockError, Store, draw_order

T0 = 1_000_000.0

action = st.one_of(
    st.tuples(st.just("claim"), st.integers(0, 39)),
    st.tuples(st.just("advance"), st.floats(0.1, 20)),
    st.tuples(st.just("status"), st.integers(0, 39)),
)


@settings(max_examples=150, deadline=None)
@given(
    mode=st.sampled_from(["LOTTERY", "FCFS"]),
    inventory=st.integers(1, 10),
    n_users=st.integers(0, 40),
    actions=st.lists(action, max_size=80),
)
def test_invariants_hold(mode, inventory, n_users, actions):
    s = Store()
    ev = s.create_event(T0, inventory=inventory, mode=mode, window_seconds=10, claim_ttl_seconds=5,
                        server_seed_hex="cd" * 32)
    now = T0
    s.open(ev, now)
    for i in range(n_users):
        s.enter(ev, f"u{i}", now + i * 0.01, "1.1.1.1", None)
    now += 10
    if mode == "LOTTERY":
        s.draw(ev, now)
    for kind, arg in actions:
        if kind == "advance":
            now += arg
        elif kind == "claim":
            try:
                s.claim(ev, f"u{arg}", f"key-{arg}-xxxx", now)
            except MockError as e:
                assert e.code in ("NOT_WINNER", "ALREADY_CLAIMED", "HOLD_EXPIRED")
        else:
            s.status(ev, f"u{arg}", now)
        inv = s.invariants(ev, now)
        assert inv["passed"], inv
        assert ev.held + ev.confirmed <= ev.inventory
    seats = [e.seat_no for e in ev.entries.values() if e.seat_no is not None]
    assert sorted(seats) == list(range(1, len(seats) + 1))


def test_draw_is_order_independent():
    def queue(order):
        s = Store()
        ev = s.create_event(T0, inventory=5, server_seed_hex="ef" * 32, event_id="evt_x")
        s.open(ev, T0)
        for i, u in enumerate(order):
            s.enter(ev, u, T0 + i, "ip", None)
        s.close(ev, T0 + 30)
        s.draw(ev, T0 + 30)
        return ev.queue

    users = [f"user{i}" for i in range(50)]
    assert queue(users) == queue(users[::-1])


def test_weighted_draw_frequencies():
    """Weight-0.5 users should win about half as often as weight-1.0 users (many seeds)."""
    entrants = [(f"a{i}", 1.0) for i in range(100)] + [(f"b{i}", 0.5) for i in range(100)]
    a = b = 0
    for seed in range(300):
        top = draw_order(f"{seed:064x}", "evt", entrants)[:10]
        a += sum(u.startswith("a") for u in top)
        b += sum(u.startswith("b") for u in top)
    assert 0.42 < b / a < 0.58, b / a
