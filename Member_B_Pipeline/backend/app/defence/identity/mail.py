"""Outgoing mail: SMTP (Mailpit or any server) or a file outbox for dev.

The file outbox exists so the stack runs on a laptop with no mail server at all:
messages land in OUTBOX_DIR as JSON. It refuses to run outside FD_ENV=dev because it
writes one-time codes in clear text to disk.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Protocol

from ..settings import Settings

log = logging.getLogger("fd.mail")


@dataclass(frozen=True)
class Message:
    to: str
    subject: str
    body: str


class MailSender(Protocol):
    async def send(self, msg: Message) -> None: ...


def otp_message(to: str, otp: str, ttl_minutes: int) -> Message:
    return Message(
        to=to,
        subject="Your Fair Drop verification code",
        body=(
            f"Your verification code is {otp}\n\n"
            f"It expires in {ttl_minutes} minutes and can be used once. "
            "If you did not request it, ignore this message."
        ),
    )


class SmtpSender:
    def __init__(self, host: str, port: int, sender: str, user: str = "", password: str = "", tls: str = "none") -> None:
        self._host, self._port, self._from = host, port, sender
        self._user, self._password, self._tls = user, password, tls

    def __repr__(self) -> str:  # never let the password reach a log line via repr()
        return f"SmtpSender({self._host}:{self._port}, user={self._user!r}, tls={self._tls})"

    async def send(self, msg: Message) -> None:
        import aiosmtplib  # imported lazily: only needed when SMTP is configured

        em = EmailMessage()
        em["From"], em["To"], em["Subject"] = self._from, msg.to, msg.subject
        em.set_content(msg.body)
        await aiosmtplib.send(
            em,
            hostname=self._host,
            port=self._port,
            username=self._user or None,
            password=self._password or None,
            use_tls=self._tls == "tls",  # implicit TLS (port 465)
            start_tls=True if self._tls == "starttls" else None,  # upgrade a plain connection (port 587)
            timeout=10,
        )


class FileSender:
    def __init__(self, directory: str) -> None:
        self._dir = Path(directory)

    def _write(self, msg: Message) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        name = f"{time.time_ns()}-{uuid.uuid4().hex[:8]}.json"  # never derived from the address
        (self._dir / name).write_text(
            json.dumps({"to": msg.to, "subject": msg.subject, "body": msg.body}, indent=2), encoding="utf-8"
        )

    async def send(self, msg: Message) -> None:
        await asyncio.to_thread(self._write, msg)


def build_sender(s: Settings) -> MailSender:
    if s.smtp_host:
        return SmtpSender(s.smtp_host, s.smtp_port, s.mail_from, s.smtp_user, s.smtp_password, s.smtp_tls)
    if s.env != "dev":
        raise RuntimeError("SMTP_HOST must be set outside FD_ENV=dev (the file outbox stores codes in clear text)")
    return FileSender(s.outbox_dir)
