"""Human-in-the-loop gate for outgoing email.

The model can only call ``create_draft``. Sending needs an approval token that ``approve``
issues when an allowlisted Discord user approves the exact draft they were shown (same
content hash). ``send`` checks the token, uses it once, re-checks the recipient allowlist,
and writes an audit line. Drafts are immutable: an edit is a new draft.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .mail import OUTBOX_HEADER, build_email
from .prefilter import Allowlist

TOKEN_TTL_S = 15 * 60
SCHEMA = """
CREATE TABLE IF NOT EXISTS drafts (
    draft_id        TEXT PRIMARY KEY,
    to_addr         TEXT NOT NULL,
    subject         TEXT NOT NULL,
    body            TEXT NOT NULL,
    in_reply_to     TEXT,
    body_hash       TEXT NOT NULL,
    origin          TEXT NOT NULL,
    status          TEXT NOT NULL,      -- pending | approved | sending | sent | send_failed | discarded | blocked
    created_at      TEXT NOT NULL,
    preview_channel TEXT,
    preview_message TEXT,
    approved_by     TEXT,
    approved_at     TEXT,
    sent_at         TEXT,
    sent_message_id TEXT
)
"""


class ApprovalError(RuntimeError):
    pass


@dataclass(frozen=True)
class Draft:
    draft_id: str
    to_addr: str
    subject: str
    body: str
    in_reply_to: str | None
    body_hash: str
    origin: str
    status: str
    preview_channel: str | None = None
    preview_message: str | None = None


def content_hash(to_addr: str, subject: str, body: str, in_reply_to: str | None) -> str:
    canonical = json.dumps([to_addr, subject, body, in_reply_to or ""], ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def load_or_create_key(path: Path) -> bytes:
    """32 random bytes in a 0600 file. In the cloud the file comes from Secret Manager."""
    if path.exists():
        key = path.read_bytes()
        if len(key) < 32:
            raise ApprovalError("approval key file is too short")
        return key
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = secrets.token_bytes(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(key)
    return key


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Outbox:
    def __init__(self, db_path: Path | str, key: bytes, *, sender_address: str, approvers: frozenset[str],
                 recipients: Allowlist, audit_dir: Path | None = None, clock: Callable[[], float] = time.time):
        # check_same_thread=False: the MCP server calls tools from worker threads, one at a time.
        self.db = sqlite3.connect(str(db_path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute(SCHEMA)
        self.db.commit()
        self._key = key
        self.sender_address = sender_address
        self.approvers, self.recipients = approvers, recipients
        self.audit_dir, self.clock = audit_dir, clock

    # ── model-facing ────────────────────────────────────────────────────────────────
    def create_draft(self, to_addr: str, subject: str, body: str, *, in_reply_to: str | None = None,
                     origin: str = "agent") -> Draft:
        to_addr = to_addr.strip()
        if not self.recipients.permits(to_addr):
            self._audit("draft_refused", None, to_addr, reason="recipient_not_allowlisted", origin=origin)
            raise ApprovalError("recipient is not on the allowlist")
        draft_id = secrets.token_hex(6)
        digest = content_hash(to_addr, subject, body, in_reply_to)
        self.db.execute(
            "INSERT INTO drafts (draft_id, to_addr, subject, body, in_reply_to, body_hash, origin, status, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
            (draft_id, to_addr, subject, body, in_reply_to, digest, origin, _now()))
        self.db.commit()
        self._audit("draft_created", draft_id, to_addr, origin=origin)
        return self.get(draft_id)

    # ── approval path (code only) ───────────────────────────────────────────────────
    def get(self, draft_id: str) -> Draft:
        row = self.db.execute("SELECT * FROM drafts WHERE draft_id = ?", (draft_id,)).fetchone()
        if row is None:
            raise ApprovalError(f"unknown draft {draft_id}")
        return Draft(**{k: row[k] for k in Draft.__dataclass_fields__})

    def awaiting_approval(self) -> list[Draft]:
        rows = self.db.execute("SELECT draft_id FROM drafts WHERE status = 'pending' "
                               "AND preview_message IS NOT NULL ORDER BY created_at").fetchall()
        return [self.get(r["draft_id"]) for r in rows]

    def set_preview(self, draft_id: str, channel_id: str, message_id: str) -> None:
        self.db.execute("UPDATE drafts SET preview_channel = ?, preview_message = ? WHERE draft_id = ?",
                        (channel_id, message_id, draft_id))
        self.db.commit()

    def _sign(self, draft_id: str, digest: str, approver: str, expires: int) -> str:
        msg = f"{draft_id}|{digest}|{approver}|{expires}".encode()
        return hmac.new(self._key, msg, hashlib.sha256).hexdigest()

    def approve(self, draft_id: str, approver: str, shown_hash: str) -> str:
        draft = self.get(draft_id)
        if approver not in self.approvers:
            self._audit("approve_refused", draft_id, draft.to_addr, approver=approver, reason="not_an_approver")
            raise ApprovalError("approver is not allowlisted")
        if draft.status != "pending":
            raise ApprovalError(f"draft is {draft.status}")
        if not hmac.compare_digest(draft.body_hash, shown_hash):
            self._audit("approve_refused", draft_id, draft.to_addr, approver=approver, reason="hash_mismatch")
            raise ApprovalError("draft changed after it was shown")
        expires = int(self.clock()) + TOKEN_TTL_S
        cur = self.db.execute("UPDATE drafts SET status = 'approved', approved_by = ?, approved_at = ? "
                              "WHERE draft_id = ? AND status = 'pending'", (approver, _now(), draft_id))
        self.db.commit()
        if cur.rowcount != 1:
            raise ApprovalError("draft was approved or discarded concurrently")
        self._audit("approved", draft_id, draft.to_addr, approver=approver)
        return f"{draft_id}.{approver}.{expires}.{self._sign(draft_id, draft.body_hash, approver, expires)}"

    def discard(self, draft_id: str, by: str) -> None:
        draft = self.get(draft_id)
        if by not in self.approvers:
            raise ApprovalError("not an approver")
        self.db.execute("UPDATE drafts SET status = 'discarded' WHERE draft_id = ? AND status IN "
                        "('pending', 'approved')", (draft_id,))
        self.db.commit()
        self._audit("discarded", draft_id, draft.to_addr, approver=by)

    def block(self, draft_id: str, reason: str) -> None:
        """Stop a pending draft whose approval failed a check; the user must draft again."""
        draft = self.get(draft_id)
        self.db.execute("UPDATE drafts SET status = 'blocked' WHERE draft_id = ? AND status = 'pending'", (draft_id,))
        self.db.commit()
        self._audit("blocked", draft_id, draft.to_addr, reason=reason)

    def send(self, draft_id: str, token: str, smtp) -> str:
        draft = self.get(draft_id)
        try:
            tok_draft, approver, expires_s, signature = token.split(".")
            expires = int(expires_s)
        except ValueError:
            raise ApprovalError("malformed token") from None
        expected = self._sign(draft_id, draft.body_hash, approver, expires)
        if tok_draft != draft_id or not hmac.compare_digest(signature, expected):
            self._audit("send_refused", draft_id, draft.to_addr, reason="bad_token")
            raise ApprovalError("token does not match this draft")
        if self.clock() > expires:
            self._audit("send_refused", draft_id, draft.to_addr, reason="token_expired")
            raise ApprovalError("token expired")
        if approver not in self.approvers or draft.status != "approved":
            raise ApprovalError(f"draft is {draft.status}")
        if not self.recipients.permits(draft.to_addr):
            self._audit("send_refused", draft_id, draft.to_addr, reason="recipient_not_allowlisted")
            raise ApprovalError("recipient is not on the allowlist")
        # Claim the draft before talking to SMTP so a token can never send twice.
        cur = self.db.execute("UPDATE drafts SET status = 'sending' WHERE draft_id = ? AND status = 'approved'",
                              (draft_id,))
        self.db.commit()
        if cur.rowcount != 1:
            raise ApprovalError("token already used")
        # The header lets the watcher skip the copy that comes back when the user is the recipient.
        message = build_email(self.sender_address, draft.to_addr, draft.subject, draft.body,
                              in_reply_to=draft.in_reply_to, headers={OUTBOX_HEADER: draft_id})
        try:
            smtp.send(message)
        except Exception as exc:
            self.db.execute("UPDATE drafts SET status = 'send_failed' WHERE draft_id = ?", (draft_id,))
            self.db.commit()
            self._audit("send_failed", draft_id, draft.to_addr, approver=approver, reason=type(exc).__name__)
            raise
        self.db.execute("UPDATE drafts SET status = 'sent', sent_at = ?, sent_message_id = ? WHERE draft_id = ?",
                        (_now(), str(message["Message-ID"]), draft_id))
        self.db.commit()
        self._audit("sent", draft_id, draft.to_addr, approver=approver)
        return str(message["Message-ID"])

    # ── audit ───────────────────────────────────────────────────────────────────────
    def _audit(self, event: str, draft_id: str | None, to_addr: str, **extra) -> None:
        """Who, when, to whom. Never the body."""
        if self.audit_dir is None:
            return
        self.audit_dir.mkdir(parents=True, exist_ok=True)
        line = {"ts": _now(), "event": event, "draft_id": draft_id, "to": to_addr, **extra}
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with open(self.audit_dir / f"{day}.jsonl", "a") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")

    def close(self) -> None:
        self.db.close()
