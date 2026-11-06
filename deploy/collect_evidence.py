#!/usr/bin/env python3
"""Collect run evidence for a time window into state/evidence/<label>/ (git-ignored).

    deploy/collect_evidence.py --since 2026-11-06T22:30:00Z --until 2026-11-07T16:30:00Z --label soak1

Sources: Cloud Logging (entrypoint / snapshot / supervisor events, router decisions, mail
sweeps, approvals) and the state bucket (lifecycle logs, and cron execution history read from
the newest snapshot inside the window). Writes sanitized JSONL per category plus summary.md.
The snapshot itself is never copied into the export; it is private state.
"""

from __future__ import annotations

import argparse
import collections
import io
import json
import re
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MASKS = [
    (re.compile(r"[A-Za-z0-9._%+-]+@(?!northwind\.test|cardinal-robotics\.test|kairo\.test|kairo-synthetic\.test)"
                r"[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "<email>"),
    (re.compile(r"\b\d{17,20}\b"), "<discord-id>"),
    (re.compile(r"\b100\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"), "<tailnet-ip>"),
    (re.compile(r"tskey-[A-Za-z0-9-]+"), "<tailscale-key>"),
]


def sanitize(text: str) -> str:
    for pattern, repl in MASKS:
        text = pattern.sub(repl, text)
    return text


def gcloud_json(args: list[str]) -> list:
    out = subprocess.run(["gcloud", *args, "--format=json"], capture_output=True, text=True, check=True).stdout
    return json.loads(out or "[]")


def logs(project: str, since: str, until: str, extra: str) -> list[dict]:
    flt = (f'resource.type="cloud_run_instance" AND timestamp>="{since}" AND timestamp<="{until}" AND ({extra})')
    rows = gcloud_json(["logging", "read", flt, "--project", project, "--limit", "100000", "--order", "asc"])
    out = []
    for e in rows:
        payload = e.get("jsonPayload") or {"text": e.get("textPayload", "")}
        out.append({"ts": e["timestamp"], **payload})
    return out


def in_window(ts: str, since: str, until: str) -> bool:
    return since <= ts.replace("+00:00", "Z") <= until


def bucket_jsonl(bucket: str, path: str, since: str, until: str) -> list[dict]:
    try:
        text = subprocess.run(["gcloud", "storage", "cat", f"gs://{bucket}/{path}"], capture_output=True,
                              text=True, check=True).stdout
    except subprocess.CalledProcessError:
        return []
    rows = [json.loads(l) for l in text.splitlines() if l.strip()]
    return [r for r in rows if in_window(r.get("ts", ""), since, until)]


def cron_history(bucket: str, since: str, until: str) -> tuple[list[dict], dict]:
    """Executions in the window, from the newest snapshot taken at or before ``until``."""
    listing = subprocess.run(["gcloud", "storage", "ls", f"gs://{bucket}/snapshots/"], capture_output=True,
                             text=True, check=True).stdout.split()
    stamp = lambda u: re.search(r"snap-(\d{8}T\d{6}Z)", u)
    until_key = until.replace("-", "").replace(":", "")[:15] + "Z"
    snaps = sorted(u for u in listing if stamp(u) and stamp(u).group(1) <= until_key)
    if not snaps:
        return [], {}
    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / "snap.tgz"
        subprocess.run(["gcloud", "storage", "cp", snaps[-1], str(local), "-q"], check=True)
        with tarfile.open(local) as tar:
            names = {}
            try:
                jobs = json.load(tar.extractfile("hermes/cron/jobs.json"))
                for j in (jobs["jobs"] if isinstance(jobs, dict) and "jobs" in jobs else jobs):
                    names[j["id"]] = j.get("name")
            except KeyError:
                pass
            db_path = Path(tmp) / "executions.db"
            db_path.write_bytes(tar.extractfile("hermes/cron/executions.db").read())
        db = sqlite3.connect(db_path)
        db.row_factory = sqlite3.Row
        rows = [dict(r) for r in db.execute("SELECT job_id, source, status, started_at, finished_at, error, "
                                             "delivery_outcome, scheduled_instant FROM executions")]
    def utc(ts):
        return datetime.fromisoformat(ts).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if ts else ""
    out = []
    for r in rows:
        r["started_utc"] = utc(r["started_at"])
        if in_window(r["started_utc"], since, until):
            r["job"] = names.get(r["job_id"], r["job_id"])
            out.append(r)
    return sorted(out, key=lambda r: r["started_utc"]), {"snapshot": snaps[-1].rsplit("/", 1)[-1]}


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(sanitize(json.dumps(r, ensure_ascii=False)) + "\n" for r in rows))


def summarize(w: dict, since: str, until: str, heartbeat_s: int) -> str:
    hb = [r for r in w["lifecycle"] if r.get("component") == "supervisor" and r.get("event") == "heartbeat"]
    status = collections.Counter(r.get("status") for r in hb)
    mem = [r["mem_used_mb"] for r in hb if "mem_used_mb" in r]
    gaps = []
    for a, b in zip(hb, hb[1:]):
        delta = (datetime.fromisoformat(b["ts"].replace("Z", "+00:00")) -
                 datetime.fromisoformat(a["ts"].replace("Z", "+00:00"))).total_seconds()
        if delta > 2.5 * heartbeat_s:
            gaps.append(f"{a['ts']} → {b['ts']} ({int(delta)}s)")
    probes = [r for r in w["lifecycle"] if r.get("event") == "inference_probe"]
    ev = collections.Counter(r.get("event") for r in w["lifecycle"] if r.get("component") in ("entrypoint", "supervisor", "snapshot"))
    routes = collections.Counter((r.get("route") or r.get("decision"), r.get("reason"), r.get("outcome")) for r in w["routing"])
    cron = collections.defaultdict(collections.Counter)
    for r in w["cron"]:
        cron[r["job"]][r["status"]] += 1
    sweeps = [r for r in w["mail"] if "SweepReport" in r.get("text", "")]
    fetched = sum(int(m.group(1)) for r in sweeps for m in [re.search(r"fetched=(\d+)", r["text"])] if m)
    lines = [f"# Run evidence {since} → {until}", "",
             "## Health", f"- heartbeats: {len(hb)} ({dict(status)})",
             f"- heartbeat gaps > {2.5 * heartbeat_s:.0f}s: {len(gaps)}" + "".join(f"\n  - {g}" for g in gaps),
             f"- container memory MB: max {max(mem) if mem else 'n/a'}, last {mem[-1] if mem else 'n/a'}",
             f"- inference probes: {sum(1 for p in probes if p.get('ok'))}/{len(probes)} ok, "
             f"median latency {sorted(p.get('latency_ms', 0) for p in probes)[len(probes)//2] if probes else 'n/a'} ms",
             "", "## Lifecycle events", *[f"- {k}: {v}" for k, v in sorted(ev.items())],
             "", "## Routing (route, reason, outcome)", *[f"- {k}: {v}" for k, v in routes.most_common()],
             "", "## Scheduled jobs", *[f"- {job}: {dict(c)}" for job, c in sorted(cron.items())],
             f"- source snapshot: {w.get('cron_meta', {}).get('snapshot', 'n/a')}",
             "", "## Mail", f"- sweeps: {len(sweeps)}, messages fetched: {fetched}",
             f"- approvals: {sum(1 for r in w['mail'] if 'approval' in r.get('text', ''))}"]
    return sanitize("\n".join(lines)) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True)
    ap.add_argument("--until", default=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    ap.add_argument("--label", required=True)
    ap.add_argument("--project", default="kairo-dk2026")
    ap.add_argument("--heartbeat-seconds", type=int, default=300)
    a = ap.parse_args()
    bucket = f"{a.project}-kairo-state"
    out = ROOT / "state" / "evidence" / a.label
    out.mkdir(parents=True, exist_ok=True)
    w = {
        "lifecycle": logs(a.project, a.since, a.until,
                          'jsonPayload.component="entrypoint" OR jsonPayload.component="supervisor" '
                          'OR jsonPayload.component="snapshot"'),
        "routing": logs(a.project, a.since, a.until, 'jsonPayload.route:* OR jsonPayload.decision="hold"'),
        "mail": logs(a.project, a.since, a.until, 'textPayload:"SweepReport" OR textPayload:"approval"'),
        "bucket_events": bucket_jsonl(bucket, "lifecycle/events.jsonl", a.since, a.until),
        "bucket_heartbeat": bucket_jsonl(bucket, "lifecycle/heartbeat.jsonl", a.since, a.until),
    }
    w["cron"], w["cron_meta"] = cron_history(bucket, a.since, a.until)
    for name in ("lifecycle", "routing", "mail", "bucket_events", "bucket_heartbeat", "cron"):
        write_jsonl(out / f"{name}.jsonl", w[name])
    (out / "summary.md").write_text(summarize(w, a.since, a.until, a.heartbeat_seconds))
    print(f"wrote {out.relative_to(ROOT)}: " + ", ".join(f"{k}={len(v)}" for k, v in w.items() if isinstance(v, list)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
