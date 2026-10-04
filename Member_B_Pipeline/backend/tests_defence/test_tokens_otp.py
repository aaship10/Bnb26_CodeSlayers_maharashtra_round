import time
import uuid

import jwt
import pytest
from app.defence.identity import otp, tokens
from app.defence.identity.tokens import TokenError

SECRET = "s" * 40
UID = uuid.UUID("12345678-1234-5678-1234-567812345678")


@pytest.fixture(autouse=True)
def env(settings_env):
    settings_env(JWT_SECRET=SECRET, AUTH_MODE="jwt")


def test_mint_decode_round_trip():
    tok, exp = tokens.mint(UID, email="a@b.edu", name="Ann")
    c = tokens.decode(tok)
    assert c.user_id == UID and c.email == "a@b.edu" and c.name == "Ann" and c.exp == exp


def test_expired_token_is_rejected_but_refreshable_inside_grace():
    now = int(time.time())
    tok, _ = tokens.mint(UID, now=now - 4000, ttl_s=1800)  # expired ~2200 s ago
    with pytest.raises(TokenError) as e:
        tokens.decode(tok)
    assert e.value.reason == "expired"
    assert tokens.decode(tok, grace_s=6 * 3600).user_id == UID


def test_refresh_grace_has_a_limit():
    now = int(time.time())
    tok, _ = tokens.mint(UID, now=now - 10 * 3600, ttl_s=1800)
    with pytest.raises(TokenError) as e:
        tokens.decode(tok, grace_s=6 * 3600)
    assert e.value.reason == "expired"


def test_session_cannot_be_extended_forever_by_refreshing():
    now = int(time.time())
    # auth_time 3 days ago, token itself fresh (as if refreshed repeatedly)
    tok, _ = tokens.mint(UID, auth_time=now - 3 * 86400, now=now - 100, ttl_s=1800)
    assert tokens.decode(tok).user_id == UID  # still a valid access token...
    with pytest.raises(TokenError) as e:
        tokens.decode(tok, grace_s=6 * 3600)  # ...but cannot be refreshed past SESSION_MAX_S
    assert e.value.reason == "session_too_old"


def test_wrong_secret_garbage_and_alg_none_are_rejected():
    now = int(time.time())
    payload = {"iss": "fairdrop", "sub": str(UID), "iat": now, "exp": now + 600}
    for bad in (
        jwt.encode(payload, "x" * 40, algorithm="HS256"),
        "not.a.jwt",
        "",
        jwt.encode(payload, None, algorithm="none"),  # unsigned token
        jwt.encode(payload, SECRET, algorithm="HS512"),  # algorithm confusion
    ):
        with pytest.raises(TokenError) as e:
            tokens.decode(bad)
        assert e.value.reason == "invalid"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.pop("exp"),
        lambda p: p.pop("sub"),
        lambda p: p.update(sub="not-a-uuid"),
        lambda p: p.update(iss="someone-else"),
        lambda p: p.update(iat=p["iat"] + 3600),  # minted in the future
    ],
)
def test_malformed_claims_are_rejected(mutate):
    now = int(time.time())
    payload = {"iss": "fairdrop", "sub": str(UID), "iat": now, "exp": now + 600}
    mutate(payload)
    with pytest.raises(TokenError):
        tokens.decode(jwt.encode(payload, SECRET, algorithm="HS256"))


def test_otp_generation_format_and_spread():
    codes = {otp.generate_otp() for _ in range(300)}
    assert all(len(c) == 6 and c.isdigit() for c in codes)
    assert len(codes) > 250  # not constant / not tiny space


def test_otp_hash_is_bound_to_email_and_pepper():
    h = otp.hash_otp("pep", "a@x.edu", "123456")
    assert "123456" not in h
    assert otp.verify_otp("pep", "a@x.edu", "123456", h)
    assert not otp.verify_otp("pep", "a@x.edu", "123457", h)
    assert not otp.verify_otp("pep", "b@x.edu", "123456", h)  # hash copied to another row
    assert not otp.verify_otp("other", "a@x.edu", "123456", h)


@pytest.mark.parametrize("s,ok", [("123456", True), ("000000", True), ("12345", False), ("1234567", False), ("12345a", False), ("", False), (" 12345", False)])
def test_otp_format(s, ok):
    assert otp.is_well_formed(s) is ok
