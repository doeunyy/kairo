# Kairo

*Kairos (καιρός): the opportune moment.*

Kairo is a proactive job-search assistant. It turns scattered recruiting emails into
application status and next actions, and alerts at the moments that matter: an interview
in 30 minutes, a reply deadline, a follow-up that is due.

CSCI 599 (Agentic AI), Fall 2026, Assignment 3. Built on
[Hermes Agent](https://github.com/NousResearch/hermes-agent) v0.22.7 (Python 3.14), with
a cloud model (`gpt-5.6-sol`) through ChatGPT OAuth and local Gemma 4 (`gemma4:e4b`) on Ollama. All accounts and data
used here are dedicated test accounts with synthetic content.

## Layout

| Path | Contents |
|---|---|
| `router/` | Task-aware hybrid LLM router (Hermes Codex endpoint → ChatGPT or local Gemma) |
| `deploy/` | Cloud Run Instance image, entrypoint, supervisor wiring, course web chat (`web_ui.py`), deploy scripts |
| `core/` | Mail pipeline (allowlist, strict extraction schema, matching, status rules, Message-ID ledger), adapters (Gmail, Notion, ICS, Discord), approval gate, Kairo MCP server, cron reports |
| `hermes/` | Hermes side: `SOUL.md`, four skills, cron wrapper template, `install.sh`, `cron-jobs.sh` |
| `docs/setup-accounts.md` | Creating the dedicated test accounts |
| `.env.example` | Every runtime setting, with placeholders only |

## Status

- [x] Router spike: cloud, local (with tool calls), simulated 429 and fail-closed hold
  verified live on the laptop. See `router/README.md`.
- [x] Mail pipeline core with tests for injection, ambiguity and duplicates (`core/`).
- [x] Adapters (`core/`): Gmail IMAP with IDLE and drafts, SMTP behind the Discord ✅ approval
  gate (HMAC token, recipient allowlist, audit log), version-aware Notion tracker, ICS calendar
  with free-slot suggestions, Discord alerts. Verified live with synthetic mail.
- [x] Hermes integration: Kairo MCP server (read tools + `draft_email` only; no send, status
  or approval tool), four skills, Discord gateway limited to `#kairo`, six cron jobs with quiet
  hours (22:00–07:30) and a sweep lock shared by the IDLE watcher and the 15-minute backup.
- [x] Cloud Run Instance deployment (Tailscale userspace to the laptop's Ollama, state bucket,
  snapshots, supervisor with staged recovery and planned restarts), preliminary soaks.
- [x] 48-hour run (2026-11-10 08:23Z → 11-12 08:23Z): 568/568 heartbeats, 16 planned restarts with
  state restored, no slot lost or run twice; 20 calendar-alert runs failed on a Google 429 and are
  documented. Evidence: [docs/evidence/run48h](docs/evidence/run48h/README.md).

## Grader access (web chat)

The Cloud Run URL serves the course's web chat (`deploy/web_ui.py`, standard library only) on
`$PORT` (8080). It reverse-proxies `/v1/*` and `/health` to the Hermes API server, which listens
on `127.0.0.1:8081` inside the container and still requires `API_SERVER_KEY` (Secret Manager;
submitted privately, never in this repo). `/health` reports the running Hermes version.

Approval-gated actions do not hang the web chat. Kairo has no send tool: the agent can only save a
draft (`mcp__kairo__draft_email`), which posts a preview to Discord `#kairo`. Sending happens only
after a ✅ reaction there from the allowed user, so a web-chat request to send an email returns at
once with "draft saved, approve it in Discord".

## Routing policy

Every model call from Hermes (chat, cron, auxiliary calls) goes through the router. Rules are
checked in order; the first match wins.

| # | Rule (`reason` in the log) | Route |
|---|---|---|
| 1 | `privacy_high`: a privacy tag, or a privacy word (confidential, private, personal, sensitive, password, bank, medical, salary, …) | local |
| 1a | `privacy_high_local_unavailable` / `privacy_high_too_long`: the same, but local Gemma is down or the prompt is too long for it | **held** |
| 2 | `rate_limit` / `rate_limit_simulated`: cloud returned 429 recently, or the simulation flag is set | local |
| 3 | `long_context`: estimated prompt above 40K tokens | cloud |
| 4 | `auxiliary_task`: Hermes-internal call (session title) | local |
| 5 | `reasoning_high`: a reasoning tag or word (step by step, explain why, compare, analyze, plan, …) | cloud |
| 6 | `agentic_prompt`: short turn about Kairo's data (applications, email, calendar, …) that needs the ~10K-token tool catalog | cloud |
| 7 | `simple_task`: short plain chat. Sent to Gemma **without** the tool catalog (`chat_only: true`) | local |
| 8 | `default`: anything else, or local Gemma is down | cloud |

**Private tasks while local Gemma is unavailable are held.** The router answers with a hold
message and sends nothing to any model (log: `route: held`, `model: null`). This is enforced in
code (`router/a3_router/policy.py`, `app.py`): a private turn is never retried in the cloud, also
when the local call fails mid-request. Non-private turns keep working through the cloud when the
laptop is off. The router re-checks local health every 15 s and logs each change.

### Routing log (Cloud Logging)

One line per model call, no message text: time, route (local / cloud / held), reason, model,
outcome, task, latency. `local_health` lines show when the router noticed Gemma going down or up.

```bash
gcloud logging read 'resource.type="cloud_run_instance" AND (jsonPayload.route:* OR jsonPayload.event="local_health")' \
  --project=kairo-dk2026 --freshness=30m --order=desc --limit=40 \
  --format='table(timestamp.date("%H:%M:%S"), jsonPayload.route, jsonPayload.reason, jsonPayload.model, jsonPayload.outcome, jsonPayload.features.task, jsonPayload.latency_ms, jsonPayload.local_up)'
```

A web-chat turn usually logs two lines: the reply itself (`task: chat`) and Hermes' session-title
call (`task: title_generation`).

## Restarts and memory

The instance runs with 1 vCPU and 2 GiB. During long runs the supervisor restarts the container at
:30 PT every 3 hours (the course's recommended setup): state is backed up first and restored at start,
and each restart is logged (`planned_restart`). Before the final backup the entrypoint waits for any
mail sweep in progress and stops the watcher, so no alert is lost between the two, then stops the
Hermes gateway (`gateway_stopped`), so the snapshot holds its clean exit.

For the grading period the interval drops to once a day at 03:30 PT, without a redeploy:

```bash
echo 24 | gcloud storage cp - gs://kairo-dk2026-kairo-state/flags/planned_every_h
gcloud storage rm gs://kairo-dk2026-kairo-state/flags/planned_every_h              # back to every 3 hours
```

Memory guard: if container memory reaches 85%, the supervisor restarts at the next :30 instead of
waiting for its slot (`memory_guard_armed`, then `planned_restart` with `memory_guard:<pct>%`).
In soak 2 memory grew about 11 MB an hour plus about 110 MB steps, so a container that runs a full
day on 1 GiB would have come close to the limit.

Mail, calendar and Notion use an app password, an ICS URL and an integration token, so there is
no Google OAuth token to renew.

The calendar is read every 5 minutes for interview alerts. In the 48-hour run Google answered the ICS
URL with HTTP 429 for about two hours, so each successful fetch is now saved to `state/calendar.ics`.
A failed fetch (429, 5xx, network) uses that copy and logs `calendar_fetch_failed` with its age;
if the copy is over an hour old, Kairo posts one notice per outage (held for the briefing in quiet hours).

## Advanced integration: skill tap

`job-tracker` is also published as a public Hermes skill tap,
[doeunyy/hermes-job-skills](https://github.com/doeunyy/hermes-job-skills), pinned at commit `e2f7035`.
When Kairo's MCP tools are present it uses Kairo's Notion tracker. On any other Hermes install it
falls back to a bundled standard-library script that keeps a local JSON file.

```bash
hermes skills tap add doeunyy/hermes-job-skills
hermes skills install doeunyy/hermes-job-skills/job-tracker --yes
```

[docs/evidence/skill-tap/](docs/evidence/skill-tap/README.md) has the transcript of a fresh Hermes
v0.22.7 install. It shows a new `HERMES_HOME` with its own login, tap add, install (security scan:
SAFE), and five chats that add, update and summarize applications.

## Run on the laptop

```bash
# once: Hermes pinned, with MCP client support
git clone --depth 1 --branch v0.22.7 https://github.com/NousResearch/hermes-agent.git ~/.local/share/kairo/hermes-agent
(cd ~/.local/share/kairo/hermes-agent && uv sync --frozen --python 3.14 --extra messaging --extra mcp)
(cd core && uv sync --extra mcp) && (cd router && uv sync)
hermes/install.sh && hermes/cron-jobs.sh      # SOUL.md, cron wrappers, six jobs

# processes (each in its own terminal)
(cd router && A3_LOCAL_MODEL=gemma4:e4b A3_STATE_DIR=../state uv run uvicorn a3_router.app:app --port 8787)
(cd core && KAIRO_STATE_DIR=../state .venv/bin/python -m kairo_core watch)      # IMAP IDLE + ✅ approvals
(cd core && .venv/bin/python -m kairo_core.launch gateway)                      # Hermes + Discord + cron
```

`~/.a3-kairo/config.yaml` needs the `mcp_servers.kairo`, `skills.external_dirs`,
`agent.disabled_toolsets` and `timezone` entries described in `hermes/README.md`.
