"""Command line: ``python -m kairo_core <command>``.

check         read-only connectivity check of every test account (prints no secrets)
init          create or find the Notion database
seed-notion   add the synthetic applications to Notion (skips ones already there)
seed-mail     put the synthetic recruiting emails into the test INBOX (IMAP APPEND, nothing is sent)
sweep         process new INBOX mail once
watch         IMAP IDLE loop plus approval sweep (Ctrl-C to stop)
calendar      today's events and free slots
draft         create a draft and ask for approval on Discord
approvals     act on ✅ / 🗑️ reactions once
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from . import calendar_ics, synthetic
from .config import Settings
from .mail import Mailbox
from .runner import Kairo, fmt_dt


def cmd_check(s: Settings) -> int:
    ok = True

    def report(name: str, fn) -> None:
        nonlocal ok
        try:
            print(f"[OK  ] {name}: {s.redact(fn())}")
        except Exception as exc:
            ok = False
            print(f"[FAIL] {name}: {s.redact(f'{type(exc).__name__}: {exc}')}")

    k = Kairo(s)

    def imap():
        with k.mailbox_factory() as box:
            box.select_inbox()
            return f"login ok, IDLE={'IDLE' in box.conn.capabilities}"
    report("Gmail IMAP", imap)
    report("Notion", lambda: f"database {k.tracker.ensure_database()[:8]}…, {len(k.tracker.rows())} rows")
    report("Calendar", lambda: f"{len(calendar_ics.parse_ics(k.calendar_text(), k.tz_name))} upcoming events")
    report("Discord", lambda: k.discord.client.request("GET", f"/channels/{k.channel}")["name"])
    report("Allowlists", lambda: f"recipients={bool(s.get('KAIRO_RECIPIENT_ALLOWLIST'))}, "
                                 f"approvers={len(s.allowed_discord_users)}, "
                                 f"trusted sender domains={sum(len(a.email_domains) for a in k.tracker.applications())}, "
                                 f"calendars={len(calendar_ics.split_urls(s.get('KAIRO_CALENDAR_ICS_URL')))}")
    return 0 if ok else 1


def cmd_seed_notion(k: Kairo) -> int:
    existing = {(a.company, a.role) for a in k.tracker.applications()}
    for app in synthetic.APPLICATIONS:
        if (app["company"], app["role"]) in existing:
            print(f"skip   {app['company']} · {app['role']}")
            continue
        fields = {key: v for key, v in app.items() if key not in ("company", "role", "status")}
        k.tracker.create_application(app["company"], app["role"], app["status"], **fields)
        print(f"added  {app['company']} · {app['role']} ({app['status'].value})")
    return 0


def cmd_seed_mail(k: Kairo) -> int:
    with k.mailbox_factory() as box:
        for expected, message in synthetic.mails(tz=k.tz_name):
            box.append("INBOX", message)
            print(f"{message['Subject'][:60]:<62} expect {expected}")
    return 0


def cmd_calendar(k: Kairo) -> int:
    tz = ZoneInfo(k.tz_name)
    now = datetime.now(tz)
    events = calendar_ics.parse_ics(k.calendar_text(), k.tz_name)
    today = calendar_ics.between(events, now.replace(hour=0, minute=0), now.replace(hour=23, minute=59))
    print(f"{len(events)} upcoming events; today: {len(today)}")
    for e in today:
        print(f"  {fmt_dt(e.start)}–{e.end:%H:%M} {e.summary}")
    for a, b in calendar_ics.free_slots(events, now.date(), k.tz_name):
        print(f"  free {a:%H:%M}–{b:%H:%M}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kairo_core", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["check", "init", "seed-notion", "seed-mail", "sweep", "watch",
                                            "calendar", "draft", "approvals"])
    parser.add_argument("--to")
    parser.add_argument("--subject")
    parser.add_argument("--body")
    parser.add_argument("--env-file")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    s = Settings.load(args.env_file)
    if args.command == "check":
        return cmd_check(s)
    k = Kairo(s)
    if args.command == "init":
        print(f"Notion database ready: {k.tracker.ensure_database()[:8]}…")
    elif args.command == "seed-notion":
        return cmd_seed_notion(k)
    elif args.command == "seed-mail":
        return cmd_seed_mail(k)
    elif args.command == "sweep":
        print(k.sweep())
    elif args.command == "watch":
        try:
            k.watch()
        except KeyboardInterrupt:
            return 0
    elif args.command == "calendar":
        return cmd_calendar(k)
    elif args.command == "draft":
        if not (args.to and args.subject and args.body):
            parser.error("draft needs --to, --subject and --body")
        print(f"draft {k.draft(args.to, args.subject, args.body, origin='cli')} waiting for approval on Discord")
    elif args.command == "approvals":
        for result in k.approvals():
            print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
