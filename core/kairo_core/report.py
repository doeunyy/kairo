"""Data for scheduled jobs, printed for Hermes cron scripts.

Agent jobs (``--script``) get this text injected into their prompt; ``--no-agent`` jobs deliver
it verbatim, and empty output means nothing is sent. Nothing here calls a model.

    python -m kairo_core.report briefing | eod | followups | interview-soon | sweep
"""

from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import calendar_ics
from .config import Settings
from .status import Status

QUIET_START, QUIET_END = time(22, 0), time(7, 30)
URGENT_WITHIN = timedelta(hours=12)
INTERVIEW_WORDS = re.compile(r"interview|면접|onsite|phone screen", re.IGNORECASE)
FOLLOW_UP_AFTER_DAYS = 10


def is_quiet(now: datetime) -> bool:
    t = now.timetz().replace(tzinfo=None)
    return t >= QUIET_START or t < QUIET_END


# ── deferred alerts (quiet hours) ─────────────────────────────────────────────────────

def defer(state: Path, text: str, reason: str) -> None:
    with open(state / "deferred.jsonl", "a") as fh:
        fh.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "reason": reason,
                             "text": text}, ensure_ascii=False) + "\n")


def take_deferred(state: Path) -> list[dict]:
    path = state / "deferred.jsonl"
    if not path.exists():
        return []
    items = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    archive = state / "logs" / "deferred"
    archive.mkdir(parents=True, exist_ok=True)
    with open(archive / f"{datetime.now(timezone.utc):%Y-%m-%d}.jsonl", "a") as fh:
        for item in items:
            fh.write(json.dumps({**item, "delivered_in": "morning_briefing"}, ensure_ascii=False) + "\n")
    path.unlink()
    return items


# ── helpers ───────────────────────────────────────────────────────────────────────────

def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def _utc_days(local_day: date) -> tuple[date, ...]:
    """Log files are named by UTC date; a local day spans two of them."""
    return (local_day - timedelta(days=1), local_day, local_day + timedelta(days=1))


def _local(ts: str, tz: ZoneInfo) -> datetime:
    return datetime.fromisoformat(ts).astimezone(tz)


# ── reports ───────────────────────────────────────────────────────────────────────────

def briefing(k) -> str:
    deferred = take_deferred(k.s.state_dir)
    lines = [f"Today: {datetime.now(ZoneInfo(k.tz_name)):%Y-%m-%d (%a)} {k.tz_name}"]
    if deferred:
        lines.append("Alerts deferred during quiet hours:")
        lines += [f"- {d['text']}" for d in deferred]
    else:
        lines.append("Alerts deferred during quiet hours: none")
    return "\n".join(lines)


def eod(k) -> str:
    tz = ZoneInfo(k.tz_name)
    today = datetime.now(tz).date()
    state = k.s.state_dir
    mail = [m for day in _utc_days(today)
            for m in _read_jsonl(state / "logs" / "mail" / f"{day:%Y-%m-%d}.jsonl")
            if _local(m["ts"], tz).date() == today and m["outcome"] != "dropped"]
    outbox = [e for day in _utc_days(today)
              for e in _read_jsonl(state / "logs" / "outbox" / f"{day:%Y-%m-%d}.jsonl")
              if _local(e["ts"], tz).date() == today]
    lines = [f"{today} Kairo activity log (things Kairo did automatically, not the user):"]
    if mail:
        index = {r["message_id"]: r for r in k.ledger.recent_mail((datetime.now(timezone.utc) - timedelta(days=2))
                                                                  .isoformat(timespec="seconds"), 200)}
        for m in mail:
            r = index.get(m["message_id"], {})
            lines.append(f"- Processed email: {r.get('company') or '?'} · {r.get('role') or ''} · type {m['type']} → {m['outcome']}"
                         + (f" (Notion fields changed: {', '.join(m['fields'])})" if m.get("fields") else ""))
    else:
        lines.append("- No recruiting email processed")
    for e in outbox:
        if e["event"] in ("draft_created", "sent", "discarded", "blocked", "send_failed"):
            lines.append(f"- Email {e['event']}: draft {e['draft_id']} → {e['to']}")
    pending = k.outbox.awaiting_approval()
    lines.append(f"Drafts waiting for approval: {len(pending)}" + (f" ({', '.join(d.draft_id for d in pending)})" if pending else ""))
    return "\n".join(lines)


def followups(k, now: datetime | None = None) -> str:
    tz = ZoneInfo(k.tz_name)
    now = now or datetime.now(tz)
    rows = k.tracker.rows()
    items = []
    for r in rows:
        f, app = r.fields, r.application
        due = f.get("due")
        if due and f.get("next_action"):
            due_at = datetime.fromisoformat(due)
            due_at = due_at if due_at.tzinfo else datetime.combine(due_at.date(), time(23, 59), tz)
            if now <= due_at <= now + timedelta(hours=24):
                items.append(f"[DUE WITHIN 24H] {app.company} · {app.role}: {f['next_action']} "
                             f"(due {due_at.astimezone(tz):%m/%d %H:%M})")
        if app.status == Status.APPLIED:
            last = f.get("last_contact") or f.get("applied_on")
            if last and (now.date() - date.fromisoformat(last[:10])).days >= FOLLOW_UP_AFTER_DAYS:
                items.append(f"[QUIET {(now.date() - date.fromisoformat(last[:10])).days} DAYS] {app.company} · {app.role} "
                             f"(last contact {last[:10]}): suggest a follow-up email")
    replied_to = {d["in_reply_to"] for d in _drafts(k) if d["in_reply_to"]}
    for m in k.ledger.recent_mail((now - timedelta(days=14)).astimezone(timezone.utc).isoformat(timespec="seconds")):
        received = _local(m["received_at"], tz)
        if m["type"] == "interview_request" and now - received >= timedelta(hours=24) \
                and m["message_id"] not in replied_to:
            items.append(f"[UNANSWERED {int((now - received).total_seconds() // 3600)}H] {m['company']} · {m['role']} "
                         f"interview request (Message-ID {m['message_id']}, from {m['sender']})")
    return "\n".join(items)


def _drafts(k) -> list[dict]:
    return [dict(r) for r in k.outbox.db.execute("SELECT draft_id, in_reply_to, status FROM drafts").fetchall()]


def interview_soon(k, now: datetime | None = None, window: timedelta = timedelta(minutes=30)) -> str:
    tz = ZoneInfo(k.tz_name)
    now = now or datetime.now(tz)
    sent_path = k.s.state_dir / "alerts_sent.json"
    sent = set(json.loads(sent_path.read_text())) if sent_path.exists() else set()
    events = calendar_ics.parse_ics(k.calendar_text(), k.tz_name)
    out = []
    for e in events:
        key = f"interview:{e.uid}:{e.start.isoformat()}"
        if not e.all_day and INTERVIEW_WORDS.search(e.summary) and now <= e.start <= now + window and key not in sent:
            minutes = int((e.start - now).total_seconds() // 60)
            out.append(f"⏰ Interview in {minutes} min: {e.summary} ({e.start:%H:%M}–{e.end:%H:%M}"
                       + (f", {e.location}" if e.location else "") + ")\n"
                       "Reply \"prep\" for the JD's key requirements and a summary of past contact.")
            sent.add(key)
    if out:
        sent_path.write_text(json.dumps(sorted(sent)))
    return "\n\n".join(out)


def main(argv: list[str] | None = None) -> int:
    from .runner import Kairo
    kind = (argv or sys.argv[1:] or [""])[0]
    k = Kairo(Settings.load())
    if kind == "sweep":
        report = k.sweep()
        held = (report.outcomes or {}).get("held")
        stamp = k.s.state_dir / "held_notice_at"
        recent = stamp.exists() and datetime.now(timezone.utc).timestamp() - stamp.stat().st_mtime < 3 * 3600
        if held and not recent:
            # At most one notice every 3 hours while the laptop stays offline.
            stamp.touch()
            print(f"⚠️ The local model (laptop) is unreachable, so {held} recruiting email(s) were held instead of "
                  "being sent to the cloud. They will be processed automatically once it is back.")
        return 0
    fn = {"briefing": briefing, "eod": eod, "followups": followups, "interview-soon": interview_soon}.get(kind)
    if fn is None:
        print("usage: python -m kairo_core.report briefing|eod|followups|interview-soon|sweep", file=sys.stderr)
        return 2
    text = fn(k)
    if text:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
