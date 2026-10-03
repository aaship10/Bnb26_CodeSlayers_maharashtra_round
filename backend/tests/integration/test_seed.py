import pytest

from scripts import seed as seed_mod

pytestmark = pytest.mark.db


def test_seed_is_deterministic_and_idempotent(db, migrated_db):
    args = ["--users", "2000", "--bot-fraction", "0.25", "--seed", "7", "--database-url", migrated_db]
    assert seed_mod.main(args) == 0
    first = db.execute("SELECT id, email, sim_label FROM users ORDER BY id").fetchall()
    assert seed_mod.main(args) == 0  # second run inserts nothing
    assert db.execute("SELECT id, email, sim_label FROM users ORDER BY id").fetchall() == first

    assert len(first) == 2001  # + admin
    assert db.execute("SELECT count(*) FROM users WHERE sim_label = 'bot'").fetchone()[0] == 500
    events = dict(db.execute("SELECT id, inventory FROM events").fetchall())
    assert events == {seed_mod.SMALL_EVENT_ID: 5, seed_mod.MAIN_EVENT_ID: 500, seed_mod.FCFS_EVENT_ID: 500}
    assert db.execute("SELECT count(*) FROM events WHERE phase = 'DRAFT'").fetchone()[0] == 3


def test_seed_reset_wipes_previous_data(db, migrated_db):
    seed_mod.main(["--users", "100", "--seed", "1", "--database-url", migrated_db])
    seed_mod.main(["--users", "50", "--seed", "2", "--reset", "--database-url", migrated_db])
    assert db.execute("SELECT count(*) FROM users").fetchone()[0] == 51
