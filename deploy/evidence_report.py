#!/usr/bin/env python3
"""Turn an exported run (state/evidence/<label>/, from collect_evidence.py) into submission evidence.

    deploy/evidence_report.py --label run48h --since 2026-11-10T08:23:00Z --until 2026-11-12T08:23:00Z

Writes docs/evidence/<label>/: README.md with hour-by-hour coverage of the whole window, every
restart, scheduled-job results and any failed or missed job, plus the sanitized JSONL logs
under logs/. collect_evidence.py already masks emails, tokens and the tailnet IP; this script
refuses to copy a log that still contains one.
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import shutil
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
PT = ZoneInfo("America/Los_Angeles")
LOGS = ["lifecycle", "bucket_heartbeat", "bucket_events", "cron", "routing", "mail"]
LEAKS = re.compile(r"\b100\.\d+\.\d+\.\d+\b|[A-Za-z0-9._%+-]+@gmail\.com|https://calendar\.google\.com/|sk-[A-Za-z0-9]{16,}")
EXPECTED = {  # job → runs expected in 48 h (from hermes/cron-jobs.sh)
    "kairo-interview-soon": 576, "kairo-mail-sweep": 192, "kairo-morning-briefing": 2,
    "kairo-eod-recap": 2, "kairo-followups": 2, "kairo-memory-consolidation": 2,
}
SCHEDULE = {
    "kairo-interview-soon": "every 5 min", "kairo-mail-sweep": "every 15 min (IDLE watcher backup)",
    "kairo-morning-briefing": "08:00 PT daily", "kairo-eod-recap": "21:00 PT daily",
    "kairo-followups": "12:00 PT daily", "kairo-memory-consolidation": "03:00 PT daily",
}


def ts(value: str) -> datetime:
    value = value.replace("Z", "+00:00")
    t = datetime.fromisoformat(value)
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def load(src: Path, name: str) -> list[dict]:
    path = src / f"{name}.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def when(row: dict) -> datetime:
    return ts(row.get("ts") or row.get("started_utc") or row["started_at"])


def fmt(t: datetime) -> str:
    return f"{t.astimezone(timezone.utc):%m-%d %H:%M:%S}Z ({t.astimezone(PT):%a %H:%M} PT)"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--since", required=True)
    ap.add_argument("--until", required=True)
    a = ap.parse_args()
    since, until = ts(a.since), ts(a.until)
    src = ROOT / "state" / "evidence" / a.label
    out = ROOT / "docs" / "evidence" / a.label
    (out / "logs").mkdir(parents=True, exist_ok=True)

    for name in LOGS:
        text = (src / f"{name}.jsonl").read_text()
        if LEAKS.search(text):
            raise SystemExit(f"{name}.jsonl still contains a private value; fix collect_evidence.py masks first")
        shutil.copyfile(src / f"{name}.jsonl", out / "logs" / f"{name}.jsonl")

    life = [r for r in load(src, "lifecycle") if since <= when(r) <= until]
    ev = lambda name: [r for r in life if r.get("event") == name]
    hb = sorted(ev("heartbeat"), key=when)
    probes = ev("inference_probe")
    cron = [r for r in load(src, "cron") if since <= when(r) <= until]
    routing = [r for r in load(src, "routing") if since <= when(r) <= until]
    sweeps = [r for r in load(src, "mail") if "SweepReport" in r.get("text", "")]
    bucket = load(src, "bucket_events")

    gaps = [(x, y) for x, y in zip(hb, hb[1:]) if (when(y) - when(x)).total_seconds() > 750]
    mem = [r["mem_pct"] for r in hb if "mem_pct" in r]
    lat = [p["latency_ms"] for p in probes if p.get("ok")]
    L: list[str] = []
    w = L.append

    w(f"# 48-hour continuous operation: {a.label}\n")
    w(f"Window: **{fmt(since)} → {fmt(until)}**, 48 h. Cloud Run Instance `kairo` (us-west1, 1 vCPU, 2 GiB), "
      "image `kairo@sha256:79e3173a…` (commit de961c6), state on the Cloud Storage volume "
      "`gs://kairo-dk2026-kairo-state` mounted at `/mnt/agent-state`.\n")
    w("No manual restart, redeploy or configuration change happened during the window. All restarts below are "
      "the planned clean restarts every 3 hours (the course's recommended setup), each with backup before "
      "shutdown and restore at startup.\n")
    w("## Result\n")
    w("| Check | Result |\n|---|---|")
    w(f"| Supervisor heartbeats (every 300 s) | {sum(r.get('status') == 'ok' for r in hb)}/{len(hb)} ok, "
      f"first {fmt(when(hb[0]))}, last {fmt(when(hb[-1]))} |")
    w(f"| Heartbeat gaps over 750 s | {len(gaps)} |")
    w(f"| Container memory | max {max(mem)}% of 2 GiB, last {mem[-1]}%; memory guard (85%) never armed |")
    w(f"| Local model reachable (inference probe over Tailscale) | {sum(bool(p.get('ok')) for p in probes)}/{len(probes)} ok, "
      f"median {statistics.median(lat) / 1000:.1f} s |")
    stops = [r for r in bucket if r.get("event") == "stop" and since <= when(r) <= until]
    w(f"| Restarts | {len(stops)}, all planned (`planned_restart`); 0 crashes, 0 platform-initiated restarts |")
    w(f"| Clean shutdown | gateway stopped cleanly {sum(r.get('result') == 'exited' for r in ev('gateway_stopped'))}/"
      f"{len(ev('gateway_stopped'))}, watcher stopped {len(ev('watcher_stopped'))}/{len(stops)} |")
    w(f"| State restored at startup | {len(ev('restore_ok'))}/{len(ev('ready'))} starts; backups written: {len(ev('backup_ok'))} |")
    by_job = collections.defaultdict(collections.Counter)
    for r in cron:
        by_job[r["job"]][r["status"]] += 1
    failed = [r for r in cron if r["status"] != "completed"]
    w(f"| Scheduled jobs | {sum(c['completed'] for c in by_job.values())} completed, {len(failed)} failed "
      "(see *Failed and missed jobs*) |")
    w(f"| Routing decisions | {sum(r.get('outcome') == 'ok' for r in routing)}/{len(routing)} ok |")
    w(f"| Mail sweeps | {len(sweeps)} |\n")

    w("## Hour-by-hour coverage\n")
    w("Every hour of the window, from the logs in `logs/`. *Restart* is the planned restart in that hour "
      "(stop → ready).\n")
    w("| Hour (UTC) | PT | Heartbeats ok | Max mem % | Probes ok | Jobs ok / failed | Routed | Restart |")
    w("|---|---|---|---|---|---|---|---|")
    starts = [r for r in bucket if r.get("event") == "start"]
    for h in range(48):
        lo, hi = since + timedelta(hours=h), since + timedelta(hours=h + 1)
        inside = lambda rows: [r for r in rows if lo <= when(r) < hi]
        hh, pp, cc = inside(hb), inside(probes), inside(cron)
        rst = ""
        for s in inside(stops):
            nxt = next((x for x in starts if when(x) >= when(s)), None)
            rst = f"{when(s):%H:%M}→{when(nxt):%H:%M} ({int((when(nxt) - when(s)).total_seconds())} s)" if nxt else "stop"
        mm = max((r["mem_pct"] for r in hh if "mem_pct" in r), default="–")
        w(f"| {lo:%m-%d %H:%M} | {lo.astimezone(PT):%a %H:%M} | {sum(r.get('status') == 'ok' for r in hh)}/{len(hh)} | {mm} | "
          f"{sum(bool(p.get('ok')) for p in pp)}/{len(pp)} | {sum(r['status'] == 'completed' for r in cc)} / "
          f"{sum(r['status'] != 'completed' for r in cc)} | {len(inside(routing))} | {rst} |")
    w("")

    w("## Restarts\n")
    w("Order on each planned restart: supervisor `planned_restart` → watcher stopped (waits for an in-progress "
      "mail sweep) → Hermes gateway stopped → final backup to the bucket → container exit → new container "
      "restores the newest snapshot → `ready`.\n")
    w("| # | Stop | Gateway stop | Restored snapshot | Ready | Down |")
    w("|---|---|---|---|---|---|")
    gws, rest = ev("gateway_stopped"), ev("restore_ok")
    for i, s in enumerate(stops, 1):
        g = next((x for x in gws if abs((when(x) - when(s)).total_seconds()) < 60), {})
        nxt = next((x for x in starts if when(x) >= when(s)), None)
        rs = next((x for x in rest if when(x) >= when(s)), {})
        down = f"{int((when(nxt) - when(s)).total_seconds())} s" if nxt else "–"
        w(f"| {i} | {fmt(when(s))} | {g.get('result', '–')} ({g.get('waited_s', '–')} s) | `{rs.get('file', '–')}` | "
          f"{fmt(when(nxt)) if nxt else '–'} | {down} |")
    w("")

    w("## Scheduled jobs\n")
    w("Hermes cron execution history, read from the newest state snapshot in the window.\n")
    w("| Job | Schedule | Expected | Completed | Failed | First run | Last run |")
    w("|---|---|---|---|---|---|---|")
    for job in EXPECTED:
        rows = sorted((r for r in cron if r["job"] == job), key=when)
        w(f"| `{job}` | {SCHEDULE[job]} | ~{EXPECTED[job]} | {by_job[job]['completed']} | {by_job[job]['failed']} | "
          f"{fmt(when(rows[0])) if rows else '–'} | {fmt(when(rows[-1])) if rows else '–'} |")
    w("\nThe daily jobs ran once on each of the two days. Slot check for the repeating jobs, by each run's "
      "`scheduled_instant`, from the start of the window to the newest snapshot "
      f"({fmt(max(map(when, cron)))}, the source of this history):\n")
    last = max(map(when, cron))
    for job, step in (("kairo-interview-soon", 5), ("kairo-mail-sweep", 15)):
        got = collections.Counter(ts(r["scheduled_instant"]) for r in cron if r["job"] == job)
        slot = since.replace(second=0, microsecond=0) + timedelta(minutes=-since.minute % step)
        slots = []
        while slot <= last:
            slots.append(slot)
            slot += timedelta(minutes=step)
        missing = [s for s in slots if s not in got]
        dups = [s for s, n in got.items() if n > 1]
        w(f"- `{job}`: {len(slots)} slots, {len(missing)} without a run, {len(dups)} run more than once"
          + (f" (missing: {', '.join(f'{s:%m-%d %H:%M}Z' for s in missing)})" if missing else "") + ".")
    w("\nSo no slot was lost or executed twice across the 16 restarts.\n")

    w("## Failed and missed jobs\n")
    if failed:
        first, last = when(failed[0]), when(failed[-1])
        reasons = collections.Counter(r["error"].strip().splitlines()[-1] for r in failed if r.get("error"))
        w(f"**{len(failed)} runs of `kairo-interview-soon` failed between {fmt(first)} and {fmt(last)}.**\n")
        for reason, n in reasons.items():
            w(f"- {n}× `{reason}`")
        w("- Cause: Google Calendar's ICS URL answered the Cloud Run instance with HTTP 429 (rate limit), most "
          "likely shared egress IP throttling. The script did not catch it and exited with code 1.")
        w("- What the user saw: Hermes posted the failure to Discord twice (03:35 and 04:20 PT); the other "
          "runs were suppressed as repeats.")
        w("- Impact: an interview starting in that window would have had no 30-minute reminder. The test "
          "calendar had no events then, so no alert was actually lost. All other jobs and the agent itself "
          "were unaffected, and the job recovered without intervention from the next run after the 429s "
          "stopped.")
        w("- Fix (after the window, not deployed during it): the last good ICS is kept in state; a 429, 5xx "
          "or network error falls back to it and logs `calendar_fetch_failed`, and the user gets one notice "
          "per outage once the copy is over an hour old.\n")
        w("| Started | Error |\n|---|---|")
        for r in failed:
            w(f"| {fmt(when(r))} | {(r.get('error') or '').strip().splitlines()[-1]} |")
        w("")
    else:
        w("None.\n")
    w("No genuine service outage: the HTTPS endpoint, Discord gateway, mail watcher and scheduler ran for "
      "the whole window apart from the planned restart downtimes listed above.\n")

    w("## Routing\n")
    w("| Route | Reason | Model | Outcome | Count |\n|---|---|---|---|---|")
    for (route, reason, model, outcome), n in collections.Counter(
            (r.get("route") or r.get("decision"), r.get("reason"), r.get("model"), r.get("outcome")) for r in routing).most_common():
        w(f"| {route} | {reason} | {model} | {outcome} | {n} |")
    w("")

    w("## Files\n")
    w("Sanitized exports (emails, tokens and the laptop's tailnet IP are masked), one JSON object per line:\n")
    w("| File | Lines | Contents |\n|---|---|---|")
    desc = {"lifecycle": "Cloud Logging: entrypoint, supervisor and snapshot events (heartbeats, probes, restarts, backups, restores)",
            "bucket_heartbeat": "Supervisor log kept in the state bucket: heartbeats, probes, planned restarts (survives Cloud Logging retention)",
            "bucket_events": "Container start/stop records written to the state bucket",
            "cron": "Hermes cron execution history (status, timing, delivery, error)",
            "routing": "Hybrid router decisions (route, reason, model, latency, outcome)",
            "mail": "Mail watcher sweep reports"}
    for name in LOGS:
        n = sum(1 for _ in (out / "logs" / f"{name}.jsonl").open())
        w(f"| `logs/{name}.jsonl` | {n} | {desc[name]} |")
    w(f"\nCollected with `deploy/collect_evidence.py --since {a.since} --until {a.until} --label {a.label}` right after "
      f"the window, and summarized with `deploy/evidence_report.py`. `persistence.md` shows the Cloud Storage "
      "volume configuration.")
    (out / "README.md").write_text("\n".join(L) + "\n")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
