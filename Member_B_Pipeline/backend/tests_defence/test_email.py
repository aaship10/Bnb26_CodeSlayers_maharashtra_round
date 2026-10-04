import pytest
from app.defence.identity import emailchecks as ec
from app.defence.identity.emailcanon import EmailError, canonicalize


@pytest.mark.parametrize(
    "raw,canonical",
    [
        ("Rahul.Sharma@Example-College.EDU", "rahul.sharma@example-college.edu"),
        ("  rahul@example-college.edu  ", "rahul@example-college.edu"),
        ("rahul+drop1@example-college.edu", "rahul@example-college.edu"),
        ("rahul+a+b@example-college.edu", "rahul@example-college.edu"),
        ("r.a.h.u.l@gmail.com", "rahul@gmail.com"),  # gmail: dots are insignificant
        ("R.AHUL+x@googlemail.com", "rahul@gmail.com"),  # googlemail folds into gmail
        ("rahul.sharma@example-college.edu", "rahul.sharma@example-college.edu"),  # dots kept elsewhere
    ],
)
def test_canonical_forms(raw, canonical):
    assert canonicalize(raw).canonical == canonical


def test_aliases_collapse_to_one_identity_key():
    variants = ["a.b@gmail.com", "ab@gmail.com", "A.B+x@gmail.com", "ab@googlemail.com", "a.b.+y@gmail.com".replace(".+", "+")]
    assert len({canonicalize(v).canonical for v in variants}) == 1


def test_dots_are_not_collapsed_at_institutional_domains():
    # john.smith and johnsmith may be different students: documented trade-off.
    assert canonicalize("john.smith@example-college.edu").canonical != canonicalize("johnsmith@example-college.edu").canonical


@pytest.mark.parametrize(
    "raw,reason",
    [
        ("", "empty"),
        ("   ", "empty"),
        ("no-at-sign", "malformed"),
        ("a@b@c.edu", "malformed"),
        ("rahul@localhost", "bad_domain"),
        ("rahul@-bad.edu", "bad_domain"),
        ("rahul@exa mple.edu", "bad_domain"),
        ("rahul@example..edu", "bad_domain"),
        ("rahul@example.123", "bad_domain"),
        ("+tag@example-college.edu", "empty_after_tag"),
        ("ra..hul@example-college.edu", "bad_local"),
        (".rahul@example-college.edu", "bad_local"),
        ("rahul.@example-college.edu", "bad_local"),
        ("ra hul@example-college.edu", "bad_local"),
        ("ra\nhul@example-college.edu", "bad_local"),
        ("rahul@example-college.edu\r\nBcc: x@y.z", "malformed"),  # header-injection attempt (second '@')
        ("rahul@example-college.edu\r\nBcc: x", "bad_domain"),  # ...and without one
        ("ｒａｈｕｌ@example-college.edu", "not_ascii"),  # fullwidth homoglyphs
        ("rahul@exämple.edu", "not_ascii"),
        ("a" * 65 + "@example-college.edu", "bad_local"),
        ("a@" + "b" * 250 + ".edu", "too_long"),
    ],
)
def test_rejected_inputs_have_stable_reasons(raw, reason):
    with pytest.raises(EmailError) as exc:
        canonicalize(raw)
    assert exc.value.reason == reason


def test_allowlist_matching():
    allow = ("example-college.edu", "*.mail.example-college.edu")
    assert ec.domain_allowed("example-college.edu", allow)
    assert ec.domain_allowed("x.mail.example-college.edu", allow)
    assert not ec.domain_allowed("mail.example-college.edu", allow)  # bare entry does not admit subdomains
    assert not ec.domain_allowed("example-college.edu.attacker.com", allow)
    assert not ec.domain_allowed("evilexample-college.edu", allow)
    assert ec.domain_allowed("anything.org", ("*",))
    assert not ec.domain_allowed("anything.org", ())


def test_disposable_domains_including_subdomains():
    assert ec.is_disposable("mailinator.com")
    assert ec.is_disposable("x.mailinator.com")
    assert not ec.is_disposable("notmailinator.com")
    assert not ec.is_disposable("example-college.edu")


@pytest.mark.parametrize(
    "a,b",
    [("user1", "user2"), ("user_17", "user.9"), ("bot001", "bot250"), ("john.smith1", "johnsmith22")],
)
def test_sequential_addresses_share_a_pattern(a, b):
    assert ec.pattern_key(a, "d.edu") == ec.pattern_key(b, "d.edu") is not None


@pytest.mark.parametrize("local", ["rahul.sharma", "2021001", "ab12", "12345678"])
def test_unclusterable_addresses_have_no_pattern(local):
    # roll-number style addresses must not all collapse into one "pattern"
    assert ec.pattern_key(local, "d.edu") is None


def test_pattern_is_scoped_to_domain():
    assert ec.pattern_key("user1", "a.edu") != ec.pattern_key("user1", "b.edu")


@pytest.mark.parametrize("local", ["xk2j9qpzv4mw", "q8w3e7r1t5y9u2", "a1b2c3d4e5f6g7"])
def test_random_looking_local_parts_are_flagged(local):
    assert ec.looks_random(local)


@pytest.mark.parametrize(
    "local", ["rahul.sharma", "rahul.sharma2021", "priya.patel", "jonathanwilliams", "k.nair", "student2021", "aaaaaaaaaa1"]
)
def test_ordinary_names_are_not_flagged(local):
    assert not ec.looks_random(local)
