"""Adapters against in-memory fakes: no network, no real accounts."""

from __future__ import annotations

import json
import re
import uuid
from datetime import date, datetime, time, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from kairo_core import calendar_ics, synthetic
from kairo_core.config import Settings, parse_allowlist, read_env_file
from kairo_core.discord import APPROVE, DISCARD, Discord, approval_sweep, request_approval
from kairo_core.extractor import RouterExtractor, build_request
from kairo_core.jsonhttp import RawResponse
from kairo_core.mail import parse_message
from kairo_core.notion import PROPS, NotionTracker, RevConflict
from kairo_core.outbox import ApprovalError, Outbox
from kairo_core.pipeline import Update
from kairo_core.runner import Kairo, suggest_slots
from kairo_core.status import Status

LA = ZoneInfo("America/Los_Angeles")
ME, STRANGER = "1001", "2002"


# ── config ─────────────────────────────────────────────────────────────────────────────

def test_env_file_strips_comments_and_quotes(tmp_path):
    f = tmp_path / "kairo.env"
    f.write_text('A_ONE=abc   # note\nA_TWO="x # y"\nA_THREE=\n# A_FOUR=skip\n')
    assert read_env_file(f) == {"A_ONE": "abc", "A_TWO": "x # y", "A_THREE": ""}


def test_allowlist_parsing():
    allow = parse_allowlist("Recruiting@Cardinal-Robotics.test, @northwind.test,")
    assert allow.permits("R <recruiting@cardinal-robotics.test>")
    assert allow.permits("talent@northwind.test")
    assert not allow.permits("other@cardinal-robotics.test")


def test_redact_hides_secret_values():
    s = Settings({"DISCORD_BOT_TOKEN": "abc.def.ghi", "KAIRO_NOTION_TOKEN": "ntn_secret"})
    assert s.redact("auth abc.def.ghi and ntn_secret") == "auth <DISCORD_BOT_TOKEN> and <KAIRO_NOTION_TOKEN>"


# ── mail parsing ───────────────────────────────────────────────────────────────────────

def test_parse_message_prefers_plain_and_reads_headers():
    m = EmailMessage()
    m["From"], m["Subject"], m["Message-ID"] = "A <a@x.test>", "Hello", "<id-1@x.test>"
    m["Date"], m["List-Unsubscribe"] = "Wed, 04 Nov 2026 10:00:00 -0800", "<https://x.test/u>"
    m.set_content("plain body")
    m.add_alternative("<p>html <b>body</b></p>", subtype="html")
    msg = parse_message(m.as_bytes())
    assert (msg.message_id, msg.subject, msg.body, msg.list_unsubscribe) == ("<id-1@x.test>", "Hello", "plain body", True)
    assert msg.received_at == datetime(2026, 11, 4, 18, 0, tzinfo=timezone.utc)


def test_parse_message_html_only_drops_scripts():
    m = EmailMessage()
    m["Message-ID"] = "<id-2@x.test>"
    m.set_content("<html><script>evil()</script><p>Interview&nbsp;on Monday</p></html>", subtype="html")
    assert parse_message(m.as_bytes()).body == "Interview\xa0on Monday"


# ── outbox ─────────────────────────────────────────────────────────────────────────────

class FakeSmtp:
    def __init__(self, fail: bool = False):
        self.sent, self.fail = [], fail

    def send(self, message):
        if self.fail:
            raise ConnectionError("smtp down")
        self.sent.append(message)


def make_outbox(tmp_path: Path, clock=lambda: 1_000_000.0) -> Outbox:
    return Outbox(tmp_path / "outbox.sqlite3", b"k" * 32, sender_address="me@kairo.test", approvers=frozenset({ME}),
                  recipients=parse_allowlist("me@kairo.test,@northwind.test"), audit_dir=tmp_path / "audit",
                  clock=clock)


def test_outbox_happy_path_sends_once_and_audits(tmp_path):
    box, smtp = make_outbox(tmp_path), FakeSmtp()
    d = box.create_draft("talent@northwind.test", "Re: Interview", "Tuesday 10am works.", in_reply_to="<m1@x>")
    token = box.approve(d.draft_id, ME, d.body_hash)
    box.send(d.draft_id, token, smtp)
    assert smtp.sent[0]["In-Reply-To"] == "<m1@x>" and box.get(d.draft_id).status == "sent"
    with pytest.raises(ApprovalError):
        box.send(d.draft_id, token, smtp)            # single use
    audit = (tmp_path / "audit").glob("*.jsonl").__next__().read_text()
    assert "Tuesday" not in audit and '"event": "sent"' in audit


def test_outbox_refuses_recipient_outside_allowlist(tmp_path):
    with pytest.raises(ApprovalError):
        make_outbox(tmp_path).create_draft("archive@totally-real-jobs.test", "notes", "x")


@pytest.mark.parametrize("approver,shown", [(STRANGER, None), (ME, "0" * 64)])
def test_outbox_approval_needs_allowlisted_user_and_same_hash(tmp_path, approver, shown):
    box = make_outbox(tmp_path)
    d = box.create_draft("me@kairo.test", "s", "b")
    with pytest.raises(ApprovalError):
        box.approve(d.draft_id, approver, shown or d.body_hash)
    assert box.get(d.draft_id).status == "pending"


def test_outbox_rejects_forged_or_expired_token(tmp_path):
    now = [1_000_000.0]
    box, smtp = make_outbox(tmp_path, clock=lambda: now[0]), FakeSmtp()
    d = box.create_draft("me@kairo.test", "s", "b")
    token = box.approve(d.draft_id, ME, d.body_hash)
    forged = token[:-4] + "0000"
    with pytest.raises(ApprovalError, match="does not match"):
        box.send(d.draft_id, forged, smtp)
    now[0] += 16 * 60
    with pytest.raises(ApprovalError, match="expired"):
        box.send(d.draft_id, token, smtp)
    assert smtp.sent == []


def test_outbox_marks_smtp_failure(tmp_path):
    box = make_outbox(tmp_path)
    d = box.create_draft("me@kairo.test", "s", "b")
    with pytest.raises(ConnectionError):
        box.send(d.draft_id, box.approve(d.draft_id, ME, d.body_hash), FakeSmtp(fail=True))
    assert box.get(d.draft_id).status == "send_failed"


# ── discord approval ───────────────────────────────────────────────────────────────────

class FakeDiscord(Discord):
    def __init__(self):
        self.posts, self.messages, self.reactions = [], {}, {}

    def post(self, channel_id, content="", embeds=None):
        mid = str(len(self.posts) + 1)
        msg = {"id": mid, "content": content, "embeds": embeds or []}
        self.posts.append(msg)
        self.messages[mid] = msg
        return msg

    def get_message(self, channel_id, message_id):
        return self.messages[message_id]

    def react(self, channel_id, message_id, emoji):
        pass

    def reaction_users(self, channel_id, message_id, emoji):
        return self.reactions.get((message_id, emoji), [])


def test_approval_sweep_ignores_strangers_then_sends_on_owner_reaction(tmp_path):
    box, smtp, discord = make_outbox(tmp_path), FakeSmtp(), FakeDiscord()
    d = box.create_draft("me@kairo.test", "s", "body")
    mid = request_approval(discord, box, d, "chan")
    discord.reactions[(mid, APPROVE)] = [STRANGER]
    assert [r.action for r in approval_sweep(discord, box, smtp)] == ["waiting"]
    discord.reactions[(mid, APPROVE)] = [STRANGER, ME]
    assert [r.action for r in approval_sweep(discord, box, smtp)] == ["sent"]
    assert len(smtp.sent) == 1 and approval_sweep(discord, box, smtp) == []


def test_approval_sweep_blocks_when_preview_hash_differs(tmp_path):
    box, smtp, discord = make_outbox(tmp_path), FakeSmtp(), FakeDiscord()
    d = box.create_draft("me@kairo.test", "s", "body")
    mid = request_approval(discord, box, d, "chan")
    footer = discord.messages[mid]["embeds"][0]["footer"]
    footer["text"] = re.sub(r"sha256:\w+", "sha256:" + "f" * 64, footer["text"])
    discord.reactions[(mid, APPROVE)] = [ME]
    assert [r.action for r in approval_sweep(discord, box, smtp)] == ["refused"]
    assert box.get(d.draft_id).status == "blocked" and smtp.sent == []


def test_approval_sweep_discard(tmp_path):
    box, discord = make_outbox(tmp_path), FakeDiscord()
    d = box.create_draft("me@kairo.test", "s", "body")
    mid = request_approval(discord, box, d, "chan")
    discord.reactions[(mid, DISCARD)] = [ME]
    assert [r.action for r in approval_sweep(discord, box, FakeSmtp())] == ["discarded"]


# ── notion ─────────────────────────────────────────────────────────────────────────────

class FakeNotion:
    """Just enough of the Notion API, storing properties in the shape Notion returns."""

    def __init__(self, parent: str = "p" * 32):
        self.parent, self.databases, self.pages, self.children = parent, {}, {}, []
        self.minute = 0

    def _read(self, props: dict) -> dict:
        out = {}
        for name, value in props.items():
            (kind, inner), = value.items()
            if kind in ("title", "rich_text"):
                inner = [{**p, "plain_text": p["text"]["content"]} for p in inner]
            out[name] = {"type": kind, kind: inner}
        return out

    def _touch(self, page: dict) -> dict:
        self.minute += 1
        page["last_edited_time"] = f"2026-11-04T10:{self.minute:02d}:00.000Z"
        return page

    def __call__(self, req, timeout) -> RawResponse:
        path = req.full_url.split("/v1", 1)[1]
        body = json.loads(req.data) if req.data else None
        m, ok = req.get_method(), lambda obj: RawResponse(200, {}, json.dumps(obj).encode())
        if m == "GET" and path.startswith(f"/blocks/{self.parent}/children"):
            return ok({"results": self.children, "has_more": False})
        if m == "POST" and path == "/databases":
            db_id = uuid.uuid4().hex
            self.databases[db_id] = body["properties"]
            self.children.append({"id": db_id, "type": "child_database",
                                  "child_database": {"title": body["title"][0]["text"]["content"]}})
            return ok({"id": db_id})
        if path.startswith("/databases/") and path.endswith("/query"):
            db_id = path.split("/")[2]
            return ok({"results": [p for p in self.pages.values() if p["db"] == db_id], "has_more": False})
        if path.startswith("/databases/"):
            db_id = path.split("/")[2]
            if db_id not in self.databases:
                return RawResponse(404, {}, b'{"code":"object_not_found"}')
            if m == "PATCH":
                self.databases[db_id].update(body["properties"])
            return ok({"id": db_id, "properties": self.databases[db_id]})
        if m == "POST" and path == "/pages":
            pid = uuid.uuid4().hex
            self.pages[pid] = self._touch({"id": pid, "db": body["parent"]["database_id"],
                                           "properties": self._read(body["properties"])})
            return ok(self.pages[pid])
        if path.startswith("/pages/"):
            page = self.pages[path.split("/")[2]]
            if m == "PATCH":
                page["properties"].update(self._read(body["properties"]))
                self._touch(page)
            return ok(page)
        return RawResponse(404, {}, b"{}")

    def user_edit(self, page_id: str) -> None:
        self._touch(self.pages[page_id])


def make_tracker(tmp_path: Path, fake: FakeNotion) -> NotionTracker:
    return NotionTracker("secret", fake.parent, tmp_path / "notion.json", transport=fake)


def test_notion_creates_database_once_and_finds_it_again(tmp_path):
    fake = FakeNotion()
    first = make_tracker(tmp_path, fake).ensure_database()
    (tmp_path / "notion.json").unlink()                     # lost cache: find it among the page's children
    assert make_tracker(tmp_path, fake).ensure_database() == first and len(fake.databases) == 1
    assert set(fake.databases[first]) == {name for name, _ in PROPS.values()}


def test_notion_update_is_version_aware(tmp_path):
    fake = FakeNotion()
    tracker = make_tracker(tmp_path, fake)
    app = tracker.create_application("Northwind Labs", "Backend Engineer Intern", Status.APPLIED, posting_id="NW-221")
    plan = Update(app.id, app.rev, {"status": Status.INTERVIEW.value, "due": "2026-11-07T17:00:00-08:00"}, app.edited_at)
    updated = tracker.apply_update(plan, "interview_request")
    assert (updated.status, updated.rev) == (Status.INTERVIEW, 1)
    with pytest.raises(RevConflict):
        tracker.apply_update(plan, "stale plan")             # rev moved
    fresh = tracker.applications()[0]
    fake.user_edit(fresh.id)
    with pytest.raises(RevConflict):
        tracker.apply_update(Update(fresh.id, fresh.rev, {"status": Status.OFFER.value}, fresh.edited_at), "x")


# ── calendar ───────────────────────────────────────────────────────────────────────────

ICS = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:standup
SUMMARY:Standup
DTSTART;TZID=America/Los_Angeles:20261102T093000
DTEND;TZID=America/Los_Angeles:20261102T100000
RRULE:FREQ=WEEKLY;BYDAY=MO,WE;COUNT=6
EXDATE;TZID=America/Los_Angeles:20261104T093000
END:VEVENT
BEGIN:VEVENT
UID:standup
RECURRENCE-ID;TZID=America/Los_Angeles:20261109T093000
SUMMARY:Standup (moved)
DTSTART;TZID=America/Los_Angeles:20261109T140000
DTEND;TZID=America/Los_Angeles:20261109T143000
END:VEVENT
BEGIN:VEVENT
UID:lunch
SUMMARY:Lunch with\\, Sam
DTSTART:20261105T190000Z
DTEND:20261105T200000Z
END:VEVENT
BEGIN:VEVENT
UID:holiday
SUMMARY:Holiday
DTSTART;VALUE=DATE:20261105
DTEND;VALUE=DATE:20261106
END:VEVENT
END:VCALENDAR
"""


def test_ics_recurrence_exdate_override_and_utc():
    events = calendar_ics.parse_ics(ICS, now=datetime(2026, 11, 1, tzinfo=LA))
    standups = [e for e in events if e.uid == "standup"]
    starts = [e.start.strftime("%m-%d %H:%M") for e in standups]
    assert "11-04 09:30" not in starts and "11-09 14:00" in starts and "11-09 09:30" not in starts
    assert len(standups) == 5            # COUNT=6 minus one EXDATE
    # DST ends 11-01: recurring wall-clock time stays at 09:30.
    assert all(e.start.hour in (9, 14) for e in standups)
    lunch = next(e for e in events if e.uid == "lunch")
    assert (lunch.summary, lunch.start.hour) == ("Lunch with, Sam", 11)


def test_free_slots_skip_busy_time_and_ignore_all_day():
    events = calendar_ics.parse_ics(ICS, now=datetime(2026, 11, 1, tzinfo=LA))
    slots = calendar_ics.free_slots(events, date(2026, 11, 5), work_start=time(9), work_end=time(14))
    assert [(a.strftime("%H:%M"), b.strftime("%H:%M")) for a, b in slots] == [("09:00", "10:45"), ("12:15", "14:00")]
    picks = suggest_slots(events, datetime(2026, 11, 4, 20, tzinfo=LA), "America/Los_Angeles")
    assert [p[0].strftime("%m-%d %H:%M") for p in picks] == ["11-05 09:00", "11-06 09:00", "11-09 09:00"]


# ── extractor ──────────────────────────────────────────────────────────────────────────

def test_extraction_request_is_private_and_neutralizes_tags():
    msg = parse_message(synthetic.mails()[4][1].as_bytes())
    body = build_request(msg)
    text = body["input"][0]["content"][0]["text"]
    assert text.startswith("<!--route task=mail_extract privacy=high-->")
    assert text.count("<!--") == 1 and body["stream"] is False


class FakeResp:
    def __init__(self, obj):
        self.data = json.dumps(obj).encode()

    def read(self):
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def test_extractor_returns_none_when_router_holds():
    held = {"status": "completed", "model": "a3-router/hold",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "held"}]}]}
    ok = {**held, "model": "gemma4:e4b"}
    msg = parse_message(synthetic.mails()[0][1].as_bytes())
    assert RouterExtractor(opener=lambda req, timeout: FakeResp(held))(msg) is None
    assert RouterExtractor(opener=lambda req, timeout: FakeResp(ok))(msg) == "held"


# ── end to end with fakes ──────────────────────────────────────────────────────────────

class FakeMailbox:
    def __init__(self, messages):
        self.messages = messages

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def fetch_new(self, cursor):
        new = [(uid, m) for uid, m in self.messages if uid > cursor.last_uid]
        cursor.uidvalidity = 1
        cursor.last_uid = max([cursor.last_uid] + [u for u, _ in new])
        return new


EXTRACTIONS = {
    "Application received": {"type": "ack", "company": "Cardinal Robotics", "role": "Robotics Software Intern",
                             "posting_id": "CR-1042", "applied_on": "2026-11-03", "deadline": None, "confidence": 0.95},
    "Interview invitation": {"type": "interview_request", "company": "Northwind Labs",
                             "role": "Backend Engineer Intern", "posting_id": "NW-221", "applied_on": None,
                             "deadline": "2026-11-07T17:00:00-08:00", "confidence": 0.9},
    "An update": {"type": "other", "company": "Northwind Labs", "role": None, "posting_id": None,
                  "applied_on": None, "deadline": None, "confidence": 0.5},
    "monthly": {"type": "newsletter", "company": "Northwind Labs", "role": None, "posting_id": None,
                "applied_on": None, "deadline": None, "confidence": 0.9},
}


def test_sweep_end_to_end(tmp_path):
    backlog = "1"
    fake_notion, discord = FakeNotion(), FakeDiscord()
    tracker = make_tracker(tmp_path, fake_notion)
    for app in synthetic.APPLICATIONS:
        tracker.create_application(app["company"], app["role"], app["status"],
                                   **{k: v for k, v in app.items() if k not in ("company", "role", "status")})
    mails = [(i, parse_message(m.as_bytes())) for i, (_, m) in enumerate(synthetic.mails(), 1)]
    asked = []

    def extractor(message):
        asked.append(message.subject)
        return json.dumps(next(v for k, v in EXTRACTIONS.items() if k in message.subject))

    settings = Settings({"KAIRO_STATE_DIR": str(tmp_path / "state"), "KAIRO_SENDER_ALLOWLIST": synthetic.SENDER_ALLOWLIST,
                         "KAIRO_RECIPIENT_ALLOWLIST": "me@kairo.test", "DISCORD_ALLOWED_USERS": ME,
                         "DISCORD_HOME_CHANNEL": "chan", "KAIRO_MAIL_USER": "me@kairo.test",
                         "KAIRO_MAIL_APP_PASSWORD": "x", "KAIRO_PROCESS_BACKLOG": backlog})
    kairo = Kairo(settings, extractor=extractor, summarizer=lambda m: None, tracker=tracker, discord=discord,
                  mailbox_factory=lambda: FakeMailbox(mails), smtp=FakeSmtp(), calendar_text=lambda: ICS)
    report = kairo.sweep()
    assert report.outcomes == {"applied": 2, "ignored": 2, "suspicious": 1}
    assert not any("Offer letter" in s for s in asked)          # the unverified stranger never reached the model
    by_role = {a.role: a for a in tracker.applications()}
    assert by_role["Robotics Software Intern"].status == Status.APPLIED
    assert by_role["Backend Engineer Intern"].status == Status.INTERVIEW
    assert by_role["Data Engineer Intern"].status == Status.APPLIED      # the injected "offer" did nothing
    alerts = [p["content"] for p in discord.posts]
    assert len(alerts) == 2 and "Interview" in alerts[0] and "Available times" in alerts[0]
    assert alerts[1].startswith("⚠️ Suspicious email") and "authentication" in alerts[1]
    assert kairo.sweep().fetched == 0                                    # cursor and ledger hold


# ── schedule helpers ───────────────────────────────────────────────────────────────────

from kairo_core import report as report_mod
from kairo_core import runner as runner_mod


def make_kairo(tmp_path, mails=(), ics=ICS, notion=None, backlog="1"):
    notion = notion or FakeNotion()
    tracker = make_tracker(tmp_path, notion)
    for app in synthetic.APPLICATIONS:
        tracker.create_application(app["company"], app["role"], app["status"],
                                   **{k: v for k, v in app.items() if k not in ("company", "role", "status")})
    settings = Settings({"KAIRO_STATE_DIR": str(tmp_path / "state"), "KAIRO_SENDER_ALLOWLIST": synthetic.SENDER_ALLOWLIST,
                         "KAIRO_RECIPIENT_ALLOWLIST": "me@kairo.test,@northwind.test", "DISCORD_ALLOWED_USERS": ME,
                         "DISCORD_HOME_CHANNEL": "chan", "KAIRO_MAIL_USER": "me@kairo.test",
                         "KAIRO_MAIL_APP_PASSWORD": "x", "KAIRO_PROCESS_BACKLOG": backlog})

    def extractor(message):
        return json.dumps(next(v for k, v in EXTRACTIONS.items() if k in message.subject))

    discord = FakeDiscord()
    k = Kairo(settings, extractor=extractor, summarizer=lambda m: None, tracker=tracker, discord=discord,
              mailbox_factory=lambda: FakeMailbox(list(mails)), smtp=FakeSmtp(), calendar_text=lambda: ics)
    return k, discord


@pytest.mark.parametrize("hhmm,quiet", [("21:59", False), ("22:00", True), ("03:00", True), ("07:29", True),
                                        ("07:30", False)])
def test_quiet_hours(hhmm, quiet):
    h, m = map(int, hhmm.split(":"))
    assert report_mod.is_quiet(datetime(2026, 11, 4, h, m, tzinfo=LA)) is quiet


def test_quiet_hours_defer_alert_to_briefing(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_mod, "is_quiet", lambda now: True)
    # The fixture's deadline is a fixed date (2026-11-07 17:00 PT); keep it non-urgent whatever today is.
    monkeypatch.setattr(runner_mod, "URGENT_WITHIN", timedelta(days=-36500))
    mails = [(1, parse_message(synthetic.mails()[1][1].as_bytes()))]          # interview request
    k, discord = make_kairo(tmp_path, mails)
    k.sweep()
    assert discord.posts == []
    text = report_mod.briefing(k)
    assert "Interview" in text and "deferred during quiet hours:" in text
    assert "quiet hours: none" in report_mod.briefing(k)                                    # delivered once


def test_interview_soon_alerts_once(tmp_path):
    ics = ICS.replace("SUMMARY:Lunch with\\, Sam", "SUMMARY:Interview - Northwind Labs")
    k, _ = make_kairo(tmp_path, ics=ics)
    now = datetime(2026, 11, 5, 10, 40, tzinfo=LA)                             # lunch moved: 11:00 PT
    first = report_mod.interview_soon(k, now)
    assert "Interview in 20 min" in first
    assert report_mod.interview_soon(k, now) == ""


def _http_429(url):
    import urllib.error
    raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, None)


def test_calendar_cache_survives_rate_limit(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(calendar_ics, "_event_log", lambda event, **f: print(json.dumps({"event": event, **f})))
    clock = [1_000_000.0]
    responses = [lambda url: ICS, _http_429]
    cal = calendar_ics.CachedCalendar("https://cal.test/x.ics", tmp_path / "calendar.ics",
                                      fetch=lambda url: responses[0](url), clock=lambda: clock[0])
    with pytest.raises(OSError):                             # no copy yet: the error surfaces as before
        calendar_ics.CachedCalendar("u", tmp_path / "none.ics", fetch=_http_429)()
    assert cal() == ICS and cal.stale_s == 0
    responses[0] = _http_429
    clock[0] = (tmp_path / "calendar.ics").stat().st_mtime + 600
    assert cal() == ICS and cal.stale_s == pytest.approx(600)
    logged = [json.loads(l) for l in capsys.readouterr().out.splitlines()]
    assert logged[-1] == {"event": "calendar_fetch_failed", "status": 429, "error": "HTTPError", "cache": True,
                          "cache_age_s": 600}


def test_stale_calendar_notice_once_per_outage(tmp_path, monkeypatch):
    monkeypatch.setattr(calendar_ics, "_event_log", lambda event, **f: None)
    k, _ = make_kairo(tmp_path)
    state = Path(k.s.state_dir)
    fetch = [lambda url: ICS]
    clock = [0.0]
    k.calendar = calendar_ics.CachedCalendar("u", state / "calendar.ics", fetch=lambda url: fetch[0](url),
                                             clock=lambda: clock[0])
    k.calendar_text = k.calendar
    noon = datetime(2026, 11, 5, 12, 0, tzinfo=LA)
    assert report_mod.interview_soon(k, noon) == ""
    fetch[0] = _http_429
    mtime = (state / "calendar.ics").stat().st_mtime
    clock[0] = mtime + 30 * 60
    assert report_mod.interview_soon(k, noon) == ""                          # 30 min stale: quiet
    clock[0] = mtime + 61 * 60
    assert "hasn't been able to refresh your calendar" in report_mod.interview_soon(k, noon)
    assert report_mod.interview_soon(k, noon) == ""                          # once per outage
    fetch[0] = lambda url: ICS
    report_mod.interview_soon(k, noon)                                       # recovered: outage over
    fetch[0] = _http_429
    clock[0] = (state / "calendar.ics").stat().st_mtime + 61 * 60
    night = datetime(2026, 11, 5, 23, 0, tzinfo=LA)
    assert report_mod.interview_soon(k, night) == ""                         # quiet hours: deferred
    assert "calendar_stale" in (state / "deferred.jsonl").read_text()


def test_followups_find_due_soon_and_unanswered_interview(tmp_path):
    mails = [(1, parse_message(synthetic.mails(now=datetime(2026, 11, 4, 9, tzinfo=LA))[1][1].as_bytes()))]
    k, _ = make_kairo(tmp_path, mails)
    k.sweep()
    text = report_mod.followups(k, now=datetime(2026, 11, 7, 9, tzinfo=LA))
    assert "[DUE WITHIN 24H] Northwind Labs · Backend Engineer Intern" in text
    assert "[UNANSWERED" in text and "interview request" in text
    assert "[QUIET" not in text
    later = report_mod.followups(k, now=datetime(2026, 11, 13, 9, tzinfo=LA))
    assert "[QUIET 11 DAYS] Northwind Labs · Data Engineer Intern" in later         # applied 11/02, no contact


def test_eod_reads_utc_named_logs_for_the_local_day(tmp_path):
    k, _ = make_kairo(tmp_path)
    late_evening_utc = datetime.now(timezone.utc).replace(microsecond=0)
    k.mail_log.mkdir(parents=True, exist_ok=True)
    (k.mail_log / f"{(late_evening_utc + timedelta(days=1)):%Y-%m-%d}.jsonl").write_text("")
    (k.mail_log / f"{late_evening_utc:%Y-%m-%d}.jsonl").write_text(json.dumps({
        "ts": late_evening_utc.isoformat(), "message_id": "<m@x>", "outcome": "applied", "type": "ack",
        "fields": ["status"]}) + "\n")
    assert "type ack → applied" in report_mod.eod(k)


def test_discord_and_notion_text_is_english(tmp_path):
    """Project rule: everything posted to Discord or written to Notion is English."""
    import re as _re
    hangul = _re.compile("[\uac00-\ud7a3]")
    fake = FakeNotion()
    k, discord = make_kairo(tmp_path, [(i, parse_message(m.as_bytes())) for i, (_, m) in enumerate(synthetic.mails(), 1)],
                            notion=fake)
    k.sweep()
    d = k.outbox.create_draft("me@kairo.test", "s", "body")
    request_approval(discord, k.outbox, d, "chan")
    posted = json.dumps(discord.posts, ensure_ascii=False)
    written = json.dumps([fake.databases, [p["properties"] for p in fake.pages.values()]], ensure_ascii=False)
    assert not hangul.search(posted) and not hangul.search(written)


def test_last_contact_uses_local_date_not_utc(tmp_path):
    """6 PM PT on Nov 4 is already Nov 5 in UTC; Notion must say Nov 4."""
    evening_utc = datetime(2026, 11, 5, 2, 0, tzinfo=timezone.utc)
    msg = parse_message(synthetic.mails(now=datetime(2026, 11, 4, 18, tzinfo=LA))[0][1].as_bytes(), evening_utc)
    k, _ = make_kairo(tmp_path, [(1, msg)])
    k.sweep()
    row = next(r for r in k.tracker.rows() if r.fields["company"] == "Cardinal Robotics")
    assert row.fields["last_contact"] == "2026-11-04"


# ── snapshots ──────────────────────────────────────────────────────────────────────────

def test_snapshot_round_trip_skips_secrets_and_copies_sqlite(tmp_path):
    import sqlite3 as _sqlite
    from kairo_core import snapshot
    kairo, hermes, bucket = tmp_path / "state", tmp_path / "hermes", tmp_path / "bucket"
    (kairo / "secrets").mkdir(parents=True)
    (kairo / "secrets" / "approval.key").write_bytes(b"k" * 32)
    (kairo / "notion.json").write_text('{"database_id": "x"}')
    db = _sqlite.connect(kairo / "ledger.sqlite3")
    db.execute("CREATE TABLE t (v)"); db.execute("INSERT INTO t VALUES (1)"); db.commit()   # left open, like a live process
    (hermes / "sessions").mkdir(parents=True)
    (hermes / "sessions" / "s1.json").write_text("{}")
    (hermes / "auth.json").write_text("secret")
    snapshot.backup({"kairo": kairo, "hermes": hermes}, bucket)
    db.close()

    fresh_k, fresh_h = tmp_path / "k2", tmp_path / "h2"
    assert snapshot.restore({"kairo": fresh_k, "hermes": fresh_h}, bucket)
    assert (fresh_k / "notion.json").exists() and (fresh_h / "sessions" / "s1.json").exists()
    assert not (fresh_k / "secrets").exists() and not (fresh_h / "auth.json").exists()
    assert _sqlite.connect(fresh_k / "ledger.sqlite3").execute("SELECT v FROM t").fetchone() == (1,)


def test_first_run_records_existing_mail_as_baseline(tmp_path):
    mails = [(i, parse_message(m.as_bytes())) for i, (_, m) in enumerate(synthetic.mails(), 1)]
    k, discord = make_kairo(tmp_path, mails, backlog="0")
    first = k.sweep()
    assert first.outcomes == {"baseline": 5} and discord.posts == []
    assert all(r.status.value in ("Preparing", "Applied") for r in k.tracker.applications())


# ── scenario injector ──────────────────────────────────────────────────────────────────

def test_scenario_schedule_wraps_past_times_to_next_day():
    from kairo_core.scenario import SCENARIOS, schedule
    plan = schedule(SCENARIOS["soak"], datetime(2026, 11, 6, 14, 30, tzinfo=LA))
    assert [(w.strftime("%m/%d %H:%M"), e.name) for w, e in plan] == [
        ("11/06 18:30", "interview_request_NW-305"), ("11/06 23:10", "assessment_reminder_quiet"),
        ("11/06 23:40", "assessment_urgent_quiet"), ("11/07 07:10", "injection_newsletter")]


def test_scenario_mails_parse_and_pass_the_sender_allowlist():
    from kairo_core.scenario import SCENARIOS
    allow = parse_allowlist(synthetic.SENDER_ALLOWLIST)
    now = datetime(2026, 11, 6, 23, 40, tzinfo=LA)
    for e in SCENARIOS["soak"]:
        msg = parse_message(e.build(now).as_bytes())
        assert msg.message_id and allow.permits(msg.sender)
        assert "Emily" in msg.body or e.name == "injection_newsletter"


def test_env_file_read_retries_transient_io_errors(tmp_path, monkeypatch):
    import pathlib
    from kairo_core import config as config_mod
    f = tmp_path / "kairo.env"
    f.write_text("A_KEY=v\n")
    real, calls = pathlib.Path.read_text, {"n": 0}

    def flaky(self, *a, **k):
        calls["n"] += 1
        if calls["n"] < 3:
            raise OSError(5, "Input/output error")
        return real(self, *a, **k)

    monkeypatch.setattr(pathlib.Path, "read_text", flaky)
    monkeypatch.setattr(config_mod.time, "sleep", lambda s: None)
    assert config_mod.read_env_file(f) == {"A_KEY": "v"} and calls["n"] == 3
