# Skill tap: fresh-install transcript

The `job-tracker` skill is published as a Hermes skill tap:

- Repo: https://github.com/doeunyy/hermes-job-skills (public, MIT)
- Pinned version: commit `e2f703571080ebca83d2e8aab32f8a618b120069`. Hermes' installer recorded the same
  commit in its source URL (`.../tree/e2f7035.../skills/job-tracker`).
- Hermes: v0.22.7, commit `b200f5bc`, the same as the deployment. It is a new git clone with its own venv.
- Recorded on 2026-11-14 15:55 PST.

## What the transcript shows

1. A new `HERMES_HOME` that holds only `config.yaml` (model `openai-codex` / `gpt-5.6-sol`,
   `adopt_external_logins: false`) and its own new ChatGPT sign-in. It has no skills and does not
   reuse Kairo's login.
2. `hermes skills tap add doeunyy/hermes-job-skills`, then `tap list`.
3. `hermes skills install doeunyy/hermes-job-skills/job-tracker --yes`. Hermes' security scan
   returned verdict SAFE, so the install was allowed. No `--force` and no direct SKILL.md URL.
4. Five chats. Each one is a new Hermes session, and the data carries over through the skill's
   JSON file:
   - Track a posting from a JD.
   - Track an application that is already at the assessment stage, with an applied date.
   - Give a status overview.
   - Record a submission.
   - List what is due this week.
5. The tracker file, followed by every `terminal` command the agent ran, read back from
   `state.db`.

This install has no Kairo MCP tools, so the skill ran in local mode: the bundled
`scripts/tracker.py` with the `terminal` tool.

## Files

| File | Contents |
|---|---|
| `transcript.txt` | The terminal recording with ANSI codes removed. The scratch path is shortened to `$FRESH`. |
| `transcript.typescript` | The raw output of `script -q`, unedited |
| `record.sh` | The script that was recorded |
| `applications.json` | The tracker file at the end of the run |

## Notes

- Rehearsals before this run used local Gemma 4 E4B. They led to the fixes in commit `e2f7035`:
  - The scanner flagged `os.environ[...]`, so the script now uses `.get()`.
  - Unquoted multi-word values are joined.
  - The script computes `due_label`, so the model no longer gets weekdays wrong.
  - Giving `--applied-on` implies at least `Applied`.

  Gemma still skips a step in about one turn in three, so ChatGPT recorded this run. The tap's
  README has tips for small local models.
- The first attempt stalled on a terminal colour query while `hermes chat` held the pty. Chats now
  run with stdin from `/dev/null`, piped through `cat`. That attempt's home was set aside and this
  run used a new one.
- In the fourth chat the agent set Northwind to Applied but left its next action as "Submit the
  application". The agent pointed this out itself in the fifth chat.
