"""job-tracker skill: the local tracker script used when Kairo's tools are absent."""

from __future__ import annotations

import importlib.util
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "hermes" / "skills" / "job-tracker" / "scripts" / "tracker.py"
spec = importlib.util.spec_from_file_location("tracker", SCRIPT)
tracker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tracker)


@pytest.fixture
def run(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("JOB_TRACKER_FILE", str(tmp_path / "apps.json"))

    def _run(*argv):
        assert tracker.main(list(argv)) == 0
        return json.loads(capsys.readouterr().out)
    return _run


def test_add_list_and_duplicate(run):
    out = run("add", "--company", "Northwind Labs", "--role", "Data Engineer Intern", "--posting-id", "NW-305")
    assert out["result"] == "added" and out["application"]["status"] == "Interested"
    again = run("add", "--company", "northwind labs", "--role", "Data  Engineer Intern")
    assert again["result"] == "exists"
    same_posting = run("add", "--company", "Northwind Labs", "--role", "Data Eng Intern", "--posting-id", "nw-305")
    assert same_posting["result"] == "exists"
    assert run("list")["count"] == 1


def test_update_records_history_and_applied_date(run):
    run("add", "--company", "Cardinal Robotics", "--role", "Robotics Software Intern")
    out = run("update", "--company", "Cardinal Robotics", "--role", "Robotics Software Intern", "--status", "applied",
              "--note", "Submitted via portal")
    app = out["application"]
    assert app["status"] == "Applied" and app["applied_on"] == date.today().isoformat()
    assert [h["status"] for h in app["history"]] == ["Interested", "Applied"]
    assert app["notes"][0]["text"] == "Submitted via portal"


def test_due_lists_overdue_first_and_skips_closed(run):
    now = datetime.now().astimezone()
    run("add", "--company", "A", "--role", "R1", "--due", (now - timedelta(days=1)).isoformat(timespec="minutes"))
    run("add", "--company", "B", "--role", "R2", "--due", (now + timedelta(days=2)).isoformat(timespec="minutes"))
    run("add", "--company", "C", "--role", "R3", "--due", (now + timedelta(days=30)).isoformat(timespec="minutes"))
    run("add", "--company", "D", "--role", "R4", "--status", "Rejected", "--due", (now + timedelta(days=1)).isoformat())
    out = run("due", "--days", "7")
    assert [r["company"] for r in out["overdue"]] == ["A"]
    assert [r["company"] for r in out["due_soon"]] == ["B"]


def test_quiet_finds_applied_without_recent_contact(run):
    run("add", "--company", "Old", "--role", "R", "--status", "Applied")
    run("update", "--company", "Old", "--role", "R", "--contact", (date.today() - timedelta(days=12)).isoformat())
    run("add", "--company", "Fresh", "--role", "R", "--status", "Applied")
    out = run("quiet", "--days", "10")
    assert [(r["company"], r["days_quiet"]) for r in out["applications"]] == [("Old", 12)]


def test_unquoted_multiword_values_are_joined(run):
    # Seen with Gemma 4 E4B in the fresh-install rehearsal: --company Northwind Labs without quotes.
    out = run("add", "--company", "Northwind", "Labs", "--role", "Data", "Engineer", "Intern",
              "--next-action", "Submit", "the", "application")
    app = out["application"]
    assert (app["company"], app["role"], app["next_action"]) == ("Northwind Labs", "Data Engineer Intern",
                                                                 "Submit the application")
    cleared = run("update", "--company", "Northwind", "Labs", "--role", "Data", "Engineer", "Intern", "--next-action")
    assert cleared["application"]["next_action"] is None


def test_applied_on_and_due_label(run):
    # Rehearsal: "I already applied on November 3" was recorded as Interested with no date.
    out = run("add", "--company", "Cardinal Robotics", "--role", "Robotics Software Intern", "--status", "Assessment",
              "--applied-on", "2026-11-03", "--due", "2026-11-18T23:59-08:00")
    app = out["application"]
    assert app["applied_on"] == "2026-11-03"
    assert app["due_label"] == "Wed Nov 18, 2026 23:59"            # weekday from the script, not the model
    implied = run("add", "--company", "C", "--role", "R", "--applied-on", "2026-11-03")["application"]
    assert implied["status"] == "Applied"                             # a date implies it was submitted
    plain = run("add", "--company", "B", "--role", "R", "--status", "Interview")["application"]
    assert plain["applied_on"] == date.today().isoformat()            # past Applied: defaults to today
    assert "due_label" not in plain


def test_bad_status_and_date_are_rejected(run):
    with pytest.raises(SystemExit):
        tracker.main(["add", "--company", "X", "--role", "Y", "--status", "Hired"])
    with pytest.raises(SystemExit):
        tracker.main(["add", "--company", "X", "--role", "Y", "--due", "next friday"])


def test_store_defaults_to_hermes_home(tmp_path, monkeypatch):
    monkeypatch.delenv("JOB_TRACKER_FILE", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert tracker.store_path() == tmp_path / "job-tracker" / "applications.json"
