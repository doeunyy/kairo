#!/usr/bin/env bash
# Fresh-install transcript for the job-tracker skill tap.
F=$FRESH
export HERMES_HOME=$F/home2 PATH=$F/hermes-agent/.venv/bin:$PATH
cd $F/work
run() { printf '\n$ %s\n' "$*"; "$@"; }
chat() { printf '\n$ hermes chat -Q -s job-tracker -q %q\n' "$1"; hermes chat -Q -s job-tracker -q "$1" </dev/null | cat; }
echo "== Fresh Hermes install: job-tracker skill tap test =="
run date
run hermes --version
run git -C $F/hermes-agent rev-parse HEAD
echo; echo "\$ HERMES_HOME=$HERMES_HOME  (new home: only config.yaml and its own new ChatGPT login, auth.json)"
run cat $HERMES_HOME/config.yaml
run hermes auth status openai-codex
run ls -A $HERMES_HOME
echo; echo "== Pinned version of the tap =="
run git ls-remote https://github.com/doeunyy/hermes-job-skills HEAD refs/heads/main
echo; echo "== Install from the tap =="
run hermes skills tap add doeunyy/hermes-job-skills
run hermes skills tap list
run hermes skills install doeunyy/hermes-job-skills/job-tracker --yes
run hermes skills list
run ls $HERMES_HOME/skills/job-tracker $HERMES_HOME/skills/job-tracker/scripts
echo; echo "== Use the skill (each line is a new Hermes session; state lives in the JSON file) =="
chat "Track this posting for me. Northwind Labs is hiring a Data Engineer Intern (req NW-305). Build batch and streaming pipelines in Python and SQL; Spark or Airflow a plus. Hybrid in Seattle. Applications close November 20, 2026 at 12:00 PM Pacific."
chat "I already applied to Cardinal Robotics for the Robotics Software Intern role on November 3. They sent an online assessment that's due November 18 at 11:59 PM Pacific. Please track it."
chat "Where do my applications stand?"
chat "I submitted the Northwind Labs Data Engineer Intern application today."
chat "What's due this week?"
echo; echo "== The tracker file =="
run cat $HERMES_HOME/job-tracker/applications.json
echo; echo "== Tool calls the agent made (from \$HERMES_HOME/state.db) =="
printf '\n$ sqlite3 $HERMES_HOME/state.db "<terminal commands from tool calls>"\n'
sqlite3 $HERMES_HOME/state.db "select tool_calls from messages where tool_calls like '%tracker.py%' order by id;" \
  | python3 -c 'import sys,json
for line in sys.stdin:
    for c in json.loads(line):
        a=json.loads(c["function"]["arguments"]); print("terminal:", a.get("command"))'
echo; echo "== done =="
