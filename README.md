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
| `docs/architecture.html` | System architecture and flows (design draft v1) |

## Status

- [x] Router spike: cloud, local (with tool calls), simulated 429 and fail-closed hold
  verified live on the laptop. See `router/README.md`.
- [ ] Skills, scheduler, Cloud Run Instance deployment, 48-hour run.
