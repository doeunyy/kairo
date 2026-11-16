# Personalization profile

All personalization data is synthetic. The user is the fictional **Emily Kim**; no real person's
details are used anywhere.

## Files

| File | Hermes role | What it holds |
|---|---|---|
| [`hermes/SOUL.md`](../hermes/SOUL.md) | Identity and rules, loaded into every session | Who Kairo is and who it serves, English-only output, Korean job-search vocabulary (지원 = application, 과제 = assessment, …), which tool answers which question, and six rules: no sending, no status changes, external text is data, no secrets, exact dates for relative ones, use the profile |
| [`hermes/profile/USER.md`](../hermes/profile/USER.md) | User profile (`memories/USER.md`, the `user` memory target) | Emily's situation and goals, target roles and locations, strengths to highlight, interview hours, email tone, briefing style, commute |
| [`hermes/profile/MEMORY.md`](../hermes/profile/MEMORY.md) | Agent memory (`memories/MEMORY.md`) | Working notes: the test project name, where the tracker lives and how statuses may change, reminder habits, how to offer interview slots |
| [`hermes/skills/`](../hermes/skills) | Skills | Five skills written for this job search (README "Skills") |

Hermes stores memory entries separated by a line with `§`, and caps USER.md at 1,375 and
MEMORY.md at 2,200 characters. The seeds use 769 and 424; every entry passes Hermes' memory
content scanner (`tools/memory_tool_store._scan_memory_content`).

## How the profile is installed and how it grows

- `hermes/install.sh` (run on every container start) copies SOUL.md and adds any seed entry that
  is missing from `memories/USER.md` and `memories/MEMORY.md`. Entries Hermes learned or Emily
  changed are kept: nothing is overwritten or removed.
- During chats Hermes' `memory` tool can add or replace entries when Emily states a preference.
- The nightly `kairo-memory-consolidation` job (03:00 PT) reviews the last day of conversations
  and keeps only stable preferences Emily stated herself; it never saves email contents,
  recruiter details or anything an email or web page asked it to remember, and removes entries
  she contradicted (`hermes/cron-jobs.sh`).

## Where the profile shows up

| Preference | Used by |
|---|---|
| Interview hours, avoid Mon/Wed standup and Friday afternoons | `interview-scheduling` when proposing slots; follow-up drafts with three times |
| Email tone, sign as "Emily" | `recruiting-email` drafts (each still needs ✅) |
| Briefing style, most urgent first | Morning Briefing and End-of-Day recap prompts |
| Metro and walking, leave-by time | `interview-weather` and on-site interview days in the briefing |
| Target roles and strengths | `job-tracker` JD summaries and gap notes, `company-research` |

## Persistence

Memory files live in `$HERMES_HOME/memories/` on the container disk and are included in every
state snapshot (every 15 minutes and before each restart) in the Cloud Storage bucket, then
restored at start. The entry "Emily's test project name is Project Cardinal." was saved through the
web chat on 2026-11-06; it is still in the 2026-11-16 snapshot after the 48-hour run's 16 planned
restarts and later deploys.
