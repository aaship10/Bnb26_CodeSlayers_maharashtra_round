from scripts.seed import generate_users


def test_generate_users_deterministic():
    assert generate_users(500, 0.2, 42) == generate_users(500, 0.2, 42)
    assert generate_users(500, 0.2, 42) != generate_users(500, 0.2, 43)


def test_generate_users_labels_and_uniqueness():
    users = generate_users(1000, 0.3, 1)
    assert sum(u.sim_label == "bot" for u in users) == 300
    assert len({u.id for u in users}) == 1000
    assert len({u.email for u in users}) == 1000
    # Names and emails must not reveal the label.
    assert not any("bot" in (u.email + u.display_name).lower() for u in users)
