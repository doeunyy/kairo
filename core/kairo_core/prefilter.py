"""Cheap checks before anything else: duplicates and Kairo's own mail. (``Allowlist`` is the
recipient allowlist for outgoing mail; incoming senders are judged by ``triage.trust``.)"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parseaddr

from .ledger import Ledger
from .mailauth import AuthResult


@dataclass(frozen=True)
class Message:
    message_id: str
    sender: str               # raw From header
    subject: str
    received_at: datetime
    body: str = ""
    list_unsubscribe: bool = False
    auth: AuthResult | None = None    # Gmail's topmost Authentication-Results, if Gmail wrote it
    reply_to: str = ""
    kairo_sent: bool = False          # carries X-Kairo-Outbox (only honoured when From is our own address)

    @property
    def address(self) -> str:
        return parseaddr(self.sender)[1].lower()

    @property
    def domain(self) -> str:
        address = self.address
        return address.rsplit("@", 1)[1] if "@" in address else ""

    @property
    def display_name(self) -> str:
        return parseaddr(self.sender)[0]


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


def prefilter(message: Message, ledger: Ledger, own_address: str = "") -> Verdict:
    if not message.message_id.strip():
        return Verdict(False, "missing_message_id")
    if ledger.seen(message.message_id):
        return Verdict(False, "duplicate")
    if message.kairo_sent and own_address and message.address == own_address.lower():
        # A copy of mail Kairo itself sent after approval; reading it back would loop.
        return Verdict(False, "sent_by_kairo")
    return Verdict(True, "ok")
