"""hermes/cron-jobs.sh must hand each agent job its whole prompt, route tag first.

A double quote inside a prompt once ended the bash string early: Hermes stored only the text after
it, the route tag was lost, and the router sent the morning briefing to the local model.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "hermes" / "cron-jobs.sh"

FAKE_HERMES = r"""#!/usr/bin/env bash
[ "$1 $2" = "cron list" ] && exit 0
if [ "$2" = create ]; then
  shift 2
  python3 - "$@" >> "$HERMES_HOME/calls.txt" <<'PY'
import json, sys
print(json.dumps(sys.argv[1:]))
PY
fi
"""


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_every_agent_prompt_is_one_argument_with_its_route_tag(tmp_path):
    import json

    fake = tmp_path / "hermes"
    fake.write_text(FAKE_HERMES)
    fake.chmod(0o755)
    (tmp_path / "cron").mkdir()
    subprocess.run(["bash", str(SCRIPT)], check=True, capture_output=True,
                   env={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
                        "HERMES_HOME": str(tmp_path), "KAIRO_HERMES_BIN": str(fake)})

    calls = [json.loads(line) for line in (tmp_path / "calls.txt").read_text().splitlines()]
    jobs = {args[args.index("--name") + 1]: args for args in calls}
    assert len(jobs) == 6
    agent_jobs = {name: args for name, args in jobs.items() if "--no-agent" not in args}
    assert set(agent_jobs) == {"kairo-morning-briefing", "kairo-eod-recap", "kairo-followups",
                               "kairo-memory-consolidation"}
    for name, args in agent_jobs.items():
        prompt = args[-1]
        task = name.removeprefix("kairo-").replace("-", "_")
        assert prompt.startswith(f"<!--route task={task}"), name
        assert "\n" in prompt and prompt.rstrip().endswith("."), name
    assert "'Unread or flagged email'" in agent_jobs["kairo-morning-briefing"][-1]
