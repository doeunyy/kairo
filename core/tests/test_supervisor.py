"""Supervisor decisions without a network: classification, staged recovery, planned restarts."""

from datetime import datetime
from zoneinfo import ZoneInfo

from kairo_core.supervisor import Policy, classify, read_every_h

LA = ZoneInfo("America/Los_Angeles")


def ts(h, m, day=6):
    return datetime(2026, 11, day, h, m, tzinfo=LA).timestamp()


def test_classify():
    assert classify({"local_up": True}, True) == "ok"
    assert classify({"local_up": False, "local_detail": "transport:ProxyError"}, False) == "laptop_offline"
    assert classify({"local_up": False, "local_detail": "transport:ProxyError"}, True) == "transport"
    assert classify({"local_up": False, "local_detail": "model_missing"}, True) == "model_missing"
    assert classify({"local_up": False, "local_detail": "http_500"}, True) == "http_error"
    assert classify(None, True) == "transport"


def test_transport_failures_reset_then_restart_once_per_hour():
    p = Policy(tz=LA, started=ts(9, 0))
    actions = [p.step(ts(10, i), "transport").actions for i in range(8)]
    assert "reset_tailscale" in actions[2] and "notify_down" in actions[2]
    assert "restart" in actions[5]
    assert not any("restart" in a for a in actions[6:])          # cooldown: not again within the hour
    assert "notify_up" in p.step(ts(10, 9), "ok").actions


def test_laptop_offline_never_triggers_network_repair():
    p = Policy(tz=LA, started=ts(9, 0))
    actions = [p.step(ts(10, i), "laptop_offline").actions for i in range(10)]
    assert not any("reset_tailscale" in a or "restart" in a for a in actions)
    assert actions[2] == ["notify_down"] and sum("notify_down" in a for a in actions) == 1


def test_planned_restart_every_three_hours_at_half_past():
    p = Policy(tz=LA, started=ts(8, 0))
    assert "planned" not in p.step(ts(10, 30), "ok").actions     # 10 is not a multiple of 3
    assert "planned" in p.step(ts(12, 31), "ok").actions
    assert "planned" not in p.step(ts(12, 36), "ok").actions     # same slot only once


def test_no_planned_restart_right_after_boot_and_flag_forces_one():
    p = Policy(tz=LA, started=ts(12, 25))
    assert "planned" not in p.step(ts(12, 31), "ok").actions     # up for 6 minutes only
    d = p.step(ts(12, 40), "ok", restart_flag=True)
    assert "planned" in d.actions and d.reason == "restart_flag"


def test_inference_probe_every_sixth_healthy_beat():
    p = Policy(tz=LA, started=ts(13, 0))
    beats = [p.step(ts(13, i), "ok").actions for i in range(1, 13)]
    assert [i for i, a in enumerate(beats, 1) if "inference" in a] == [6, 12]


def test_new_container_does_not_repeat_a_completed_slot():
    first = Policy(tz=LA, started=ts(15, 0))
    assert "planned" in first.step(ts(15, 33), "ok").actions
    # The restarted container loads the completed slot from the bucket.
    second = Policy(tz=LA, started=ts(15, 34), planned_slot_done=first.planned_slot_done)
    assert "planned" not in second.step(ts(15, 50), "ok").actions
    assert "planned" in second.step(ts(18, 31), "ok").actions


def test_daily_interval_restarts_only_at_three_thirty():
    p = Policy(tz=LA, started=ts(0, 0), every_h=24)
    assert "planned" not in p.step(ts(0, 31), "ok").actions
    assert "planned" in p.step(ts(3, 31), "ok").actions
    for h in (6, 9, 12, 15, 18, 21):
        assert "planned" not in p.step(ts(h, 31), "ok").actions
    assert "planned" in p.step(ts(3, 31, day=7), "ok").actions


def test_three_hour_slots_unchanged_by_the_anchor():
    p = Policy(tz=LA, started=ts(0, 0))
    hits = [h for h in range(1, 24) if "planned" in p.step(ts(h, 31), "ok").actions]
    assert hits == [3, 6, 9, 12, 15, 18, 21]


def test_memory_guard_restarts_at_next_half_hour_once_armed():
    p = Policy(tz=LA, started=ts(0, 0), every_h=24)
    d = p.step(ts(10, 5), "ok", mem_pct=86.0)
    assert d.actions == ["memory_guard_armed"]                    # armed, not yet at :30
    assert "planned" not in p.step(ts(10, 20), "ok", mem_pct=80.0).actions
    d = p.step(ts(10, 31), "ok", mem_pct=80.0)
    assert "planned" in d.actions and d.reason == "memory_guard:86.0%"


def test_memory_guard_waits_for_minimum_uptime_and_ignores_low_usage():
    p = Policy(tz=LA, started=ts(10, 25), every_h=24)
    assert "planned" not in p.step(ts(10, 31), "ok", mem_pct=90.0).actions   # up 6 minutes only
    assert "planned" in p.step(ts(11, 31), "ok", mem_pct=90.0).actions
    q = Policy(tz=LA, started=ts(0, 0), every_h=24)
    assert not any("planned" in q.step(ts(h, 31), "ok", mem_pct=84.9).actions for h in (10, 11, 12))


def test_interval_flag_parsing(tmp_path):
    f = tmp_path / "planned_every_h"
    assert read_every_h(f, 24) == (3, None)                       # no file: default
    f.write_text("24\n")
    assert read_every_h(f, 3) == (24, None)
    f.write_text("5")
    assert read_every_h(f, 24) == (24, "not_allowed:5")
    f.write_text("daily")
    assert read_every_h(f, 3) == (3, "not_an_integer:daily")


def test_run_job_flag_runs_a_kairo_job_once(tmp_path):
    import json
    from kairo_core.supervisor import run_job_flag
    (tmp_path / "cron").mkdir()
    (tmp_path / "cron" / "jobs.json").write_text(json.dumps({"jobs": [{"id": "j-77", "name": "kairo-eod-recap"}]}))
    flag, calls = tmp_path / "run_job", []

    class Started:
        pid = 4242

    def runner(cmd, **kw):
        calls.append((cmd[1:], kw["env"]["HERMES_HOME"], kw["env"]["KAIRO_HERMES_BIN"]))
        return Started()
    assert run_job_flag(flag, "hermes", tmp_path, runner=runner) is None          # no flag, nothing runs
    flag.write_text("kairo-eod-recap\n")
    assert run_job_flag(flag, "hermes", tmp_path, runner=runner) == "kairo-eod-recap"
    assert calls == [(["-m", "kairo_core.launch", "cron", "run", "j-77"], str(tmp_path), "hermes")]
    assert not flag.exists()
    flag.write_text("rm -rf /")                                                    # only Kairo's own jobs
    run_job_flag(flag, "hermes", tmp_path, runner=runner)
    flag.write_text("kairo-morning-briefing")                                      # known name, not registered
    run_job_flag(flag, "hermes", tmp_path, runner=runner)
    assert len(calls) == 1 and not flag.exists()
