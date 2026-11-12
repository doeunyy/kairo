# 48-hour continuous operation: run48h

Window: **11-10 08:23:00Z (Tue 00:23 PT) → 11-12 08:23:00Z (Thu 00:23 PT)**, 48 h. Cloud Run Instance `kairo` (us-west1, 1 vCPU, 2 GiB), image `kairo@sha256:79e3173a…` (commit de961c6), state on the Cloud Storage volume `gs://kairo-dk2026-kairo-state` mounted at `/mnt/agent-state`.

No manual restart, redeploy or configuration change happened during the window. All restarts below are the planned clean restarts every 3 hours (the course's recommended setup), each with backup before shutdown and restore at startup.

## Result

| Check | Result |
|---|---|
| Supervisor heartbeats (every 300 s) | 568/568 ok, first 11-10 08:24:32Z (Tue 00:24 PT), last 11-12 08:20:01Z (Thu 00:20 PT) |
| Heartbeat gaps over 750 s | 0 |
| Container memory | max 39.3% of 2 GiB, last 33%; memory guard (85%) never armed |
| Local model reachable (inference probe over Tailscale) | 88/88 ok, median 3.8 s |
| Restarts | 16, all planned (`planned_restart`); 0 crashes, 0 platform-initiated restarts |
| Clean shutdown | gateway stopped cleanly 16/16, watcher stopped 16/16 |
| State restored at startup | 16/16 starts; backups written: 199 |
| Scheduled jobs | 755 completed, 20 failed (see *Failed and missed jobs*) |
| Routing decisions | 122/122 ok |
| Mail sweeps | 193 |

## Hour-by-hour coverage

Every hour of the window, from the logs in `logs/`. *Restart* is the planned restart in that hour (stop → ready).

| Hour (UTC) | PT | Heartbeats ok | Max mem % | Probes ok | Jobs ok / failed | Routed | Restart |
|---|---|---|---|---|---|---|---|
| 11-10 08:23 | Tue 00:23 | 12/12 | 32.8 | 2/2 | 16 / 0 | 2 | 08:34→08:36 (91 s) |
| 11-10 09:23 | Tue 01:23 | 12/12 | 32.5 | 2/2 | 16 / 0 | 2 |  |
| 11-10 10:23 | Tue 02:23 | 12/12 | 38.2 | 2/2 | 17 / 0 | 7 |  |
| 11-10 11:23 | Tue 03:23 | 11/11 | 38.2 | 1/1 | 8 / 8 | 1 | 11:32→11:33 (85 s) |
| 11-10 12:23 | Tue 04:23 | 12/12 | 32.7 | 2/2 | 4 / 12 | 2 |  |
| 11-10 13:23 | Tue 05:23 | 12/12 | 32.8 | 2/2 | 16 / 0 | 2 |  |
| 11-10 14:23 | Tue 06:23 | 12/12 | 32.8 | 2/2 | 16 / 0 | 2 | 14:34→14:35 (90 s) |
| 11-10 15:23 | Tue 07:23 | 12/12 | 38.8 | 2/2 | 17 / 0 | 6 |  |
| 11-10 16:23 | Tue 08:23 | 12/12 | 38.9 | 2/2 | 16 / 0 | 2 |  |
| 11-10 17:23 | Tue 09:23 | 11/11 | 38.9 | 1/1 | 16 / 0 | 1 | 17:31→17:33 (72 s) |
| 11-10 18:23 | Tue 10:23 | 12/12 | 32.8 | 2/2 | 16 / 0 | 2 |  |
| 11-10 19:23 | Tue 11:23 | 12/12 | 38.9 | 2/2 | 17 / 0 | 7 |  |
| 11-10 20:23 | Tue 12:23 | 12/12 | 39 | 2/2 | 16 / 0 | 2 | 20:33→20:35 (78 s) |
| 11-10 21:23 | Tue 13:23 | 12/12 | 32.8 | 2/2 | 16 / 0 | 2 |  |
| 11-10 22:23 | Tue 14:23 | 12/12 | 32.9 | 2/2 | 16 / 0 | 2 |  |
| 11-10 23:23 | Tue 15:23 | 11/11 | 32.9 | 1/1 | 16 / 0 | 1 | 23:31→23:33 (111 s) |
| 11-11 00:23 | Tue 16:23 | 12/12 | 32.8 | 2/2 | 16 / 0 | 2 |  |
| 11-11 01:23 | Tue 17:23 | 12/12 | 33 | 2/2 | 16 / 0 | 2 |  |
| 11-11 02:23 | Tue 18:23 | 12/12 | 33 | 2/2 | 16 / 0 | 2 | 02:34→02:36 (97 s) |
| 11-11 03:23 | Tue 19:23 | 12/12 | 32.8 | 2/2 | 16 / 0 | 2 |  |
| 11-11 04:23 | Tue 20:23 | 12/12 | 39 | 2/2 | 17 / 0 | 6 |  |
| 11-11 05:23 | Tue 21:23 | 11/11 | 38.8 | 1/1 | 16 / 0 | 1 | 05:31→05:33 (103 s) |
| 11-11 06:23 | Tue 22:23 | 12/12 | 32.9 | 2/2 | 16 / 0 | 2 |  |
| 11-11 07:23 | Tue 23:23 | 12/12 | 33 | 2/2 | 16 / 0 | 2 |  |
| 11-11 08:23 | Wed 00:23 | 12/12 | 33 | 2/2 | 16 / 0 | 2 | 08:34→08:36 (82 s) |
| 11-11 09:23 | Wed 01:23 | 12/12 | 33 | 2/2 | 16 / 0 | 2 |  |
| 11-11 10:23 | Wed 02:23 | 12/12 | 38.5 | 2/2 | 17 / 0 | 7 |  |
| 11-11 11:23 | Wed 03:23 | 11/11 | 38.5 | 1/1 | 16 / 0 | 1 | 11:32→11:33 (91 s) |
| 11-11 12:23 | Wed 04:23 | 12/12 | 32.9 | 2/2 | 16 / 0 | 2 |  |
| 11-11 13:23 | Wed 05:23 | 12/12 | 32.9 | 2/2 | 16 / 0 | 2 |  |
| 11-11 14:23 | Wed 06:23 | 12/12 | 33.1 | 2/2 | 16 / 0 | 2 | 14:34→14:36 (94 s) |
| 11-11 15:23 | Wed 07:23 | 12/12 | 39.1 | 2/2 | 17 / 0 | 5 |  |
| 11-11 16:23 | Wed 08:23 | 12/12 | 39.3 | 2/2 | 16 / 0 | 2 |  |
| 11-11 17:23 | Wed 09:23 | 11/11 | 39.2 | 1/1 | 16 / 0 | 1 | 17:32→17:34 (96 s) |
| 11-11 18:23 | Wed 10:23 | 12/12 | 33 | 2/2 | 16 / 0 | 2 |  |
| 11-11 19:23 | Wed 11:23 | 12/12 | 39.2 | 2/2 | 17 / 0 | 7 |  |
| 11-11 20:23 | Wed 12:23 | 12/12 | 39 | 1/1 | 16 / 0 | 1 | 20:30→20:31 (84 s) |
| 11-11 21:23 | Wed 13:23 | 12/12 | 32.9 | 2/2 | 16 / 0 | 2 |  |
| 11-11 22:23 | Wed 14:23 | 12/12 | 33 | 2/2 | 16 / 0 | 2 |  |
| 11-11 23:23 | Wed 15:23 | 11/11 | 33.2 | 2/2 | 16 / 0 | 2 | 23:32→23:33 (83 s) |
| 11-12 00:23 | Wed 16:23 | 12/12 | 33 | 2/2 | 16 / 0 | 2 |  |
| 11-12 01:23 | Wed 17:23 | 12/12 | 33 | 2/2 | 16 / 0 | 2 |  |
| 11-12 02:23 | Wed 18:23 | 12/12 | 33 | 2/2 | 16 / 0 | 2 | 02:35→02:36 (93 s) |
| 11-12 03:23 | Wed 19:23 | 12/12 | 33 | 2/2 | 16 / 0 | 2 |  |
| 11-12 04:23 | Wed 20:23 | 12/12 | 39 | 2/2 | 17 / 0 | 5 |  |
| 11-12 05:23 | Wed 21:23 | 11/11 | 39 | 1/1 | 16 / 0 | 1 | 05:32→05:34 (77 s) |
| 11-12 06:23 | Wed 22:23 | 12/12 | 33 | 2/2 | 16 / 0 | 2 |  |
| 11-12 07:23 | Wed 23:23 | 12/12 | 33.1 | 2/2 | 15 / 0 | 2 |  |

## Restarts

Order on each planned restart: supervisor `planned_restart` → watcher stopped (waits for an in-progress mail sweep) → Hermes gateway stopped → final backup to the bucket → container exit → new container restores the newest snapshot → `ready`.

| # | Stop | Gateway stop | Restored snapshot | Ready | Down |
|---|---|---|---|---|---|
| 1 | 11-10 08:34:41Z (Tue 00:34 PT) | exited (1 s) | `snap-20261110T083439Z.tar.gz` | 11-10 08:36:12Z (Tue 00:36 PT) | 91 s |
| 2 | 11-10 11:32:10Z (Tue 03:32 PT) | exited (0 s) | `snap-20261110T113209Z.tar.gz` | 11-10 11:33:35Z (Tue 03:33 PT) | 85 s |
| 3 | 11-10 14:34:29Z (Tue 06:34 PT) | exited (1 s) | `snap-20261110T143427Z.tar.gz` | 11-10 14:35:59Z (Tue 06:35 PT) | 90 s |
| 4 | 11-10 17:31:48Z (Tue 09:31 PT) | exited (1 s) | `snap-20261110T173146Z.tar.gz` | 11-10 17:33:00Z (Tue 09:33 PT) | 72 s |
| 5 | 11-10 20:33:54Z (Tue 12:33 PT) | exited (1 s) | `snap-20261110T203353Z.tar.gz` | 11-10 20:35:12Z (Tue 12:35 PT) | 78 s |
| 6 | 11-10 23:31:16Z (Tue 15:31 PT) | exited (1 s) | `snap-20261110T233115Z.tar.gz` | 11-10 23:33:07Z (Tue 15:33 PT) | 111 s |
| 7 | 11-11 02:34:24Z (Tue 18:34 PT) | exited (1 s) | `snap-20261111T023423Z.tar.gz` | 11-11 02:36:01Z (Tue 18:36 PT) | 97 s |
| 8 | 11-11 05:31:59Z (Tue 21:31 PT) | exited (1 s) | `snap-20261111T053158Z.tar.gz` | 11-11 05:33:42Z (Tue 21:33 PT) | 103 s |
| 9 | 11-11 08:34:51Z (Wed 00:34 PT) | exited (1 s) | `snap-20261111T083450Z.tar.gz` | 11-11 08:36:13Z (Wed 00:36 PT) | 82 s |
| 10 | 11-11 11:32:14Z (Wed 03:32 PT) | exited (1 s) | `snap-20261111T113212Z.tar.gz` | 11-11 11:33:45Z (Wed 03:33 PT) | 91 s |
| 11 | 11-11 14:34:51Z (Wed 06:34 PT) | exited (0 s) | `snap-20261111T143450Z.tar.gz` | 11-11 14:36:25Z (Wed 06:36 PT) | 94 s |
| 12 | 11-11 17:32:29Z (Wed 09:32 PT) | exited (1 s) | `snap-20261111T173227Z.tar.gz` | 11-11 17:34:05Z (Wed 09:34 PT) | 96 s |
| 13 | 11-11 20:30:08Z (Wed 12:30 PT) | exited (1 s) | `snap-20261111T203006Z.tar.gz` | 11-11 20:31:32Z (Wed 12:31 PT) | 84 s |
| 14 | 11-11 23:32:36Z (Wed 15:32 PT) | exited (1 s) | `snap-20261111T233235Z.tar.gz` | 11-11 23:33:59Z (Wed 15:33 PT) | 83 s |
| 15 | 11-12 02:35:07Z (Wed 18:35 PT) | exited (1 s) | `snap-20261112T023506Z.tar.gz` | 11-12 02:36:40Z (Wed 18:36 PT) | 93 s |
| 16 | 11-12 05:32:49Z (Wed 21:32 PT) | exited (1 s) | `snap-20261112T053247Z.tar.gz` | 11-12 05:34:06Z (Wed 21:34 PT) | 77 s |

## Scheduled jobs

Hermes cron execution history, read from the newest state snapshot in the window.

| Job | Schedule | Expected | Completed | Failed | First run | Last run |
|---|---|---|---|---|---|---|
| `kairo-interview-soon` | every 5 min | ~576 | 555 | 20 | 11-10 08:25:36Z (Tue 00:25 PT) | 11-12 08:15:22Z (Thu 00:15 PT) |
| `kairo-mail-sweep` | every 15 min (IDLE watcher backup) | ~192 | 192 | 0 | 11-10 08:30:36Z (Tue 00:30 PT) | 11-12 08:15:22Z (Thu 00:15 PT) |
| `kairo-morning-briefing` | 08:00 PT daily | ~2 | 2 | 0 | 11-10 16:00:15Z (Tue 08:00 PT) | 11-11 16:00:44Z (Wed 08:00 PT) |
| `kairo-eod-recap` | 21:00 PT daily | ~2 | 2 | 0 | 11-11 05:00:21Z (Tue 21:00 PT) | 11-12 05:00:02Z (Wed 21:00 PT) |
| `kairo-followups` | 12:00 PT daily | ~2 | 2 | 0 | 11-10 20:00:17Z (Tue 12:00 PT) | 11-11 20:00:18Z (Wed 12:00 PT) |
| `kairo-memory-consolidation` | 03:00 PT daily | ~2 | 2 | 0 | 11-10 11:00:25Z (Tue 03:00 PT) | 11-11 11:00:35Z (Wed 03:00 PT) |

The daily jobs ran once on each of the two days. Slot check for the repeating jobs, by each run's `scheduled_instant`, from the start of the window to the newest snapshot (11-12 08:15:22Z (Thu 00:15 PT), the source of this history):

- `kairo-interview-soon`: 575 slots, 0 without a run, 0 run more than once.
- `kairo-mail-sweep`: 192 slots, 0 without a run, 0 run more than once.

So no slot was lost or executed twice across the 16 restarts.

## Failed and missed jobs

**20 runs of `kairo-interview-soon` failed between 11-10 11:35:55Z (Tue 03:35 PT) and 11-10 13:20:55Z (Tue 05:20 PT).**

- 20× `urllib.error.HTTPError: HTTP Error 429: Too Many Requests`
- Cause: Google Calendar's ICS URL answered the Cloud Run instance with HTTP 429 (rate limit), most likely shared egress IP throttling. The script did not catch it and exited with code 1.
- What the user saw: Hermes posted the failure to Discord twice (03:35 and 04:20 PT); the other runs were suppressed as repeats.
- Impact: an interview starting in that window would have had no 30-minute reminder. The test calendar had no events then, so no alert was actually lost. All other jobs and the agent itself were unaffected, and the job recovered without intervention from the next run after the 429s stopped.
- Fix (after the window, not deployed during it): the last good ICS is kept in state; a 429, 5xx or network error falls back to it and logs `calendar_fetch_failed`, and the user gets one notice per outage once the copy is over an hour old.

| Started | Error |
|---|---|
| 11-10 11:35:55Z (Tue 03:35 PT) | urllib.error.HTTPError: HTTP Error 429: Too Many Requests |
| 11-10 11:40:55Z (Tue 03:40 PT) | urllib.error.HTTPError: HTTP Error 429: Too Many Requests |
| 11-10 11:45:55Z (Tue 03:45 PT) | urllib.error.HTTPError: HTTP Error 429: Too Many Requests |
| 11-10 11:50:55Z (Tue 03:50 PT) | urllib.error.HTTPError: HTTP Error 429: Too Many Requests |
| 11-10 11:55:55Z (Tue 03:55 PT) | urllib.error.HTTPError: HTTP Error 429: Too Many Requests |
| 11-10 12:00:55Z (Tue 04:00 PT) | urllib.error.HTTPError: HTTP Error 429: Too Many Requests |
| 11-10 12:05:55Z (Tue 04:05 PT) | urllib.error.HTTPError: HTTP Error 429: Too Many Requests |
| 11-10 12:20:55Z (Tue 04:20 PT) | urllib.error.HTTPError: HTTP Error 429: Too Many Requests |
| 11-10 12:25:55Z (Tue 04:25 PT) | urllib.error.HTTPError: HTTP Error 429: Too Many Requests |
| 11-10 12:30:55Z (Tue 04:30 PT) | urllib.error.HTTPError: HTTP Error 429: Too Many Requests |
| 11-10 12:35:55Z (Tue 04:35 PT) | urllib.error.HTTPError: HTTP Error 429: Too Many Requests |
| 11-10 12:40:55Z (Tue 04:40 PT) | urllib.error.HTTPError: HTTP Error 429: Too Many Requests |
| 11-10 12:45:55Z (Tue 04:45 PT) | urllib.error.HTTPError: HTTP Error 429: Too Many Requests |
| 11-10 12:50:55Z (Tue 04:50 PT) | urllib.error.HTTPError: HTTP Error 429: Too Many Requests |
| 11-10 12:55:55Z (Tue 04:55 PT) | urllib.error.HTTPError: HTTP Error 429: Too Many Requests |
| 11-10 13:00:55Z (Tue 05:00 PT) | urllib.error.HTTPError: HTTP Error 429: Too Many Requests |
| 11-10 13:05:55Z (Tue 05:05 PT) | urllib.error.HTTPError: HTTP Error 429: Too Many Requests |
| 11-10 13:10:55Z (Tue 05:10 PT) | urllib.error.HTTPError: HTTP Error 429: Too Many Requests |
| 11-10 13:15:55Z (Tue 05:15 PT) | urllib.error.HTTPError: HTTP Error 429: Too Many Requests |
| 11-10 13:20:55Z (Tue 05:20 PT) | urllib.error.HTTPError: HTTP Error 429: Too Many Requests |

No genuine service outage: the HTTPS endpoint, Discord gateway, mail watcher and scheduler ran for the whole window apart from the planned restart downtimes listed above.

## Routing

| Route | Reason | Model | Outcome | Count |
|---|---|---|---|---|
| local | simple_task | gemma4:e4b | ok | 88 |
| cloud | reasoning_high | gpt-5.6-sol | ok | 32 |
| cloud | long_context | gpt-5.6-sol | ok | 2 |

## Files

Sanitized exports (emails, tokens and the laptop's tailnet IP are masked), one JSON object per line:

| File | Lines | Contents |
|---|---|---|
| `logs/lifecycle.jsonl` | 1223 | Cloud Logging: entrypoint, supervisor and snapshot events (heartbeats, probes, restarts, backups, restores) |
| `logs/bucket_heartbeat.jsonl` | 688 | Supervisor log kept in the state bucket: heartbeats, probes, planned restarts (survives Cloud Logging retention) |
| `logs/bucket_events.jsonl` | 32 | Container start/stop records written to the state bucket |
| `logs/cron.jsonl` | 775 | Hermes cron execution history (status, timing, delivery, error) |
| `logs/routing.jsonl` | 122 | Hybrid router decisions (route, reason, model, latency, outcome) |
| `logs/mail.jsonl` | 193 | Mail watcher sweep reports |

Collected with `deploy/collect_evidence.py --since 2026-11-10T08:23:00Z --until 2026-11-12T08:23:00Z --label run48h` right after the window, and summarized with `deploy/evidence_report.py`. `persistence.md` shows the Cloud Storage volume configuration.
