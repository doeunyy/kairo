"""Wires the adapters around the deterministic pipeline.

``sweep`` is one pass: new INBOX mail → prefilter → local extraction → pipeline →
version-aware Notion write → Discord alert. ``watch`` repeats it on IMAP IDLE and runs the
approval sweep between waits. Neither has a path that sends email without approval.
"""

from __future__ import annotations

import fcntl
import json
import logging
import time
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from email.utils import parseaddr
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from . import calendar_ics
from .config import Settings
from .discord import Discord, approval_sweep, request_approval
from .extractor import RouterExtractor
from .ledger import Ledger
from .mail import Cursor, Mailbox, SmtpSender
from .matcher import Application
from .notion import NotionTracker, RevConflict
from .outbox import Outbox, load_or_create_key
from .pipeline import Outcome, process
from .prefilter import Message, prefilter
from .report import URGENT_WITHIN, defer, is_quiet
from .status import MailType

log = logging.getLogger("kairo")
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def fmt_dt(value: datetime) -> str:
    return f"{WEEKDAYS[value.weekday()]} {value.month}/{value.day} {value:%H:%M}"


def suggest_slots(events: list[calendar_ics.Event], now: datetime, tz_name: str, *, count: int = 3,
                  minutes: int = 60, days: int = 7) -> list[tuple[datetime, datetime]]:
    """One slot per weekday, starting tomorrow, so the options are spread out."""
    picks = []
    for offset in range(1, days + 1):
        day = (now + timedelta(days=offset)).date()
        if day.weekday() >= 5:
            continue
        for start, end in calendar_ics.free_slots(events, day, tz_name):
            if end - start >= timedelta(minutes=minutes):
                picks.append((start, start + timedelta(minutes=minutes)))
                break
        if len(picks) == count:
            break
    return picks


@dataclass
class SweepReport:
    fetched: int = 0
    outcomes: dict[str, int] | None = None

    def add(self, kind: str) -> None:
        self.outcomes = self.outcomes or {}
        self.outcomes[kind] = self.outcomes.get(kind, 0) + 1


class Kairo:
    def __init__(self, settings: Settings, *, extractor: Callable[[Message], str | None] | None = None,
                 tracker: NotionTracker | None = None, discord: Discord | None = None,
                 mailbox_factory: Callable[[], Mailbox] | None = None, smtp: SmtpSender | None = None,
                 calendar_text: Callable[[], str] | None = None):
        self.s = settings
        state = settings.state_dir
        self.tz_name = settings.get("KAIRO_TZ", "America/Los_Angeles")
        self.ledger = Ledger(state / "ledger.sqlite3")
        self.cursor_path = state / "mail_cursor.json"
        self.extract = extractor or RouterExtractor(settings.get("KAIRO_ROUTER_URL", "http://127.0.0.1:8787"))
        self.tracker = tracker or NotionTracker(settings.require("KAIRO_NOTION_TOKEN"),
                                                settings.require("KAIRO_NOTION_PARENT_PAGE_ID"), state / "notion.json")
        self.discord = discord or Discord(settings.require("DISCORD_BOT_TOKEN"))
        self.channel = settings.require("DISCORD_HOME_CHANNEL")
        user = settings.require("KAIRO_MAIL_USER")
        password = settings.require("KAIRO_MAIL_APP_PASSWORD")
        self.mailbox_factory = mailbox_factory or (lambda: Mailbox(settings.get("KAIRO_IMAP_HOST", "imap.gmail.com"),
                                                                   user, password))
        self.smtp = smtp or SmtpSender(settings.get("KAIRO_SMTP_HOST", "smtp.gmail.com"),
                                       int(settings.get("KAIRO_SMTP_PORT", "465")), user, password)
        self.outbox = Outbox(state / "outbox.sqlite3", load_or_create_key(state / "secrets" / "approval.key"),
                             sender_address=user, approvers=settings.allowed_discord_users,
                             recipients=settings.recipient_allowlist, audit_dir=state / "logs" / "outbox")
        # The cache is built lazily so commands that never read the calendar don't need its URL.
        self.calendar: calendar_ics.CachedCalendar | None = None
        self.calendar_text = calendar_text or self._cached_calendar_text
        self.mail_log = state / "logs" / "mail"

    def _cached_calendar_text(self) -> str:
        if self.calendar is None:
            self.calendar = calendar_ics.CachedCalendar(self.s.require("KAIRO_CALENDAR_ICS_URL"),
                                                        self.s.state_dir / "calendar.ics")
        return self.calendar()

    # ── mail ────────────────────────────────────────────────────────────────────────
    def sweep(self) -> SweepReport:
        """One pass at a time across processes (IDLE watcher and the 15-minute cron backup)."""
        with open(self.s.state_dir / "sweep.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return self._sweep()

    def _sweep(self) -> SweepReport:
        report = SweepReport()
        cursor = Cursor.load(self.cursor_path)
        first_run = cursor.uidvalidity == 0
        with self.mailbox_factory() as box:
            fetched = box.fetch_new(cursor)
        report.fetched = len(fetched)
        if first_run and self.s.get("KAIRO_PROCESS_BACKLOG") != "1":
            # A fresh deployment (no cursor yet) must not replay old mail into Notion and Discord:
            # record what is already in the inbox as a baseline and act only on mail after it.
            for _, message in fetched:
                if message.message_id:
                    self.ledger.record(message.message_id, "baseline")
                    report.add("baseline")
            cursor.save(self.cursor_path)
            return report
        applications = self.tracker.applications() if fetched else []
        held_uids = []
        for uid, message in fetched:
            outcome = self.handle(message, applications)
            report.add(outcome.kind)
            if outcome.kind == "held":
                held_uids.append(uid)
            elif outcome.update is not None:
                applications = self.tracker.applications()
        if held_uids:
            # Held mail was not recorded; rewind so the next sweep retries it.
            cursor.last_uid = min(held_uids) - 1
        cursor.save(self.cursor_path)
        return report

    def handle(self, message: Message, applications: list[Application]) -> Outcome:
        # Gmail's INTERNALDATE is UTC; dates written to Notion (last contact, applied on) are the
        # user's calendar dates, so convert before the pipeline takes .date().
        message = replace(message, received_at=message.received_at.astimezone(ZoneInfo(self.tz_name)))
        allowlist = self.s.sender_allowlist
        raw = self.extract(message) if prefilter(message, allowlist, self.ledger).passed else None
        outcome = process(message, raw, applications, allowlist, self.ledger)
        before = next((a for a in applications if outcome.update and a.id == outcome.update.application_id), None)
        if outcome.update is not None:
            try:
                self.tracker.apply_update(outcome.update, f"{outcome.extraction.type.value} email: {message.subject[:80]}")
            except RevConflict as exc:
                self.ledger.mark(message.message_id, "rev_conflict")
                outcome = Outcome("rev_conflict", str(exc), outcome.extraction, notify="warn")
        self._log(message, outcome)
        self._index(message, outcome)
        self.notify(message, outcome, before)
        return outcome

    def _index(self, message: Message, outcome: Outcome) -> None:
        if outcome.kind == "dropped":
            return
        ex = outcome.extraction
        self.ledger.index_mail({
            "message_id": message.message_id,
            "received_at": message.received_at.astimezone(timezone.utc).isoformat(timespec="seconds"),
            "sender": parseaddr(message.sender)[1].lower(), "subject": message.subject[:200],
            "type": ex.type.value if ex else None, "company": ex.company if ex else None,
            "role": ex.role if ex else None, "deadline": ex.deadline.isoformat() if ex and ex.deadline else None,
            "outcome": outcome.kind,
            "application_id": outcome.update.application_id if outcome.update else None})

    def _log(self, message: Message, outcome: Outcome) -> None:
        """One line per message: ids and decisions only, never the body."""
        self.mail_log.mkdir(parents=True, exist_ok=True)
        ex = outcome.extraction
        line = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "message_id": message.message_id,
                "outcome": outcome.kind, "reason": outcome.reason, "notify": outcome.notify,
                "type": ex.type.value if ex else None, "confidence": ex.confidence if ex else None,
                "application_id": outcome.update.application_id if outcome.update else None,
                "fields": sorted(outcome.update.fields) if outcome.update else None}
        with open(self.mail_log / f"{datetime.now(timezone.utc):%Y-%m-%d}.jsonl", "a") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")

    # ── alerts ──────────────────────────────────────────────────────────────────────
    def notify(self, message: Message, outcome: Outcome, before: Application | None) -> None:
        text = self.alert_text(message, outcome, before)
        if not text or outcome.notify not in ("now", "warn"):
            return
        now = datetime.now(ZoneInfo(self.tz_name))
        deadline = outcome.extraction.deadline if outcome.extraction else None
        urgent = deadline is not None and deadline - now <= URGENT_WITHIN
        if is_quiet(now) and not urgent:
            # 22:00-07:30: hold for the Morning Briefing, and say why in the log.
            defer(self.s.state_dir, text, f"quiet_hours:{outcome.kind}")
            return
        self.discord.post(self.channel, text)

    def alert_text(self, message: Message, outcome: Outcome, before: Application | None) -> str | None:
        ex = outcome.extraction
        subject = message.subject[:120]
        if outcome.kind in ("applied", "unchanged") and before is not None:
            fields = outcome.update.fields
            name = f"**{before.company}** · {before.role}"
            change = f"{before.status.value} → {fields['status']}" if "status" in fields else before.status.value
            lines = [f"📌 {name}: {change}", f"Email: {subject}"]
            if fields.get("next_action"):
                due = fields.get("due")
                due_text = f" (due {fmt_dt(datetime.fromisoformat(due).astimezone(ZoneInfo(self.tz_name)))})" if due else ""
                lines.append(f"Next: {fields['next_action']}{due_text}")
            if ex and ex.type == MailType.INTERVIEW_REQUEST:
                lines.append(self.slot_text())
            return "\n".join(lines)
        if outcome.kind == "unknown_application":
            return (f"❓ Email about an application Kairo isn't tracking: **{ex.company}** {ex.role or ''}\n"
                    f"Email: {subject}\nShould I add it to Notion as a new application?")
        if outcome.kind == "awaiting_user":
            options = "\n".join(f"{i}. {a.company} · {a.role} ({a.status.value})"
                                for i, a in enumerate(outcome.candidates, 1))
            why = "low extraction confidence" if outcome.reason == "low_extraction_confidence" else "more than one candidate"
            return f"🤔 Which application is this email about? ({why})\nEmail: {subject}\n{options}"
        if outcome.kind == "refused":
            return f"⚠️ Refused a status change ({outcome.reason}). Email: {subject}"
        if outcome.kind == "rejected_extraction":
            return f"⚠️ The email could not be parsed into the expected fields, so nothing was changed. Email: {subject}"
        if outcome.kind == "rev_conflict":
            return f"⚠️ The Notion row changed in the meantime, so I did not overwrite it. Please check it. Email: {subject}"
        return None

    def slot_text(self) -> str:
        try:
            events = calendar_ics.parse_ics(self.calendar_text(), self.tz_name)
        except Exception as exc:          # calendar trouble must not block the alert
            log.warning("calendar unavailable: %s", type(exc).__name__)
            return "Available times: could not read the calendar."
        slots = suggest_slots(events, datetime.now(ZoneInfo(self.tz_name)), self.tz_name)
        if not slots:
            return "Available times: no free slot in the next 7 days."
        return "Available times (from your calendar, PT): " + ", ".join(f"{fmt_dt(a)}–{b:%H:%M}" for a, b in slots)

    # ── outgoing mail ───────────────────────────────────────────────────────────────
    def draft(self, to_addr: str, subject: str, body: str, *, in_reply_to: str | None = None,
              origin: str = "agent") -> str:
        draft = self.outbox.create_draft(to_addr, subject, body, in_reply_to=in_reply_to, origin=origin)
        with self.mailbox_factory() as box:
            from .mail import build_email
            box.save_draft(build_email(self.outbox.sender_address, draft.to_addr, draft.subject, draft.body,
                                       in_reply_to=draft.in_reply_to))
        request_approval(self.discord, self.outbox, draft, self.channel)
        return draft.draft_id

    def approvals(self):
        return approval_sweep(self.discord, self.outbox, self.smtp)

    # ── long-running ────────────────────────────────────────────────────────────────
    def watch(self, idle_seconds: float = 60, sweep_every: float = 15 * 60, stop: Callable[[], bool] = lambda: False):
        """IDLE in short waits so approvals stay responsive; a full sweep at least every 15 minutes."""
        last_sweep = float("-inf")       # sweep at once on start: mail may have arrived while down
        while not stop():
            try:
                if time.monotonic() - last_sweep > sweep_every:
                    log.info("sweep %s", self.sweep())
                    last_sweep = time.monotonic()
                with self.mailbox_factory() as box:
                    box.select_inbox()
                    got_mail = box.idle(idle_seconds)
                if got_mail:
                    log.info("sweep %s", self.sweep())
                    last_sweep = time.monotonic()
                for result in self.approvals():
                    if result.action != "waiting":
                        log.info("approval %s", result)
            except Exception as exc:       # reconnect after network trouble; never crash the loop
                log.warning("watch error: %s", self.s.redact(f"{type(exc).__name__}: {exc}"))
                time.sleep(10)
