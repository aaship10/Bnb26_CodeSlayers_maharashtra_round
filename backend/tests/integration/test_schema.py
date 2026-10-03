"""Stage 1: the database itself enforces the integrity guarantees.

Each test bypasses the application entirely and writes raw SQL, proving that a
buggy code path (or a manual query) still cannot oversell, double-allocate,
mix events, rewrite draw results, or edit the audit log.
"""
import psycopg
import pytest
from psycopg import errors

from tests.conftest import hold, make_entry, make_event, make_user, seat_id

pytestmark = pytest.mark.db


def test_migration_head_applied(db, migrated_db):
    from alembic.script import ScriptDirectory

    from tests.conftest import alembic_config
    head = ScriptDirectory.from_config(alembic_config(migrated_db)).get_current_head()
    assert db.execute("SELECT version_num FROM public.alembic_version").fetchone()[0] == head


def test_one_entry_per_user_per_event(db):
    ev, u = make_event(db), make_user(db)
    make_entry(db, ev, u)
    with pytest.raises(errors.UniqueViolation):
        make_entry(db, ev, u)
    # ...but the same user may enter a different event.
    make_entry(db, make_event(db), u)


def test_seat_cannot_have_two_active_allocations(db):
    ev = make_event(db, inventory=1)
    e1, e2 = make_entry(db, ev, make_user(db)), make_entry(db, ev, make_user(db))
    s = seat_id(db, ev, 1)
    hold(db, ev, e1, s, "HELD")
    with pytest.raises(errors.UniqueViolation):
        hold(db, ev, e2, s, "HELD")
    with pytest.raises(errors.UniqueViolation):
        hold(db, ev, e2, s, "CONFIRMED")


def test_entry_cannot_hold_two_seats(db):
    ev = make_event(db, inventory=2)
    e = make_entry(db, ev, make_user(db))
    hold(db, ev, e, seat_id(db, ev, 1))
    with pytest.raises(errors.UniqueViolation):
        hold(db, ev, e, seat_id(db, ev, 2))


def test_expired_allocation_frees_seat_for_next_holder(db):
    ev = make_event(db, inventory=1)
    e1, e2 = make_entry(db, ev, make_user(db)), make_entry(db, ev, make_user(db))
    s = seat_id(db, ev, 1)
    a1 = hold(db, ev, e1, s)
    db.execute("UPDATE allocations SET status = 'EXPIRED', ended_at = fd_now() WHERE id = %s", (a1,))
    hold(db, ev, e2, s)  # allowed: only HELD/CONFIRMED rows are "active"
    active = db.execute(
        "SELECT count(*) FROM allocations WHERE seat_id = %s AND status IN ('HELD','CONFIRMED')", (s,)
    ).fetchone()[0]
    assert active == 1


def test_allocation_cannot_mix_events(db):
    ev_a, ev_b = make_event(db), make_event(db)
    entry_a = make_entry(db, ev_a, make_user(db))
    with pytest.raises(errors.ForeignKeyViolation):
        hold(db, ev_a, entry_a, seat_id(db, ev_b, 1))  # seat from another event


def test_seat_numbers_unique_per_event(db):
    ev = make_event(db, inventory=3)
    with pytest.raises(errors.UniqueViolation):
        db.execute("INSERT INTO seats (event_id, seat_no) VALUES (%s, 2)", (ev,))


def test_allocation_status_shape_checks(db):
    ev = make_event(db)
    e = make_entry(db, ev, make_user(db))
    with pytest.raises(errors.CheckViolation):  # HELD without expiry
        db.execute(
            "INSERT INTO allocations (event_id, entry_id, seat_id, status, held_at) "
            "VALUES (%s, %s, %s, 'HELD', fd_now())",
            (ev, e, seat_id(db, ev, 1)),
        )
    with pytest.raises(errors.CheckViolation):  # CONFIRMED without ticket
        db.execute(
            "INSERT INTO allocations (event_id, entry_id, seat_id, status, confirmed_at) "
            "VALUES (%s, %s, %s, 'CONFIRMED', fd_now())",
            (ev, e, seat_id(db, ev, 1)),
        )


@pytest.mark.parametrize(
    "path, ok",
    [
        (["WON", "CLAIMED"], True),
        (["WAITLISTED", "WON", "EXPIRED"], True),
        (["WAITLISTED", "LOST"], True),
        (["CLAIMED"], True),           # FCFS
        (["LOST"], True),              # FCFS / weight 0
        (["WON", "WAITLISTED"], False),
        (["CLAIMED", "ENTERED"], False),
        (["LOST", "WON"], False),
        (["EXPIRED"], False),          # must have been WON first
    ],
)
def test_entry_state_machine(db, path, ok):
    ev = make_event(db)
    e = make_entry(db, ev, make_user(db))

    def run():
        for i, st in enumerate(path):
            wl = 1 if st == "WAITLISTED" else None
            db.execute(
                "UPDATE entries SET state = %s, draw_rank = COALESCE(draw_rank, %s), "
                "waitlist_position = COALESCE(waitlist_position, %s) WHERE id = %s",
                (st, 1 if wl else None, wl, e),
            )

    if ok:
        run()
    else:
        with pytest.raises(errors.CheckViolation):
            run()


@pytest.mark.parametrize(
    "path, ok",
    [
        (["CONFIRMED"], True),
        (["EXPIRED"], True),
        (["CONFIRMED", "RELEASED"], True),
        (["EXPIRED", "HELD"], False),
        (["CONFIRMED", "HELD"], False),
        (["EXPIRED", "CONFIRMED"], False),
    ],
)
def test_allocation_state_machine(db, path, ok):
    ev = make_event(db)
    e = make_entry(db, ev, make_user(db))
    a = hold(db, ev, e, seat_id(db, ev, 1))

    def run():
        for st in path:
            db.execute(
                "UPDATE allocations SET status = %s, ended_at = COALESCE(ended_at, fd_now()), "
                "confirmed_at = COALESCE(confirmed_at, fd_now()), "
                "ticket_code = COALESCE(ticket_code, gen_random_uuid()::text) WHERE id = %s",
                (st, a),
            )

    if ok:
        run()
    else:
        with pytest.raises(errors.CheckViolation):
            run()


def test_allocation_seat_is_immutable(db):
    ev = make_event(db, inventory=2)
    a = hold(db, ev, make_entry(db, ev, make_user(db)), seat_id(db, ev, 1))
    with pytest.raises(errors.CheckViolation):
        db.execute("UPDATE allocations SET seat_id = %s WHERE id = %s", (seat_id(db, ev, 2), a))


def test_draw_results_are_immutable(db):
    ev = make_event(db)
    e = make_entry(db, ev, make_user(db))
    db.execute("UPDATE entries SET draw_rank = 7, waitlist_position = 2, state = 'WAITLISTED' WHERE id = %s", (e,))
    with pytest.raises(errors.CheckViolation):
        db.execute("UPDATE entries SET draw_rank = 1 WHERE id = %s", (e,))
    with pytest.raises(errors.CheckViolation):
        db.execute("UPDATE entries SET waitlist_position = 1 WHERE id = %s", (e,))
    with pytest.raises(errors.CheckViolation):
        db.execute("UPDATE entries SET weight = 0.5 WHERE id = %s", (e,))


def test_weight_and_identity_constraints(db):
    ev = make_event(db)
    e = make_entry(db, ev, make_user(db))
    db.execute("UPDATE entries SET weight = 0.25 WHERE id = %s", (e,))  # allowed pre-draw
    with pytest.raises(errors.CheckViolation):
        db.execute("UPDATE entries SET weight = -1 WHERE id = %s", (e,))
    with pytest.raises(errors.CheckViolation):
        db.execute("UPDATE entries SET weight = 2 WHERE id = %s", (e,))
    with pytest.raises(errors.CheckViolation):
        db.execute("UPDATE entries SET public_id = gen_random_uuid() WHERE id = %s", (e,))


def test_waitlist_positions_unique_per_event(db):
    ev = make_event(db)
    e1, e2 = make_entry(db, ev, make_user(db)), make_entry(db, ev, make_user(db))
    db.execute("UPDATE entries SET state='WAITLISTED', draw_rank=6, waitlist_position=1 WHERE id=%s", (e1,))
    with pytest.raises(errors.UniqueViolation):
        db.execute("UPDATE entries SET state='WAITLISTED', draw_rank=7, waitlist_position=1 WHERE id=%s", (e2,))


def test_event_checks(db):
    with pytest.raises(errors.CheckViolation):  # window ends before it starts
        db.execute(
            "INSERT INTO events (name, inventory, window_opens_at, window_closes_at, "
            "claim_ttl_seconds, claim_phase_seconds) VALUES ('x', 5, fd_now(), fd_now() - interval '1s', 60, 60)"
        )
    ev = make_event(db)
    with pytest.raises(errors.CheckViolation):  # lottery cannot leave DRAFT without a commitment
        db.execute("UPDATE events SET phase = 'SCHEDULED' WHERE id = %s", (ev,))
    with pytest.raises(errors.CheckViolation):  # seed cannot be revealed before the draw
        db.execute("UPDATE events SET revealed_seed = repeat('a', 64) WHERE id = %s", (ev,))
    with pytest.raises(errors.CheckViolation):
        db.execute("UPDATE events SET mode = 'RANDOM' WHERE id = %s", (ev,))
    with pytest.raises(errors.CheckViolation):
        db.execute("UPDATE events SET config = '[1,2]' WHERE id = %s", (ev,))


def test_sim_label_values(db):
    make_user(db, "bot")
    make_user(db, "human")
    make_user(db, None)
    with pytest.raises(errors.CheckViolation):
        make_user(db, "robot")


def _audit_row(db, prev="0" * 64, h=None):
    return db.execute(
        "INSERT INTO audit_log (event_id, type, payload, prev_hash, hash, created_at) "
        "VALUES (NULL, 'test', '{}', %s, %s, fd_now()) RETURNING seq",
        (prev, h or "1" * 64),
    ).fetchone()[0]


def test_audit_log_is_append_only(db):
    seq = _audit_row(db)
    with pytest.raises(errors.InsufficientPrivilege):
        db.execute("UPDATE audit_log SET type = 'forged' WHERE seq = %s", (seq,))
    with pytest.raises(errors.InsufficientPrivilege):
        db.execute("DELETE FROM audit_log WHERE seq = %s", (seq,))
    with pytest.raises(errors.InsufficientPrivilege):
        db.execute("TRUNCATE audit_log")
    assert db.execute("SELECT count(*) FROM audit_log").fetchone()[0] == 1


def test_audit_reset_escape_hatch_is_transaction_local(db):
    _audit_row(db)
    with db.transaction():
        db.execute("SET LOCAL fairdrop.allow_audit_reset = 'on'")
        db.execute("DELETE FROM audit_log")
    _audit_row(db, h="2" * 64)
    # The flag died with its transaction: deletes are blocked again.
    with pytest.raises(errors.InsufficientPrivilege):
        db.execute("DELETE FROM audit_log")


def test_fd_now_offset(db):
    real = db.execute("SELECT now()").fetchone()[0]
    db.execute("UPDATE dev_clock SET offset_seconds = 3600 WHERE id")
    shifted = db.execute("SELECT fd_now()").fetchone()[0]
    assert 3599 < (shifted - real).total_seconds() < 3700


def test_fd_now_is_constant_within_a_transaction(db):
    with db.transaction():
        a = db.execute("SELECT fd_now()").fetchone()[0]
        db.execute("SELECT pg_sleep(0.05)")
        b = db.execute("SELECT fd_now()").fetchone()[0]
    assert a == b


def test_dev_clock_single_row(db):
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("INSERT INTO dev_clock (id) VALUES (false)")
    with pytest.raises(psycopg.errors.UniqueViolation):
        db.execute("INSERT INTO dev_clock (id) VALUES (true)")
