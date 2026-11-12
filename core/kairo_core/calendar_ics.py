"""Read-only calendar from a secret ICS URL: events, conflicts and free slots.

A small RFC 5545 reader for what Google Calendar exports: line folding, TZID / UTC /
all-day times, and DAILY or WEEKLY recurrence (INTERVAL, COUNT, UNTIL, BYDAY, EXDATE).
Other recurrence rules are kept as their first occurrence and flagged ``recurrence_unsupported``.
"""

from __future__ import annotations

import json
import os
import re
import ssl
import sys
import urllib.request
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from time import time as time_now
from typing import Callable
from zoneinfo import ZoneInfo

WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
MAX_OCCURRENCES = 500


@dataclass(frozen=True)
class Event:
    uid: str
    summary: str
    start: datetime
    end: datetime
    all_day: bool = False
    location: str = ""
    cancelled: bool = False
    recurrence_unsupported: bool = False


def fetch_ics(url: str, timeout: float = 20.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Kairo/0.1"})
    with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as resp:
        return resp.read().decode("utf-8", "replace")


def _event_log(event: str, **fields) -> None:
    """One JSON line for Cloud Logging. Cron scripts' own stdout is what Hermes delivers to Discord,
    so write to the container's stdout (PID 1) when it is reachable, else to stderr."""
    line = json.dumps({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                       "component": "calendar", "event": event, **fields})
    try:
        with open("/proc/1/fd/1", "a") as fh:
            fh.write(line + "\n")
    except OSError:
        print(line, file=sys.stderr)


class CachedCalendar:
    """Fetches the ICS and keeps the last good copy, so a rate limit (HTTP 429), a 5xx or a network
    error falls back to that copy instead of failing. ``stale_s`` is how old the copy in use is
    (0 after a successful fetch). With no copy yet, the fetch error is raised as before."""

    def __init__(self, url: str, cache_path: Path, fetch: Callable[[str], str] = fetch_ics,
                 clock: Callable[[], float] = time_now):
        self.url, self.cache_path, self.fetch, self.clock = url, cache_path, fetch, clock
        self.stale_s = 0.0

    def __call__(self) -> str:
        try:
            text = self.fetch(self.url)
        except OSError as exc:  # HTTPError, URLError and timeouts are all OSError
            status = getattr(exc, "code", None)
            if not self.cache_path.exists():
                _event_log("calendar_fetch_failed", status=status, error=type(exc).__name__, cache=False)
                raise
            self.stale_s = max(0.0, self.clock() - self.cache_path.stat().st_mtime)
            _event_log("calendar_fetch_failed", status=status, error=type(exc).__name__, cache=True,
                       cache_age_s=int(self.stale_s))
            return self.cache_path.read_text()
        tmp = self.cache_path.with_suffix(f".{os.getpid()}.tmp")  # the MCP server and cron jobs may both write
        tmp.write_text(text)
        tmp.replace(self.cache_path)
        self.stale_s = 0.0
        return text


def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        elif raw:
            lines.append(raw)
    return lines


def _split(line: str) -> tuple[str, dict[str, str], str]:
    head, _, value = line.partition(":")
    name, *params = head.split(";")
    return name.upper(), dict(p.split("=", 1) for p in params if "=" in p), value


def _unescape(value: str) -> str:
    return value.replace("\\n", "\n").replace("\\N", "\n").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")


def _when(value: str, params: dict[str, str], tz: ZoneInfo) -> tuple[datetime, bool]:
    value = value.strip()
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        d = datetime.strptime(value, "%Y%m%d").date()
        return datetime.combine(d, time.min, tz), True
    if value.endswith("Z"):
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).astimezone(tz), False
    zone = ZoneInfo(params["TZID"]) if "TZID" in params else tz
    return datetime.strptime(value, "%Y%m%dT%H%M%S").replace(tzinfo=zone).astimezone(tz), False


def _expand(event: Event, rule: str, exdates: set[datetime], tz: ZoneInfo, horizon: datetime) -> list[Event]:
    parts = dict(p.split("=", 1) for p in rule.split(";") if "=" in p)
    freq = parts.get("FREQ")
    if freq not in ("DAILY", "WEEKLY") or set(parts) - {"FREQ", "INTERVAL", "COUNT", "UNTIL", "BYDAY", "WKST"}:
        return [replace(event, recurrence_unsupported=True)]
    interval = int(parts.get("INTERVAL", 1))
    count = int(parts["COUNT"]) if "COUNT" in parts else None
    until = _when(parts["UNTIL"], {}, tz)[0] if "UNTIL" in parts else None
    length = event.end - event.start
    days = sorted(WEEKDAYS[d[-2:]] for d in parts.get("BYDAY", "").split(",") if d) if freq == "WEEKLY" else []
    out: list[Event] = []
    seen = 0
    step = 0
    while seen < MAX_OCCURRENCES:
        if freq == "DAILY":
            candidates = [event.start + timedelta(days=step * interval)]
        else:
            week_start = event.start - timedelta(days=event.start.weekday()) + timedelta(weeks=step * interval)
            candidates = [week_start + timedelta(days=d) for d in (days or [event.start.weekday()])]
        step += 1
        for local in candidates:
            # Re-anchor wall-clock time so DST changes keep "10:00" at 10:00.
            start = datetime.combine(local.date(), event.start.timetz().replace(tzinfo=None), tz)
            if start < event.start:
                continue
            if (until and start > until) or start > horizon or (count is not None and seen >= count):
                return out
            seen += 1
            if start not in exdates:
                out.append(replace(event, start=start, end=start + length))
    return out


def parse_ics(text: str, tz_name: str = "America/Los_Angeles", *, horizon_days: int = 120,
              now: datetime | None = None) -> list[Event]:
    tz = ZoneInfo(tz_name)
    horizon = (now or datetime.now(tz)) + timedelta(days=horizon_days)
    events: list[Event] = []
    overrides: dict[tuple[str, datetime], Event] = {}
    current: dict | None = None
    for line in _unfold(text):
        name, params, value = _split(line)
        if name == "BEGIN" and value == "VEVENT":
            current = {"exdates": set()}
        elif name == "END" and value == "VEVENT" and current is not None:
            if "start" in current:
                start, all_day = current["start"]
                end = current.get("end", (start + (timedelta(days=1) if all_day else timedelta(hours=1)), all_day))[0]
                ev = Event(current.get("uid", ""), current.get("summary", "(no title)"), start, end, all_day,
                           current.get("location", ""), current.get("status") == "CANCELLED")
                if "recurrence_id" in current:
                    overrides[(ev.uid, current["recurrence_id"])] = ev
                elif "rrule" in current:
                    events += _expand(ev, current["rrule"], current["exdates"], tz, horizon)
                else:
                    events.append(ev)
            current = None
        elif current is not None:
            if name == "DTSTART":
                current["start"] = _when(value, params, tz)
            elif name == "DTEND":
                current["end"] = _when(value, params, tz)
            elif name == "SUMMARY":
                current["summary"] = _unescape(value)
            elif name == "LOCATION":
                current["location"] = _unescape(value)
            elif name == "UID":
                current["uid"] = value
            elif name == "STATUS":
                current["status"] = value.upper()
            elif name == "RRULE":
                current["rrule"] = value
            elif name == "EXDATE":
                current["exdates"] |= {_when(v, params, tz)[0] for v in value.split(",")}
            elif name == "RECURRENCE-ID":
                current["recurrence_id"] = _when(value, params, tz)[0]
    # A moved or cancelled single occurrence replaces the generated one.
    merged = [overrides.pop((e.uid, e.start), e) for e in events]
    merged += overrides.values()
    return sorted((e for e in merged if not e.cancelled), key=lambda e: e.start)


def between(events: list[Event], start: datetime, end: datetime) -> list[Event]:
    return [e for e in events if e.start < end and e.end > start]


def conflicts(events: list[Event], start: datetime, end: datetime) -> list[Event]:
    """Timed events overlapping [start, end). All-day events (holidays, reminders) do not block."""
    return [e for e in between(events, start, end) if not e.all_day]


def free_slots(events: list[Event], day: date, tz_name: str = "America/Los_Angeles", *,
               work_start: time = time(9), work_end: time = time(18), min_minutes: int = 30,
               buffer_minutes: int = 15) -> list[tuple[datetime, datetime]]:
    tz = ZoneInfo(tz_name)
    window_start = datetime.combine(day, work_start, tz)
    window_end = datetime.combine(day, work_end, tz)
    pad = timedelta(minutes=buffer_minutes)
    busy = sorted((max(e.start - pad, window_start), min(e.end + pad, window_end))
                  for e in conflicts(events, window_start, window_end))
    slots, cursor = [], window_start
    for b_start, b_end in busy:
        if b_start - cursor >= timedelta(minutes=min_minutes):
            slots.append((cursor, b_start))
        cursor = max(cursor, b_end)
    if window_end - cursor >= timedelta(minutes=min_minutes):
        slots.append((cursor, window_end))
    return slots
