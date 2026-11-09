"""Processed-message ledger: one row per Message-ID, so restarts and IMAP IDLE
reconnects never apply the same email twice.

Lives on local disk (SQLite is unsafe on Cloud Storage FUSE) and is included in
the periodic state snapshot.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS processed (
    message_id     TEXT PRIMARY KEY,
    processed_at   TEXT NOT NULL,
    outcome        TEXT NOT NULL,
    application_id TEXT
)
"""
# What chat tools may show about processed mail: headers and the validated extraction,
# never the body. Only mail that passed the sender allowlist is indexed.
MAIL_INDEX = """
CREATE TABLE IF NOT EXISTS mail_index (
    message_id     TEXT PRIMARY KEY,
    received_at    TEXT NOT NULL,
    sender         TEXT NOT NULL,
    subject        TEXT NOT NULL,
    type           TEXT,
    company        TEXT,
    role           TEXT,
    deadline       TEXT,
    outcome        TEXT NOT NULL,
    application_id TEXT
)
"""


class Ledger:
    def __init__(self, path: Path | str = ":memory:"):
        # check_same_thread=False: the MCP server calls tools from worker threads, one at a time.
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.execute(SCHEMA)
        self.db.execute(MAIL_INDEX)
        self.db.commit()

    def seen(self, message_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM processed WHERE message_id = ?", (message_id,)).fetchone() is not None

    def record(self, message_id: str, outcome: str, application_id: str | None = None) -> bool:
        """Insert once. Returns False when another run already recorded this message."""
        cur = self.db.execute(
            "INSERT OR IGNORE INTO processed (message_id, processed_at, outcome, application_id) VALUES (?, ?, ?, ?)",
            (message_id, datetime.now(timezone.utc).isoformat(timespec="seconds"), outcome, application_id))
        self.db.commit()
        return cur.rowcount == 1

    def mark(self, message_id: str, outcome: str) -> None:
        """Correct the outcome after a later step failed (for example a tracker conflict)."""
        self.db.execute("UPDATE processed SET outcome = ? WHERE message_id = ?", (outcome, message_id))
        self.db.commit()

    def index_mail(self, row: dict) -> None:
        cols = ("message_id", "received_at", "sender", "subject", "type", "company", "role", "deadline",
                "outcome", "application_id")
        self.db.execute(f"INSERT OR REPLACE INTO mail_index ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                        tuple(row.get(c) for c in cols))
        self.db.commit()

    def recent_mail(self, since_iso: str, limit: int = 50) -> list[dict]:
        self.db.row_factory = sqlite3.Row
        try:
            rows = self.db.execute("SELECT * FROM mail_index WHERE received_at >= ? ORDER BY received_at DESC LIMIT ?",
                                   (since_iso, limit)).fetchall()
        finally:
            self.db.row_factory = None
        return [dict(r) for r in rows]

    def close(self) -> None:
        self.db.close()
