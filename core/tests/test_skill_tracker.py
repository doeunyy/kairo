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


def test_bad_status_and_date_are_rejected(run):
    with pytest.raises(SystemExit):
        tracker.main(["add", "--company", "X", "--role", "Y", "--status", "Hired"])
    with pytest.raises(SystemExit):
        tracker.main(["add", "--company", "X", "--role", "Y", "--due", "next friday"])


def test_store_defaults_to_hermes_home(tmp_path, monkeypatch):
    monkeypatch.delenv("JOB_TRACKER_FILE", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert tracker.store_path() == tmp_path / "job-tracker" / "applications.json"
