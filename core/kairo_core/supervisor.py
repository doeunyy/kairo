"""Heartbeat, staged recovery and planned restarts for the cloud container.

Every beat (default 5 min) the supervisor asks the router for a fresh local-model check and
asks Tailscale whether the laptop is online, then classifies the result:

    ok              model reachable through the tailnet
    laptop_offline  the laptop is not on the tailnet: nothing in the cloud can fix that
    model_missing / http_error   Ollama answered but is not usable: a network reset won't help
    transport       laptop online but unreachable: the tunnel is the likely fault

Only ``transport`` triggers repairs, in stages: after 3 consecutive failures the Tailscale
connection is reset (``tailscale down`` / ``up``) and re-verified; if failures continue
for 3 more beats the container is restarted, at most once per hour. Every 6th healthy beat
also runs a real inference through the router. Planned restarts happen every 3 hours at :30
(America/Los_Angeles) or when ``flags/restart_now`` appears in the bucket.

Exit codes tell the entrypoint why the supervisor stopped: 10 planned restart, 3 recovery
restart. Events are JSON lines on stdout (Cloud Logging) and in the bucket's lifecycle log.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

EXIT_PLANNED, EXIT_RECOVERY = 10, 3
RESET_AFTER, RESTART_AFTER = 3, 6            # consecutive transport failures
RESTART_COOLDOWN_S = 3600
PLANNED_EVERY_H, PLANNED_MINUTE = 3, 30
MIN_UPTIME_FOR_PLANNED_S = 15 * 60
INFERENCE_EVERY = 6                          # beats (30 min at the default 5 min beat)


def classify(health: dict | None, peer_online: bool | None) -> str:
    if health and health.get("local_up"):
        return "ok"
    if peer_online is False:
        return "laptop_offline"
    detail = (health or {}).get("local_detail", "router_unreachable")
    if detail == "model_missing":
        return "model_missing"
    if detail.startswith("http_"):
        return "http_error"
    return "transport"


@dataclass
class Decision:
    actions: list[str] = field(default_factory=list)   # reset_tailscale | restart | planned | notify_down | notify_up | inference
    reason: str = ""


@dataclass
class Policy:
    """Pure state machine; the loop below feeds it observations and performs its actions."""
    tz: ZoneInfo = field(default_factory=lambda: ZoneInfo("America/Los_Angeles"))
    started: float = field(default_factory=time.time)
    beats: int = 0
    transport_failures: int = 0
    unhealthy_beats: int = 0
    down_notified: bool = False
    last_recovery_restart: float = 0.0
    planned_slot_done: str = ""

    def planned_slot(self, now: float) -> str | None:
        local = datetime.fromtimestamp(now, self.tz)
        if local.hour % PLANNED_EVERY_H == 0 and local.minute >= PLANNED_MINUTE:
            return local.strftime("%Y-%m-%dT%H:30")
        return None

    def step(self, now: float, status: str, restart_flag: bool = False) -> Decision:
        self.beats += 1
        d = Decision(reason=status)
        if status == "transport":
            self.transport_failures += 1
            if self.transport_failures == RESET_AFTER:
                d.actions.append("reset_tailscale")
            if (self.transport_failures >= RESTART_AFTER
                    and now - self.last_recovery_restart >= RESTART_COOLDOWN_S):
                self.last_recovery_restart = now
                d.actions.append("restart")
        else:
            self.transport_failures = 0
        unhealthy = status != "ok"
        self.unhealthy_beats = self.unhealthy_beats + 1 if unhealthy else 0
        if self.unhealthy_beats >= RESET_AFTER and not self.down_notified:
            self.down_notified = True
            d.actions.append("notify_down")
        if not unhealthy and self.down_notified:
            self.down_notified = False
            d.actions.append("notify_up")
        if status == "ok" and self.beats % INFERENCE_EVERY == 0:
            d.actions.append("inference")
        slot = self.planned_slot(now)
        if restart_flag:
            d.actions.append("planned")
            d.reason = "restart_flag"
        elif slot and slot != self.planned_slot_done and now - self.started >= MIN_UPTIME_FOR_PLANNED_S:
            self.planned_slot_done = slot
            d.actions.append("planned")
            d.reason = f"schedule:{slot}"
        return d


# ── side effects ──────────────────────────────────────────────────────────────────────

def event(name: str, lifecycle: Path | None = None, **fields) -> None:
    line = json.dumps({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "component": "supervisor",
                       "event": name, **fields})
    print(line, flush=True)
    if lifecycle is not None:
        try:
            with open(lifecycle, "a") as fh:
                fh.write(line + "\n")
        except OSError:
            pass


def container_memory() -> dict:
    """cgroup v2 (or v1) usage against the 1 GiB limit, to catch slow leaks during long runs."""
    for cur, lim in (("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory.max"),
                     ("/sys/fs/cgroup/memory/memory.usage_in_bytes", "/sys/fs/cgroup/memory/memory.limit_in_bytes")):
        try:
            used = int(Path(cur).read_text())
            raw = Path(lim).read_text().strip()
            limit = None if raw == "max" else int(raw)
            out = {"mem_used_mb": used // 2**20}
            if limit and limit < 2**50:
                out["mem_limit_mb"] = limit // 2**20
                out["mem_pct"] = round(100 * used / limit, 1)
            return out
        except (OSError, ValueError):
            continue
    return {}


def router_health(router: str) -> dict | None:
    try:
        with urllib.request.urlopen(f"{router}/router/health", timeout=15) as resp:
            return json.loads(resp.read())
    except (OSError, ValueError):
        return None


def tailscale(sock: str, *args: str, timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run(["tailscale", f"--socket={sock}", *args], capture_output=True, text=True, timeout=timeout)


def peer_online(sock: str, laptop_ip: str) -> bool | None:
    try:
        status = json.loads(tailscale(sock, "status", "--json").stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    for peer in (status.get("Peer") or {}).values():
        if laptop_ip in (peer.get("TailscaleIPs") or []):
            return bool(peer.get("Online"))
    return False


def inference_probe(router: str) -> dict:
    body = {"model": "gpt-5.6-sol", "store": False, "stream": False,
            "instructions": "<!--route task=heartbeat simple=true-->\nReply with the single word OK.",
            "input": [{"role": "user", "content": [{"type": "input_text",
                                                     "text": "<!--route task=heartbeat simple=true-->\nPing"}]}]}
    req = urllib.request.Request(f"{router}/v1/responses", data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            out = json.loads(resp.read())
        ok = out.get("status") == "completed" and out.get("model") != "a3-router/hold"
        return {"ok": ok, "model": out.get("model"), "latency_ms": int((time.monotonic() - started) * 1000)}
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": type(exc).__name__, "latency_ms": int((time.monotonic() - started) * 1000)}


def reset_tailscale(sock: str, auth_key: str, hostname: str) -> bool:
    tailscale(sock, "down")
    result = tailscale(sock, "up", f"--auth-key={auth_key}", f"--hostname={hostname}", "--accept-dns=false",
                       timeout=60)
    return result.returncode == 0


def notify(text: str) -> None:
    try:
        from .config import Settings
        from .discord import Discord
        s = Settings.load()
        Discord(s.require("DISCORD_BOT_TOKEN")).post(s.require("DISCORD_HOME_CHANNEL"), text)
    except Exception as exc:     # a notification problem must never stop the supervisor
        event("notify_failed", error=type(exc).__name__)


def main() -> int:
    from .config import read_env_file
    router = os.environ.get("KAIRO_ROUTER_URL", "http://127.0.0.1:8787")
    sock = os.environ.get("TS_SOCKET", str(Path.home() / ".tailscale" / "tailscaled.sock"))
    laptop_ip = os.environ["A3_OLLAMA_BASE_URL"].split("//", 1)[1].split(":", 1)[0]
    every = float(os.environ.get("KAIRO_HEARTBEAT_EVERY_S", "300"))
    # Keep the tailnet path warm between heartbeats: after a few idle minutes the first
    # connection through the DERP relay can miss the router's health-check timeout.
    keepalive = min(every, float(os.environ.get("KAIRO_KEEPALIVE_S", "60")))
    state_mnt = Path(os.environ.get("KAIRO_STATE_MNT", "/mnt/agent-state"))
    lifecycle = state_mnt / "lifecycle" / "heartbeat.jsonl"
    restart_flag = state_mnt / "flags" / "restart_now"
    stamp = state_mnt / "lifecycle" / "last_recovery_restart"
    # Read once at start: after scheduled maintenance the secret mount can briefly fail reads.
    auth_key = read_env_file(os.environ["KAIRO_ENV_FILE"]).get("TAILSCALE_AUTHKEY", "")
    policy = Policy()
    try:
        policy.last_recovery_restart = float(stamp.read_text())
    except (OSError, ValueError):
        pass
    event("supervisor_start", lifecycle, heartbeat_every_s=every, keepalive_s=keepalive, laptop=laptop_ip)

    while True:
        next_beat = time.monotonic() + every
        while time.monotonic() + keepalive < next_beat:
            time.sleep(keepalive)
            router_health(router)                     # keepalive only; not logged
        time.sleep(max(0.0, next_beat - time.monotonic()))
        health = router_health(router)
        online = peer_online(sock, laptop_ip)
        status = classify(health, online)
        flag = restart_flag.exists()
        d = policy.step(time.time(), status, flag)
        event("heartbeat", lifecycle, status=status, peer_online=online,
              local_detail=(health or {}).get("local_detail"), failures=policy.transport_failures,
              actions=d.actions, **container_memory())
        if "notify_down" in d.actions:
            notify("⚠️ The local model (laptop) is unreachable. Private tasks such as email analysis are held, "
                   "not sent to the cloud, until it is back.")
        if "notify_up" in d.actions:
            notify("✅ The local model is reachable again. Held emails will be processed on the next sweep.")
        if "inference" in d.actions:
            event("inference_probe", lifecycle, **inference_probe(router))
        if "reset_tailscale" in d.actions:
            try:
                ok = reset_tailscale(sock, auth_key, os.environ.get("TS_HOSTNAME", "kairo-cloud"))
            except (OSError, subprocess.SubprocessError) as exc:
                ok = False
                event("recovery_stage1_error", lifecycle, error=type(exc).__name__)
            after = classify(router_health(router), peer_online(sock, laptop_ip))
            event("recovery_stage1_tailscale_reset", lifecycle, up_ok=ok, status_after=after)
        if "restart" in d.actions:
            stamp.write_text(str(policy.last_recovery_restart))
            event("recovery_stage2_container_restart", lifecycle, failures=policy.transport_failures)
            return EXIT_RECOVERY
        if "planned" in d.actions:
            restart_flag.unlink(missing_ok=True)
            event("planned_restart", lifecycle, reason=d.reason)
            return EXIT_PLANNED


if __name__ == "__main__":
    sys.exit(main())
