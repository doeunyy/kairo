# Kairo

*Kairos (καιρός): the opportune moment.*

Kairo is a proactive job-search assistant. It turns scattered recruiting emails into
application status and next actions, and alerts at the moments that matter: an interview
in 30 minutes, a reply deadline, a follow-up that is due.

CSCI 599 (Agentic AI), Fall 2026, Assignment 3. Built on
[Hermes Agent](https://github.com/NousResearch/hermes-agent) v0.21.5 (`v2026.9.24`), with
a cloud model through ChatGPT OAuth and local Gemma 4 on Ollama. All accounts and data
used here are dedicated test accounts with synthetic content.

## Layout

| Path | Contents |
|---|---|
| `router/` | Task-aware hybrid LLM router (Hermes Codex endpoint → ChatGPT or local Gemma) |
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
- [ ] Tailscale Serve/ACL, Cloud Run Instance deployment, backup/restart, 48-hour run.

## Run on the laptop

```bash
# once: Hermes pinned, with MCP client support
git clone --depth 1 --branch v2026.9.24 https://github.com/NousResearch/hermes-agent.git ~/.local/share/kairo/hermes-agent
(cd ~/.local/share/kairo/hermes-agent && uv sync --frozen --python 3.13 --extra messaging --extra mcp)
(cd core && uv sync --extra mcp) && (cd router && uv sync)
hermes/install.sh && hermes/cron-jobs.sh      # SOUL.md, cron wrappers, six jobs

# processes (each in its own terminal)
(cd router && A3_LOCAL_MODEL=gemma4:e4b A3_STATE_DIR=../state uv run uvicorn a3_router.app:app --port 8787)
(cd core && KAIRO_STATE_DIR=../state .venv/bin/python -m kairo_core watch)      # IMAP IDLE + ✅ approvals
(cd core && .venv/bin/python -m kairo_core.launch gateway)                      # Hermes + Discord + cron
```

`~/.a3-kairo/config.yaml` needs the `mcp_servers.kairo`, `skills.external_dirs`,
`agent.disabled_toolsets` and `timezone` entries described in `hermes/README.md`.
