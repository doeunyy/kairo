# Hermes configuration for Kairo

`HERMES_HOME` is `~/.a3-kairo`. Secrets stay in `~/.a3-kairo/kairo.env`; `kairo_core.launch`
passes Hermes only the `DISCORD_*`, `HERMES_*` and `API_SERVER_*` values, and the Kairo MCP
server reads the mail, Notion and calendar secrets itself.

Add to `~/.a3-kairo/config.yaml` (paths are this checkout's):

```yaml
timezone: America/Los_Angeles

agent:
  # Untrusted email and web text reach the model, so it gets no shell, files, code execution,
  # browser or raw Discord actions, and cannot read kairo.env or the approval key.
  disabled_toolsets: [terminal, file, code_execution, browser, computer_use, delegation, discord,
                      discord_admin, image_gen, video_gen, tts, vision, homeassistant, spotify,
                      kanban, connections, x_search]

skills:
  external_dirs: [<repo>/kairo/hermes/skills]

mcp_servers:
  kairo:
    command: <repo>/kairo/core/.venv/bin/python
    args: ["-m", "kairo_core.mcp_server"]
    cwd: <repo>/kairo/core
    env: {KAIRO_STATE_DIR: <repo>/kairo/state}
    timeout: 60
    connect_timeout: 30
```

| File | Purpose |
|---|---|
| `SOUL.md` | Identity (fictional user Emily Kim), English-only output, vocabulary, tool rules; copied by `install.sh` |
| `skills/` | `job-tracker`, `recruiting-email`, `interview-scheduling`, `interview-weather` |
| `scripts/kairo-job.sh.in` | Cron wrapper template. Hermes runs only scripts inside `$HERMES_HOME/scripts` (symlinks are rejected), so `install.sh` copies one per job with absolute paths filled in |
| `cron-jobs.sh` | Registers the six jobs; skips names that already exist |

## Scheduled jobs (America/Los_Angeles)

| Job | Schedule | Kind | What it does |
|---|---|---|---|
| `kairo-morning-briefing` | 08:00 | agent + script | Today's events and free time, weather, last 24h of mail, deadlines, alerts deferred overnight |
| `kairo-eod-recap` | 21:00 | agent + script | What Kairo did today (from logs), decisions needed, tomorrow |
| `kairo-interview-soon` | every 5 min | script only | Interview starting within 30 min; each event alerts once |
| `kairo-followups` | 12:00 | agent + script | Due within 24h, interview requests unanswered for 24h (drafts a reply for approval), applications quiet for 10+ days; `[SILENT]` when nothing |
| `kairo-memory-consolidation` | 03:00 | agent | Saves only preferences the user stated; never email content |
| `kairo-mail-sweep` | every 15 min | script only | Backup sweep if IDLE missed mail; shares a lock with the watcher |

Hermes runs a missed slot once after a restart and locks each tick, so a job never runs twice
for the same slot. Mail alerts between 22:00 and 07:30 are deferred to the Morning Briefing
unless a deadline is within 12 hours; each deferral is logged with its reason.
