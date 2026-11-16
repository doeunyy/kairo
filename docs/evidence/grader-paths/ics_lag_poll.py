"""Poll the primary calendar's ICS until an event titled 'Interview test' appears; print only timings."""
import os, re, sys, time
from datetime import datetime, timezone
sys.path.insert(0, "/Users/doeunkim/GitHub/usc/csci-599-asgmt3/kairo/core")
from kairo_core.config import read_env_file
from kairo_core.calendar_ics import split_urls, fetch_ics, _unfold
url = split_urls(read_env_file(os.path.expanduser("~/.a3-kairo/kairo.env"))["KAIRO_CALENDAR_ICS_URL"])[1]
start = datetime.now(timezone.utc)
print("polling started", start.isoformat(timespec="seconds"), flush=True)
for i in range(75):
    now = datetime.now(timezone.utc)
    try:
        text = fetch_ics(url)
    except OSError as exc:
        print(now.isoformat(timespec="seconds"), "fetch error", getattr(exc, "code", type(exc).__name__), flush=True)
        time.sleep(60); continue
    block = None
    for ev in text.split("BEGIN:VEVENT")[1:]:
        lines = _unfold(ev)
        if any(l.upper().startswith("SUMMARY") and "interview test" in l.lower() for l in lines):
            block = lines; break
    if block:
        created = next((l.split(":", 1)[1] for l in block if l.startswith("CREATED")), None)
        c = datetime.strptime(created, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc) if created else None
        print("FOUND", now.isoformat(timespec="seconds"), "created", c.isoformat() if c else None,
              "lag_s", int((now - c).total_seconds()) if c else None, "polls", i + 1, flush=True)
        break
    if i % 5 == 0:
        print(now.isoformat(timespec="seconds"), "not yet", flush=True)
    time.sleep(60)
else:
    print("gave up after 75 min", flush=True)
