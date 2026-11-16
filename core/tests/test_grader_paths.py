"""What a grader touches: several calendars, alerts copied to a Gmail label, Kairo's own sent mail."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from email import message_from_bytes, policy

import pytest

from kairo_core import calendar_ics
from kairo_core import report as report_mod
from kairo_core import runner as runner_mod
from kairo_core.alerts import ALERT_HEADER, AlertMirror
from kairo_core.config import Settings
from kairo_core.discord import APPROVE, approval_sweep, request_approval
from kairo_core.mail import OUTBOX_HEADER, parse_message
from kairo_core.prefilter import prefilter
from kairo_core.ledger import Ledger

from test_adapters import ICS, LA, ME, FakeMailbox, FakeSmtp, _http_429, make_kairo, make_outbox
from test_proposals import GRADER, extraction, mail

PRIMARY = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:grader-1
SUMMARY:Interview with Acme
DTSTART:20261105T200000Z
DTEND:20261105T203000Z
END:VEVENT
BEGIN:VEVENT
UID:lunch
SUMMARY:Lunch with\\, Sam
DTSTART:20261105T190000Z
DTEND:20261105T200000Z
END:VEVENT
END:VCALENDAR
"""


# ── calendars ──────────────────────────────────────────────────────────────────────────

def test_several_calendars_read_as_one_without_duplicates(tmp_path, monkeypatch):
    monkeypatch.setattr(calendar_ics, "_event_log", lambda event, **f: None)
    feeds = {"https://cal.test/kairo.ics": ICS, "https://cal.test/primary.ics": PRIMARY}
    cal = calendar_ics.MultiCalendar(calendar_ics.split_urls("https://cal.test/kairo.ics, https://cal.test/primary.ics"),
                                     tmp_path, fetch=lambda url: feeds[url])
    events = calendar_ics.parse_ics(cal(), "America/Los_Angeles", now=datetime(2026, 11, 1, tzinfo=LA))
    titles = [e.summary for e in events]
    assert "Interview with Acme" in titles and titles.count("Lunch with, Sam") == 1     # same UID in both
    assert (tmp_path / "calendar.ics").exists() and (tmp_path / "calendar-2.ics").exists()


def test_one_calendar_down_still_reads_the_other_and_counts_as_stale(tmp_path, monkeypatch):
    monkeypatch.setattr(calendar_ics, "_event_log", lambda event, **f: None)
    cal = calendar_ics.MultiCalendar(["a", "b"], tmp_path,
                                     fetch=lambda url: PRIMARY if url == "a" else _http_429(url))
    assert "Interview with Acme" in cal() and cal.stale_s == float("inf")
    with pytest.raises(OSError):
        calendar_ics.MultiCalendar(["a"], tmp_path / "x", fetch=_http_429)()


def test_interview_on_the_primary_calendar_alerts(tmp_path, monkeypatch):
    monkeypatch.setattr(calendar_ics, "_event_log", lambda event, **f: None)
    k, _ = make_kairo(tmp_path, ics=ICS + PRIMARY)
    soon = datetime(2026, 11, 5, 11, 40, tzinfo=LA)              # 20 min before 12:00 PT
    assert "Interview in 20 min: Interview with Acme" in report_mod.interview_soon(k, soon)


def test_each_calendar_url_is_redacted():
    s = Settings({"KAIRO_CALENDAR_ICS_URL": "https://cal.test/secret-one.ics,https://cal.test/secret-two.ics"})
    out = s.redact("fetch https://cal.test/secret-two.ics failed")
    assert "secret-two" not in out and "<KAIRO_CALENDAR_ICS_URL>" in out


# ── alerts copied to Gmail ─────────────────────────────────────────────────────────────

class LabelBox(FakeMailbox):
    def __init__(self, messages=(), store=None, fail=False):
        super().__init__(list(messages))
        self.store, self.fail = (store if store is not None else []), fail

    def ensure_folder(self, name):
        if self.fail:
            raise OSError("imap down")
        return f'"{name}"'

    def append(self, folder, message, flags=""):
        self.store.append((folder, message))


def test_alert_copy_goes_to_the_label_not_the_inbox():
    store = []
    mirror = AlertMirror(lambda: LabelBox(store=store), "me@kairo.test")
    assert mirror("📌 **Northwind Labs** · Backend: Applied → Interview\nNext: reply", "applied")
    folder, msg = store[0]
    assert folder == '"Kairo Alerts"' and msg[ALERT_HEADER] == "applied"
    assert msg["Subject"] == "[Kairo] 📌 Northwind Labs · Backend: Applied → Interview"
    assert msg["To"] == "me@kairo.test" and "not sent to anyone" in msg.get_content()


def test_alert_copy_failure_never_blocks():
    assert AlertMirror(lambda: LabelBox(fail=True), "me@kairo.test")("text") is False


@pytest.fixture
def kairo(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_mod, "is_quiet", lambda now: False)

    def build(mails, extractions=None):
        k, discord = make_kairo(tmp_path, mails)
        store = []
        k.mirror.mailbox_factory = lambda: LabelBox(mails, store)
        k.extract = lambda m: (extractions or {})[m.subject]
        return k, discord, store
    return build


def test_runner_alerts_and_cards_are_copied_to_gmail(kairo):
    subject = "Application received: Robotics Software Intern (CR-1042)"
    k, discord, store = kairo([mail(1, "Northwind Labs HR <offers@northwind-labs.test>", "Offer letter"),
                               mail(2, GRADER, subject)],
                              {subject: extraction(type="ack", company="Cardinal Robotics", posting_id="CR-1042")})
    k.sweep()
    subjects = [m["Subject"] for _, m in store]
    assert subjects[0].startswith("[Kairo] ⚠️ Suspicious email")
    assert subjects[1].startswith("[Kairo] 🗂️ Tracker change waiting for your OK")
    assert "Preparing → Applied" in store[1][1].get_content()


def test_quiet_hours_copy_says_discord_is_held(kairo, monkeypatch):
    k, discord, store = kairo([mail(1, "Northwind Labs HR <offers@northwind-labs.test>", "Offer letter")])
    monkeypatch.setattr(runner_mod, "is_quiet", lambda now: True)
    k.sweep()
    assert discord.posts == [] and "Held for the Morning Briefing" in store[0][1].get_content()


def test_interview_soon_job_copies_its_alert(tmp_path, monkeypatch):
    monkeypatch.setattr(calendar_ics, "_event_log", lambda event, **f: None)
    k, _ = make_kairo(tmp_path, ics=ICS + PRIMARY)
    copies = []
    k.mirror = lambda text, kind="": copies.append((kind, text))
    monkeypatch.setattr(runner_mod, "Kairo", lambda settings: k)
    monkeypatch.setattr(report_mod, "interview_soon", lambda kk: "⏰ Interview in 20 min: Interview with Acme")
    monkeypatch.setattr(Settings, "load", classmethod(lambda cls: k.s))
    assert report_mod.main(["interview-soon"]) == 0
    assert copies == [("interview_soon", "⏰ Interview in 20 min: Interview with Acme")]


# ── Kairo's own sent mail ──────────────────────────────────────────────────────────────

def test_sent_mail_is_tagged_and_its_inbox_copy_skipped(tmp_path):
    box, smtp, discord = make_outbox(tmp_path), FakeSmtp(), __import__("test_adapters").FakeDiscord()
    d = box.create_draft("me@kairo.test", "Note to self", "body")
    mid = request_approval(discord, box, d, "chan")
    discord.reactions[(mid, APPROVE)] = [ME]
    approval_sweep(discord, box, smtp)
    sent = smtp.sent[0]
    assert sent[OUTBOX_HEADER] == d.draft_id
    copy = parse_message(sent.as_bytes())               # what arrives when the user is the recipient
    led = Ledger()
    try:
        assert prefilter(copy, led, "me@kairo.test").reason == "sent_by_kairo"
    finally:
        led.close()


def test_trust_domain_cli_fills_every_row_of_a_company(tmp_path, capsys):
    from kairo_core.__main__ import cmd_trust_domain
    k, _ = make_kairo(tmp_path)
    assert cmd_trust_domain(k, "Northwind Labs", "@careers.northwind.test") == 0
    rows = [a for a in k.tracker.applications() if a.company == "Northwind Labs"]
    assert len(rows) == 2 and all(a.email_domains == {"northwind.test", "careers.northwind.test"} for a in rows)
    assert cmd_trust_domain(k, "Northwind Labs", "careers.northwind.test") == 0          # idempotent
    assert "skip" in capsys.readouterr().out
    assert cmd_trust_domain(k, "Northwind Labs", "gmail.com") == 1                       # never a personal domain
