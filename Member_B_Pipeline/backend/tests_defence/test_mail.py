import json

import pytest
from app.defence.identity import mail
from app.defence.identity.mail import FileSender, Message, SmtpSender, build_sender, otp_message
from app.defence.settings import get_settings


def test_otp_message_contains_code_and_expiry_and_no_link():
    m = otp_message("a@b.edu", "123456", 10)
    assert "123456" in m.body and "10 minutes" in m.body and "http" not in m.body


# ----------------------------------------------------------------- settings
def test_gmail_app_password_spaces_are_stripped(settings_env):
    settings_env(SMTP_HOST="smtp.gmail.com", SMTP_PORT="587", SMTP_USER="u@gmail.com", SMTP_PASSWORD="abcd efgh ijkl mnop")
    s = get_settings()
    assert s.smtp_password == "abcdefghijklmnop" and s.smtp_tls == "starttls"


def test_other_providers_keep_their_password_untouched(settings_env):
    settings_env(SMTP_HOST="smtp.example.org", SMTP_PORT="587", SMTP_USER="u", SMTP_PASSWORD="pass word")
    assert get_settings().smtp_password == "pass word"


def test_tls_defaults(settings_env):
    settings_env(SMTP_HOST="smtp.example.org", SMTP_PORT="465", SMTP_USER="u", SMTP_PASSWORD="p")
    assert get_settings().smtp_tls == "tls"
    settings_env(SMTP_HOST="localhost", SMTP_PORT="1025")  # Mailpit-style, no login
    assert get_settings().smtp_tls == "none"


def test_credentials_over_cleartext_to_a_remote_host_are_refused(settings_env):
    settings_env(SMTP_HOST="smtp.example.org", SMTP_USER="u", SMTP_PASSWORD="p", SMTP_TLS="none")
    with pytest.raises(ValueError, match="clear"):
        get_settings()
    settings_env(SMTP_HOST="127.0.0.1", SMTP_USER="u", SMTP_PASSWORD="p", SMTP_TLS="none")
    get_settings()  # a local test server is fine


def test_user_and_password_must_come_together(settings_env):
    settings_env(SMTP_HOST="smtp.gmail.com", SMTP_USER="u@gmail.com")
    with pytest.raises(ValueError, match="together"):
        get_settings()


def test_bad_tls_value_is_rejected(settings_env):
    settings_env(SMTP_HOST="h", SMTP_TLS="ssl3")
    with pytest.raises(ValueError):
        get_settings()


# ------------------------------------------------------------------- sender
async def test_smtp_sender_passes_login_and_tls_options(monkeypatch):
    import aiosmtplib

    seen = {}

    async def fake_send(message, **kw):
        seen.update(kw, to=message["To"], frm=message["From"], subject=message["Subject"], body=message.get_content())

    monkeypatch.setattr(aiosmtplib, "send", fake_send)
    await SmtpSender("smtp.gmail.com", 587, "me@gmail.com", "me@gmail.com", "app-pass", "starttls").send(Message("x@y.edu", "S", "code 123456"))
    assert seen["hostname"] == "smtp.gmail.com" and seen["port"] == 587
    assert seen["username"] == "me@gmail.com" and seen["password"] == "app-pass"
    assert seen["start_tls"] is True and seen["use_tls"] is False
    assert seen["to"] == "x@y.edu" and seen["frm"] == "me@gmail.com" and "123456" in seen["body"]

    seen.clear()
    await SmtpSender("h", 465, "f@h", "u", "p", "tls").send(Message("x@y.edu", "S", "b"))
    assert seen["use_tls"] is True and not seen["start_tls"]

    seen.clear()
    await SmtpSender("localhost", 1025, "f@h").send(Message("x@y.edu", "S", "b"))
    assert seen["username"] is None and seen["password"] is None and not seen["use_tls"] and not seen["start_tls"]


def test_the_password_never_appears_in_repr():
    assert "super-secret" not in repr(SmtpSender("h", 587, "f@h", "u", "super-secret", "starttls"))


def test_build_sender_picks_smtp_or_file(settings_env, tmp_path):
    settings_env(SMTP_HOST="smtp.example.org", SMTP_USER="u", SMTP_PASSWORD="p")
    assert isinstance(build_sender(get_settings()), SmtpSender)
    settings_env(FD_ENV="dev", OUTBOX_DIR=str(tmp_path))
    assert isinstance(build_sender(get_settings()), FileSender)
    settings_env(FD_ENV="prod")
    with pytest.raises(RuntimeError):  # the file outbox stores codes in clear text
        build_sender(get_settings())


async def test_file_outbox_writes_one_json_per_message(tmp_path):
    await FileSender(str(tmp_path)).send(Message("a@b.edu", "S", "code 654321"))
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1 and json.loads(files[0].read_text())["body"] == "code 654321"
    assert "a@b" not in files[0].name  # filename never derived from the address
