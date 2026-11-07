"""Supervisor decisions without a network: classification, staged recovery, planned restarts."""

from datetime import datetime
from zoneinfo import ZoneInfo

from kairo_core.supervisor import Policy, classify

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
