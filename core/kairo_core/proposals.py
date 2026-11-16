"""Tracker changes that wait for the user: a card in Discord, applied only after ✅.

Mail from a sender Kairo cannot fully trust (a new domain, a personal address), mail about an
application the tracker doesn't have, and any move to Offer or Rejected become a proposal
instead of a write. ``proposal_sweep`` reads the reactions:

- ✅ from an allowed Discord user, on a card whose content hash matches what was stored,
  applies the plan with the same version check as automatic updates. If the sender's domain
  can belong to a company (not gmail.com, a school or an ATS), it is added to that row's
  Email Domains, so the company's next email is trusted.
- ❌ leaves the tracker as it is.
- After 7 days a proposal expires.

A card is posted when the proposal is made, except in quiet hours (22:00-07:30), when it waits
for the first sweep after 07:30. Nothing here sends email.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .discord import Discord, _quote
from .notion import RevConflict
from .pipeline import Update
from .status import Status
from .triage import learnable

ACCEPT, REJECT = "✅", "❌"
TTL = timedelta(days=7)
DIGEST_FOOTER = re.compile(r"sha256:([0-9a-f]{64})")
SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
    proposal_id     TEXT PRIMARY KEY,
    message_id      TEXT NOT NULL,
    kind            TEXT NOT NULL,      -- update | add
    payload         TEXT NOT NULL,      -- JSON plan
    digest          TEXT NOT NULL,
    sender_domain   TEXT,
    status          TEXT NOT NULL,      -- pending | applied | rejected | expired | conflict
    created_at      TEXT NOT NULL,
    channel         TEXT,
    discord_message TEXT,
    decided_by      TEXT,
    decided_at      TEXT,
    detail          TEXT
)
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Proposal:
    proposal_id: str
    message_id: str
    kind: str
    payload: dict[str, Any]
    digest: str
    sender_domain: str | None
    status: str
    created_at: str
    channel: str | None = None
    discord_message: str | None = None


class Proposals:
    def __init__(self, db_path: Path | str = ":memory:"):
        self.db = sqlite3.connect(str(db_path), check_same_thread=False)
        self.db.execute(SCHEMA)
        self.db.commit()

    def create(self, kind: str, payload: dict[str, Any], *, message_id: str, sender_domain: str | None) -> Proposal:
        body = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        digest = hashlib.sha256(f"{kind}\n{body}".encode()).hexdigest()
        pid = "p-" + secrets.token_hex(4)
        self.db.execute("INSERT INTO proposals (proposal_id, message_id, kind, payload, digest, sender_domain, status, "
                        "created_at) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
                        (pid, message_id, kind, body, digest, sender_domain, _now().isoformat(timespec="seconds")))
        self.db.commit()
        return self.get(pid)

    def get(self, proposal_id: str) -> Proposal:
        row = self.db.execute("SELECT proposal_id, message_id, kind, payload, digest, sender_domain, status, created_at, "
                              "channel, discord_message FROM proposals WHERE proposal_id = ?", (proposal_id,)).fetchone()
        if row is None:
            raise KeyError(proposal_id)
        return Proposal(row[0], row[1], row[2], json.loads(row[3]), *row[4:])

    def pending(self) -> list[Proposal]:
        ids = self.db.execute("SELECT proposal_id FROM proposals WHERE status = 'pending' ORDER BY created_at").fetchall()
        return [self.get(i) for (i,) in ids]

    def set_card(self, proposal_id: str, channel: str, message_id: str) -> None:
        self.db.execute("UPDATE proposals SET channel = ?, discord_message = ? WHERE proposal_id = ?",
                        (channel, message_id, proposal_id))
        self.db.commit()

    def decide(self, proposal_id: str, status: str, by: str | None = None, detail: str = "") -> None:
        self.db.execute("UPDATE proposals SET status = ?, decided_by = ?, decided_at = ?, detail = ? "
                        "WHERE proposal_id = ? AND status = 'pending'",
                        (status, by, _now().isoformat(timespec="seconds"), detail[:500], proposal_id))
        self.db.commit()

    def decided_on(self, day, tz) -> list[dict]:
        """Proposals the user approved or rejected on a local calendar day (for the end-of-day recap)."""
        rows = self.db.execute("SELECT proposal_id, status, decided_at, detail FROM proposals "
                               "WHERE decided_at IS NOT NULL AND status IN ('applied', 'rejected')").fetchall()
        return [{"proposal_id": r[0], "status": r[1], "detail": r[3] or ""} for r in rows
                if datetime.fromisoformat(r[2]).astimezone(tz).date() == day]

    def close(self) -> None:
        self.db.close()


# ── Discord card ───────────────────────────────────────────────────────────────────────

def _describe(p: Proposal) -> tuple[str, str]:
    d = p.payload
    if p.kind == "add":
        row = d["row"]
        what = f"Add **{_quote(row['company'], 80)}** · {_quote(row['role'], 80)} as {row['status']}"
        extra = [f"Next: {_quote(row['next_action'], 120)}" if row.get("next_action") else "",
                 f"Due: {row['due']}" if row.get("due") else ""]
    else:
        f = d["fields"]
        change = f"{d['from_status']} → {f['status']}" if "status" in f else f"{d['from_status']} (dates only)"
        what = f"Update **{_quote(d['company'], 80)}** · {_quote(d['role'], 80)}: {change}"
        extra = [f"Next: {_quote(f['next_action'], 120)}" if f.get("next_action") else "",
                 f"Due: {f['due']}" if f.get("due") else ("Due: cleared" if "due" in f else "")]
    return what, "\n".join(x for x in extra if x)


def proposal_embed(p: Proposal) -> dict[str, Any]:
    what, extra = _describe(p)
    d = p.payload
    return {
        "title": "Tracker change needs your OK",
        "description": f"{what}\n{extra}".strip(),
        "fields": [{"name": "Email", "value": _quote(d.get("subject") or "(no subject)", 200), "inline": False},
                   {"name": "From", "value": _quote(d.get("sender") or "?", 120), "inline": True},
                   {"name": "Why it asks", "value": _quote(d.get("why") or "", 120), "inline": True}],
        "footer": {"text": f"{ACCEPT} apply · {REJECT} leave unchanged · sha256:{p.digest}"},
    }


def post_card(discord: Discord, store: Proposals, p: Proposal, channel: str) -> str:
    message = discord.post(channel, embeds=[proposal_embed(p)])
    store.set_card(p.proposal_id, channel, message["id"])
    for emoji in (ACCEPT, REJECT):
        discord.react(channel, message["id"], emoji)
    return message["id"]


# ── decisions ──────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ReviewResult:
    proposal_id: str
    action: str          # applied | rejected | expired | conflict | refused | waiting | posted
    detail: str = ""


def _domains_with(existing: list[str], domain: str | None, own_address: str) -> list[str] | None:
    if not domain or not learnable(domain, own_address) or domain in existing:
        return None
    return sorted({*existing, domain})


def apply_proposal(p: Proposal, tracker, approver: str, own_address: str) -> str:
    """Write the plan. Returns a short description; raises RevConflict if the row moved."""
    d = p.payload
    if p.kind == "add":
        row = dict(d["row"])
        domains = _domains_with([], p.sender_domain, own_address)
        if domains:
            row["email_domains"] = domains
        company, role, status = row.pop("company"), row.pop("role"), Status(row.pop("status"))
        row["change_reason"] = f"Added after approval in Discord ({p.proposal_id})"
        app = tracker.create_application(company, role, status, **row)
        return f"added {app.company} · {app.role}" + (f"; trusted {p.sender_domain}" if domains else "")
    fields = dict(d["fields"])
    domains = _domains_with(d.get("email_domains") or [], p.sender_domain, own_address)
    if domains:
        fields["email_domains"] = domains
    update = Update(d["application_id"], d["expected_rev"], fields, d.get("expected_edited_at"))
    tracker.apply_update(update, f"Approved in Discord ({p.proposal_id}): {d.get('subject', '')[:60]}")
    return f"updated {d['company']} · {d['role']}" + (f"; trusted {p.sender_domain}" if domains else "")


def proposal_sweep(discord: Discord, store: Proposals, tracker, approvers: frozenset[str], channel: str, *,
                   own_address: str = "", quiet: bool = False) -> list[ReviewResult]:
    results = []
    for p in store.pending():
        if datetime.fromisoformat(p.created_at) + TTL < _now():
            store.decide(p.proposal_id, "expired")
            results.append(ReviewResult(p.proposal_id, "expired"))
            continue
        if not p.discord_message:
            if not quiet:
                post_card(discord, store, p, channel)
                results.append(ReviewResult(p.proposal_id, "posted"))
            continue
        rejecters = [u for u in discord.reaction_users(p.channel, p.discord_message, REJECT) if u in approvers]
        accepters = [u for u in discord.reaction_users(p.channel, p.discord_message, ACCEPT) if u in approvers]
        if rejecters:
            store.decide(p.proposal_id, "rejected", rejecters[0])
            discord.post(p.channel, f"{REJECT} Left the tracker unchanged (`{p.proposal_id}`).")
            results.append(ReviewResult(p.proposal_id, "rejected"))
            continue
        if not accepters:
            results.append(ReviewResult(p.proposal_id, "waiting"))
            continue
        footer = (discord.get_message(p.channel, p.discord_message).get("embeds") or [{}])[0] \
            .get("footer", {}).get("text", "")
        shown = DIGEST_FOOTER.search(footer)
        if not shown or shown.group(1) != p.digest:
            store.decide(p.proposal_id, "refused", accepters[0], "card content differs from the stored plan")
            discord.post(p.channel, f"⚠️ Did not apply `{p.proposal_id}`: the card no longer matches the plan.")
            results.append(ReviewResult(p.proposal_id, "refused"))
            continue
        try:
            done = apply_proposal(p, tracker, accepters[0], own_address)
        except RevConflict as exc:
            store.decide(p.proposal_id, "conflict", accepters[0], str(exc))
            discord.post(p.channel, f"⚠️ The Notion row changed since this card was made, so `{p.proposal_id}` "
                                    "was not applied. Please check the row.")
            results.append(ReviewResult(p.proposal_id, "conflict", str(exc)))
            continue
        store.decide(p.proposal_id, "applied", accepters[0], done)
        discord.post(p.channel, f"{ACCEPT} Applied `{p.proposal_id}`: {done}.")
        results.append(ReviewResult(p.proposal_id, "applied", done))
    return results
