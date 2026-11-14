#!/usr/bin/env python3
"""Local job-application tracker for the job-tracker skill (standard library only).

Used when the agent has no Kairo/Notion tools. Rows live in one JSON file:
$JOB_TRACKER_FILE, else $HERMES_HOME/job-tracker/applications.json, else
~/.hermes/job-tracker/applications.json. Every command prints one JSON object.

    tracker.py add --company "Northwind Labs" --role "Data Engineer Intern" --posting-id NW-305 \
                   --due 2026-11-20T12:00-08:00 --next-action "Submit application" --jd-summary "..."
    tracker.py list [--status Applied]
    tracker.py update (--id ID | --company C --role R) [--status S] [--next-action T] [--due D]
                      [--contact YYYY-MM-DD] [--note TEXT]
    tracker.py due [--days 7]          # overdue first, then due within N days
    tracker.py quiet [--days 10]       # Applied with no contact for N+ days
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

STATUSES = ["Interested", "Preparing", "Applied", "Assessment", "Interview", "Offer", "Rejected", "Withdrawn"]
CLOSED = {"Offer", "Rejected", "Withdrawn"}


def store_path() -> Path:
    if os.environ.get("JOB_TRACKER_FILE"):
        return Path(os.environ["JOB_TRACKER_FILE"]).expanduser()
    home = Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()
    return home / "job-tracker" / "applications.json"


def load() -> list[dict]:
    path = store_path()
    if not path.exists():
        return []
    return json.loads(path.read_text()).get("applications", [])


def save(rows: list[dict]) -> None:
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".applications.", suffix=".tmp")
    with os.fdopen(fd, "w") as fh:
        json.dump({"version": 1, "applications": rows}, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)           # atomic: a crash never leaves half a file


def norm(text: str | None) -> str:
    return " ".join((text or "").lower().split())


def status_of(value: str) -> str:
    match = next((s for s in STATUSES if s.lower() == value.strip().lower()), None)
    if not match:
        raise SystemExit(json.dumps({"error": f"unknown status {value!r}", "statuses": STATUSES}))
    return match


def parse_due(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).isoformat() if "T" in value else date.fromisoformat(value).isoformat()
    except ValueError:
        raise SystemExit(json.dumps({"error": f"due must be ISO 8601 (YYYY-MM-DD or YYYY-MM-DDTHH:MM±HH:MM), got {value!r}"}))


def due_moment(row: dict) -> datetime | None:
    if not row.get("due"):
        return None
    d = datetime.fromisoformat(row["due"]) if "T" in row["due"] else datetime.combine(
        date.fromisoformat(row["due"]), datetime.max.time())
    return d if d.tzinfo else d.astimezone()     # local time when no offset was given


def find(rows: list[dict], args) -> dict:
    if args.id:
        hits = [r for r in rows if r["id"] == args.id]
    else:
        hits = [r for r in rows if norm(r["company"]) == norm(args.company) and norm(r["role"]) == norm(args.role)]
    if len(hits) != 1:
        raise SystemExit(json.dumps({"error": "no matching application" if not hits else "ambiguous; use --id",
                                     "matches": hits}))
    return hits[0]


def cmd_add(args) -> dict:
    rows = load()
    for r in rows:
        same_posting = args.posting_id and norm(r.get("posting_id")) == norm(args.posting_id) \
            and norm(r["company"]) == norm(args.company)
        if same_posting or (norm(r["company"]) == norm(args.company) and norm(r["role"]) == norm(args.role)):
            return {"result": "exists", "application": r}
    today = date.today().isoformat()
    row = {"id": uuid.uuid4().hex[:8], "company": args.company.strip(), "role": args.role.strip(),
           "status": status_of(args.status), "posting_id": args.posting_id, "url": args.url,
           "applied_on": today if status_of(args.status) == "Applied" else None, "last_contact": None,
           "next_action": args.next_action, "due": parse_due(args.due), "jd_summary": args.jd_summary,
           "notes": [], "history": [{"on": today, "status": status_of(args.status)}]}
    rows.append(row)
    save(rows)
    return {"result": "added", "application": row}


def cmd_list(args) -> dict:
    rows = load()
    if args.status:
        rows = [r for r in rows if r["status"] == status_of(args.status)]
    order = {s: i for i, s in enumerate(reversed(STATUSES[:6]))}
    rows.sort(key=lambda r: (r["status"] in CLOSED, order.get(r["status"], 99), due_moment(r) or datetime.max.replace(tzinfo=timezone.utc)))
    return {"count": len(rows), "applications": rows, "file": str(store_path())}


def cmd_update(args) -> dict:
    rows = load()
    row = find(rows, args)
    today = date.today().isoformat()
    if args.status:
        new = status_of(args.status)
        if new != row["status"]:
            row["history"].append({"on": today, "status": new})
            if new == "Applied" and not row.get("applied_on"):
                row["applied_on"] = today
        row["status"] = new
    if args.next_action is not None:
        row["next_action"] = args.next_action or None
    if args.due is not None:
        row["due"] = parse_due(args.due)
    if args.contact:
        row["last_contact"] = date.fromisoformat(args.contact).isoformat()
    if args.note:
        row["notes"].append({"on": today, "text": args.note})
    save(rows)
    return {"result": "updated", "application": row}


def cmd_due(args) -> dict:
    now = datetime.now().astimezone()
    horizon = now + timedelta(days=args.days)
    open_rows = [r for r in load() if r["status"] not in CLOSED and due_moment(r)]
    overdue = sorted((r for r in open_rows if due_moment(r) < now), key=due_moment)
    soon = sorted((r for r in open_rows if now <= due_moment(r) <= horizon), key=due_moment)
    return {"now": now.isoformat(timespec="minutes"), "overdue": overdue, "due_within_days": args.days, "due_soon": soon}


def cmd_quiet(args) -> dict:
    cutoff = date.today() - timedelta(days=args.days)
    quiet = []
    for r in load():
        last = r.get("last_contact") or r.get("applied_on")
        if r["status"] == "Applied" and last and date.fromisoformat(last) <= cutoff:
            quiet.append({**r, "days_quiet": (date.today() - date.fromisoformat(last)).days})
    return {"quiet_after_days": args.days, "applications": sorted(quiet, key=lambda r: -r["days_quiet"])}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("--company", required=True)
    a.add_argument("--role", required=True)
    a.add_argument("--status", default="Interested")
    a.add_argument("--posting-id")
    a.add_argument("--url")
    a.add_argument("--due")
    a.add_argument("--next-action")
    a.add_argument("--jd-summary")
    s = sub.add_parser("list")
    s.add_argument("--status")
    u = sub.add_parser("update")
    u.add_argument("--id")
    u.add_argument("--company")
    u.add_argument("--role")
    u.add_argument("--status")
    u.add_argument("--next-action")
    u.add_argument("--due")
    u.add_argument("--contact")
    u.add_argument("--note")
    d = sub.add_parser("due")
    d.add_argument("--days", type=int, default=7)
    q = sub.add_parser("quiet")
    q.add_argument("--days", type=int, default=10)
    args = ap.parse_args(argv)
    if args.cmd == "update" and not args.id and not (args.company and args.role):
        ap.error("update needs --id, or --company and --role")
    out = {"add": cmd_add, "list": cmd_list, "update": cmd_update, "due": cmd_due, "quiet": cmd_quiet}[args.cmd](args)
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
