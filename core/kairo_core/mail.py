"""Gmail over IMAP/SMTP with an app password.

- ``parse_message`` turns raw RFC 822 bytes into the pipeline's ``Message``.
- ``Mailbox`` reads INBOX by UID (with ``BODY.PEEK`` so nothing is marked read), waits
  with IMAP IDLE, and saves drafts with APPEND. It has no send method.
- ``SmtpSender`` is only called by ``outbox.Outbox.send`` after an approval token checks out.
"""

from __future__ import annotations

import html
import imaplib
import json
import re
import smtplib
import socket
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import make_msgid, parsedate_to_datetime
from pathlib import Path

from .prefilter import Message

MAX_BODY_CHARS = 20_000
TAG = re.compile(r"<[^>]+>")
BLOCK_TAG = re.compile(r"(?is)<(script|style|head)\b.*?</\1>")
SPACES = re.compile(r"[ \t\r\f\v]+")
BLANK_LINES = re.compile(r"\n{3,}")


def html_to_text(markup: str) -> str:
    text = BLOCK_TAG.sub(" ", markup)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>", "\n", text)
    text = html.unescape(TAG.sub(" ", text))
    return BLANK_LINES.sub("\n\n", "\n".join(SPACES.sub(" ", line).strip() for line in text.splitlines()))


def _body_text(msg) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        content = part.get_content()
    except (LookupError, UnicodeDecodeError):
        content = part.get_payload(decode=True).decode("utf-8", "replace")
    if part.get_content_subtype() == "html":
        content = html_to_text(content)
    return content.strip()[:MAX_BODY_CHARS]


def parse_message(raw: bytes, internal_date: datetime | None = None) -> Message:
    msg = message_from_bytes(raw, policy=policy.default)
    received = internal_date
    if received is None and msg["Date"]:
        try:
            received = parsedate_to_datetime(str(msg["Date"]))
        except (TypeError, ValueError):
            received = None
    received = received or datetime.now(timezone.utc)
    if received.tzinfo is None:
        received = received.replace(tzinfo=timezone.utc)
    return Message(
        message_id=str(msg["Message-ID"] or "").strip(),
        sender=str(msg["From"] or ""),
        subject=str(msg["Subject"] or ""),
        received_at=received,
        body=_body_text(msg),
        list_unsubscribe=msg["List-Unsubscribe"] is not None,
    )


@dataclass
class Cursor:
    """Where the last sweep stopped. A new UIDVALIDITY means the UIDs were renumbered."""
    uidvalidity: int = 0
    last_uid: int = 0

    @classmethod
    def load(cls, path: Path) -> "Cursor":
        if path.exists():
            return cls(**json.loads(path.read_text()))
        return cls()

    def save(self, path: Path) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.__dict__))
        tmp.replace(path)


INTERNALDATE = re.compile(rb'INTERNALDATE "([^"]+)"')
UID_RE = re.compile(rb"UID (\d+)")


class Mailbox:
    def __init__(self, host: str, user: str, password: str, *, port: int = 993,
                 imap_factory=imaplib.IMAP4_SSL):
        self.host, self.user, self.port = host, user, port
        self._password = password.replace(" ", "")
        self._factory = imap_factory
        self.conn: imaplib.IMAP4 | None = None

    # ── connection ──────────────────────────────────────────────────────────────────
    def __enter__(self) -> "Mailbox":
        self.conn = self._factory(self.host, self.port, ssl_context=ssl.create_default_context())
        self.conn.login(self.user, self._password)
        return self

    def __exit__(self, *exc) -> None:
        if self.conn is not None:
            try:
                self.conn.logout()
            except (imaplib.IMAP4.error, OSError):
                pass
            self.conn = None

    def _ok(self, typ: str, data, what: str):
        if typ != "OK":
            raise imaplib.IMAP4.error(f"{what} failed: {data!r}")
        return data

    def select_inbox(self) -> int:
        self._ok(*self.conn.select("INBOX", readonly=True), "SELECT INBOX")
        typ, data = self.conn.response("UIDVALIDITY")
        return int(data[0]) if data and data[0] else 0

    # ── reading ─────────────────────────────────────────────────────────────────────
    def fetch_new(self, cursor: Cursor, *, first_run_window: int = 50) -> list[tuple[int, Message]]:
        """Messages after ``cursor.last_uid``; on a first run (or new UIDVALIDITY) the last
        ``first_run_window`` messages, so the Message-ID ledger decides what is new."""
        uidvalidity = self.select_inbox()
        data = self._ok(*self.conn.uid("SEARCH", None, "ALL"), "UID SEARCH")
        uids = [int(u) for u in data[0].split()] if data and data[0] else []
        if cursor.uidvalidity != uidvalidity:
            cursor.uidvalidity, cursor.last_uid = uidvalidity, 0
            uids = uids[-first_run_window:]
        else:
            uids = [u for u in uids if u > cursor.last_uid]
        out: list[tuple[int, Message]] = []
        for uid in uids:
            data = self._ok(*self.conn.uid("FETCH", str(uid), "(UID INTERNALDATE BODY.PEEK[])"), "UID FETCH")
            for item in data:
                if not isinstance(item, tuple):
                    continue
                meta, raw = item
                when = INTERNALDATE.search(meta)
                internal = _idate(when.group(1)) if when else None
                out.append((uid, parse_message(raw, internal)))
            cursor.last_uid = max(cursor.last_uid, uid)
        return out

    def idle(self, timeout: float = 25 * 60) -> bool:
        """Block until the server reports new mail (True) or ``timeout`` passes (False).

        Python 3.13's imaplib has no IDLE, so this speaks RFC 2177 directly. Gmail drops
        IDLE after about 29 minutes, so callers loop with a shorter timeout.
        """
        conn = self.conn
        if b"IDLE" not in [c.encode() if isinstance(c, str) else c for c in conn.capabilities]:
            raise imaplib.IMAP4.error("server does not support IDLE")
        tag = conn._new_tag()
        conn.send(tag + b" IDLE\r\n")
        line = conn.readline()
        if not line.startswith(b"+"):
            raise imaplib.IMAP4.error(f"IDLE refused: {line!r}")
        sock = conn.sock
        sock.settimeout(timeout)
        got_mail = False
        try:
            while True:
                line = conn.readline()
                if not line:
                    raise imaplib.IMAP4.abort("connection closed during IDLE")
                if line.startswith(b"*") and (b"EXISTS" in line or b"RECENT" in line):
                    got_mail = True
                    break
        except (socket.timeout, TimeoutError, ssl.SSLError):
            pass
        finally:
            sock.settimeout(None)
            conn.send(b"DONE\r\n")
            while True:
                line = conn.readline()
                if not line or line.startswith(tag):
                    break
        return got_mail

    # ── drafts ──────────────────────────────────────────────────────────────────────
    def drafts_folder(self) -> str:
        """Gmail localizes folder names, so look for the ``\\Drafts`` special-use flag."""
        data = self._ok(*self.conn.list(), "LIST")
        for line in data:
            text = line.decode() if isinstance(line, bytes) else str(line)
            if "\\Drafts" in text:
                name = text.rsplit(' "/" ', 1)[-1].strip()
                return name if name.startswith('"') else f'"{name}"'
        return '"[Gmail]/Drafts"'

    def append(self, folder: str, message: EmailMessage, flags: str = "") -> None:
        self._ok(*self.conn.append(folder, flags, imaplib.Time2Internaldate(datetime.now(timezone.utc)),
                                   message.as_bytes()), f"APPEND {folder}")

    def save_draft(self, message: EmailMessage) -> None:
        self.append(self.drafts_folder(), message, r"(\Draft)")


def _idate(text: bytes) -> datetime:
    return datetime.strptime(text.decode().strip(), "%d-%b-%Y %H:%M:%S %z")


def build_email(sender: str, to: str, subject: str, body: str, *, in_reply_to: str | None = None,
                message_id: str | None = None) -> EmailMessage:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, to, subject
    msg["Message-ID"] = message_id or make_msgid(domain=sender.rsplit("@", 1)[-1])
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content(body)
    return msg


class SmtpSender:
    def __init__(self, host: str, port: int, user: str, password: str, smtp_factory=smtplib.SMTP_SSL):
        self.host, self.port, self.user = host, port, user
        self._password = password.replace(" ", "")
        self._factory = smtp_factory

    def send(self, message: EmailMessage) -> None:
        with self._factory(self.host, self.port, context=ssl.create_default_context(), timeout=30) as smtp:
            smtp.login(self.user, self._password)
            smtp.send_message(message)
