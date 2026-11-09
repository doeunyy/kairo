"""The MCP server runs each sync tool in a worker thread (mcp 2.x): the SQLite stores created on
the first call must work from the threads that serve later calls."""

from __future__ import annotations

import threading

from kairo_core import mcp_server
from kairo_core.ledger import Ledger
from kairo_core.outbox import Outbox
from kairo_core.prefilter import Allowlist


def in_other_thread(fn):
    box: dict = {}

    def run():
        try:
            box["value"] = fn()
        except Exception as exc:              # surface it in the test thread
            box["error"] = exc
    th = threading.Thread(target=run)
    th.start()
    th.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")


def test_outbox_and_ledger_usable_from_another_thread(tmp_path):
    outbox = Outbox(tmp_path / "outbox.sqlite3", b"k" * 32, sender_address="me@kairo.test",
                    approvers=frozenset({"1"}), recipients=Allowlist(addresses=frozenset({"r@kairo.test"})))
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    draft = in_other_thread(lambda: outbox.create_draft("r@kairo.test", "Hi", "Body"))
    assert outbox.get(draft.draft_id).status == "pending"
    assert in_other_thread(lambda: ledger.record("<m1@kairo.test>", "processed")) is True
    assert ledger.seen("<m1@kairo.test>")


def test_tool_calls_are_serialized(monkeypatch):
    active, peak = 0, 0
    gate = threading.Lock()

    class Fake:
        def list_applications(self, status):
            nonlocal active, peak
            with gate:
                active += 1
                peak = max(peak, active)
            threading.Event().wait(0.02)
            with gate:
                active -= 1
            return []

    monkeypatch.setattr(mcp_server, "_tools", Fake())
    threads = [threading.Thread(target=mcp_server.list_applications) for _ in range(5)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert peak == 1
