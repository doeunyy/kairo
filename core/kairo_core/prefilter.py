"""Cheap checks before any model sees a message: sender allowlist and duplicates."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parseaddr

from .ledger import Ledger


@dataclass(frozen=True)
class Message:
    message_id: str
    sender: str               # raw From header
    subject: str
    received_at: datetime
    body: str = ""
    list_unsubscribe: bool = False


@dataclass(frozen=True)
class Allowlist:
    addresses: frozenset[str] = field(default_factory=frozenset)
    domains: frozenset[str] = field(default_factory=frozenset)

    def permits(self, sender: str) -> bool:
        address = parseaddr(sender)[1].lower()
        if not address or "@" not in address:
            return False
        domain = address.rsplit("@", 1)[1]
        return address in self.addresses or domain in self.domains


@dataclass(frozen=True)
class Verdict:
    passed: bool
    reason: str


def prefilter(message: Message, allowlist: Allowlist, ledger: Ledger) -> Verdict:
    if not message.message_id.strip():
        return Verdict(False, "missing_message_id")
    if ledger.seen(message.message_id):
        return Verdict(False, "duplicate")
    if not allowlist.permits(message.sender):
        return Verdict(False, "sender_not_allowlisted")
    return Verdict(True, "ok")
