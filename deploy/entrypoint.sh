#!/usr/bin/env bash
# PID 1 (under tini) for the Kairo Cloud Run Instance.
#   1 secrets present   2 bucket writable   3 restore snapshot   4 isolation report
#   5 tailscaled → login   6 router   7 Hermes gateway (+ cron)   8 mail watcher
#   9 readiness check      10 background backup + login write-back; SIGTERM → final backup
# Secret values are never printed.
set -euo pipefail

APP=/app
STATE_MNT=/mnt/agent-state
TS_SOCK="$HOME/.tailscale/tailscaled.sock"
PY="$APP/core/.venv/bin/python"
BACKUP_EVERY="${KAIRO_BACKUP_EVERY_S:-900}"
AUTH_SYNC_EVERY="${KAIRO_AUTH_SYNC_EVERY_S:-300}"
PIDS=()

log() {  # log EVENT [extra JSON fields without braces]
  printf '{"ts":"%s","component":"entrypoint","event":"%s"%s}\n' "$(date -u +%FT%TZ)" "$1" "${2:+,$2}"
}
fail() { log "fatal" "\"reason\":\"$1\""; exit 1; }

# ── 1. secrets ─────────────────────────────────────────────────────────────────────────
for f in "$KAIRO_ENV_FILE" /secrets/chatgpt/auth.json /secrets/approval/key; do
  [ -s "$f" ] || fail "missing_secret_file:$f"
done
"$PY" - <<'EOF' || fail "kairo_env_incomplete"
import sys
sys.path.insert(0, "/app/core")
from kairo_core.config import read_env_file
import os
env = read_env_file(os.environ["KAIRO_ENV_FILE"])
need = ["DISCORD_BOT_TOKEN", "DISCORD_ALLOWED_USERS", "DISCORD_HOME_CHANNEL", "KAIRO_MAIL_USER",
        "KAIRO_MAIL_APP_PASSWORD", "KAIRO_CALENDAR_ICS_URL", "KAIRO_NOTION_TOKEN", "KAIRO_NOTION_PARENT_PAGE_ID",
        "KAIRO_SENDER_ALLOWLIST", "KAIRO_RECIPIENT_ALLOWLIST", "TAILSCALE_AUTHKEY", "API_SERVER_KEY"]
missing = [k for k in need if not env.get(k)]
if len(env.get("API_SERVER_KEY", "")) < 16:
    missing.append("API_SERVER_KEY(>=16 chars)")
print('{"component":"entrypoint","event":"secrets_checked","missing":%s}' % __import__("json").dumps(missing))
sys.exit(1 if missing else 0)
EOF
log "secrets_ok"

# ── 2. bucket ──────────────────────────────────────────────────────────────────────────
probe="$STATE_MNT/.write-probe-$HOSTNAME-$$"
( echo ok > "$probe" && rm -f "$probe" ) 2>/dev/null || fail "state_bucket_not_writable"
mkdir -p "$STATE_MNT/snapshots" "$STATE_MNT/flags" "$STATE_MNT/lifecycle"
log "bucket_ok"

# ── 3. restore ─────────────────────────────────────────────────────────────────────────
mkdir -p "$HERMES_HOME" "$KAIRO_STATE_DIR/secrets" && chmod 700 "$HERMES_HOME" "$KAIRO_STATE_DIR"
"$PY" -m kairo_core.snapshot restore
# Config and identity come from the image, credentials from Secret Manager, never from a snapshot.
cp "$APP/deploy/hermes-config.yaml" "$HERMES_HOME/config.yaml"
install -m 600 /secrets/chatgpt/auth.json "$HERMES_HOME/auth.json"
# The mounted login is already the newest secret version; only a refresh after this point is pushed back.
sha256sum "$HERMES_HOME/auth.json" | cut -d" " -f1 > "$HERMES_HOME/.auth.synced"
ln -sfn /secrets/approval/key "$KAIRO_STATE_DIR/secrets/approval.key"
rm -rf "$KAIRO_STATE_DIR/flags" && ln -sfn "$STATE_MNT/flags" "$KAIRO_STATE_DIR/flags"
HERMES_HOME="$HERMES_HOME" KAIRO_ENV_FILE="$KAIRO_ENV_FILE" "$APP/hermes/install.sh" >/dev/null
log "restore_done"

# ── 4. isolation report ────────────────────────────────────────────────────────────────
"$PY" - <<'EOF'
import json, os
status = dict(l.split(":\t", 1) for l in open("/proc/self/status").read().splitlines() if ":\t" in l)
listen = set()
for name in ("/proc/net/tcp", "/proc/net/tcp6"):
    try:
        for line in open(name).read().splitlines()[1:]:
            local, state = line.split()[1], line.split()[3]
            if state == "0A":
                listen.add(int(local.rsplit(":", 1)[1], 16))
    except OSError:
        pass
ro = [m.split()[1] for m in open("/proc/mounts") if " ro," in m and m.split()[1].startswith(("/secrets", "/app"))]
print(json.dumps({"component": "entrypoint", "event": "isolation_report", "uid": os.getuid(), "gid": os.getgid(),
                  "cap_eff": status.get("CapEff", "?").strip(), "no_new_privs": status.get("NoNewPrivs", "?").strip(),
                  "listening_ports_before_start": sorted(listen), "read_only_mounts": ro,
                  "app_writable": os.access("/app", os.W_OK)}))
EOF

# ── 5. tailscale (userspace; SOCKS5 for the router's local path) ───────────────────────
mkdir -p "$(dirname "$TS_SOCK")"
tailscaled --tun=userspace-networking --socks5-server=127.0.0.1:1055 --state=mem: \
  --socket="$TS_SOCK" --statedir="$HOME/.tailscale" >"$HOME/.tailscale/tailscaled.log" 2>&1 &
PIDS+=($!)
TS_KEY="$("$PY" -c 'import os,sys; sys.path.insert(0,"/app/core"); from kairo_core.config import read_env_file; print(read_env_file(os.environ["KAIRO_ENV_FILE"])["TAILSCALE_AUTHKEY"])')"
for _ in $(seq 1 30); do [ -S "$TS_SOCK" ] && break; sleep 1; done
tailscale --socket="$TS_SOCK" up --auth-key="$TS_KEY" --hostname="${TS_HOSTNAME:-kairo-cloud}" \
  --accept-dns=false >/dev/null || fail "tailscale_up"
unset TS_KEY
log "tailscale_up" "\"ip\":\"$(tailscale --socket="$TS_SOCK" ip -4 | head -1)\""

# ── 6. router ──────────────────────────────────────────────────────────────────────────
( cd "$APP/router" && A3_STATE_DIR="$KAIRO_STATE_DIR" exec .venv/bin/uvicorn a3_router.app:app \
    --host 127.0.0.1 --port 8787 --no-access-log ) &
PIDS+=($!)

# ── 7. Hermes gateway: Discord, API server on $PORT (key required), cron ───────────────
# gateway.log may come back from a snapshot; readiness only counts lines written after this point.
GW_LOG="$HERMES_HOME/logs/gateway.log"
GW_LOG_OFFSET=$(( $( [ -f "$GW_LOG" ] && wc -c < "$GW_LOG" || echo 0 ) + 1 ))
( cd "$APP/core" && API_SERVER_ENABLED=true API_SERVER_HOST=0.0.0.0 API_SERVER_PORT="$PORT" \
    exec .venv/bin/python -m kairo_core.launch gateway ) &
PIDS+=($!)

# On 1 vCPU, start-up is CPU-bound: let the gateway finish (Discord connected) before anything else.
gw_ready=0
for _ in $(seq 1 150); do
  tail -c "+$GW_LOG_OFFSET" "$GW_LOG" 2>/dev/null | grep -q "discord connected" && { gw_ready=1; break; }
  sleep 2
done
log "gateway_$([ $gw_ready = 1 ] && echo up || echo slow)"

# ── 8. mail watcher ────────────────────────────────────────────────────────────────────
( cd "$APP/core" && exec .venv/bin/python -m kairo_core watch ) &
PIDS+=($!)

# Cron registration uses the hermes CLI (~50 s per call on 1 vCPU) and skips jobs a restored
# snapshot already has, so it runs last, in the background.
( SHOW_JOBS=0 HERMES_HOME="$HERMES_HOME" "$APP/hermes/cron-jobs.sh" 2>/dev/null \
    | while read -r verb job _; do log "cron_job" "\"result\":\"$verb\",\"job\":\"$job\""; done
  log "cron_registered" ) &

# ── 9. readiness: model reachable through the tailnet, Discord connected ───────────────
ready=0
for _ in $(seq 1 30); do
  health="$(curl -fsS 127.0.0.1:8787/router/health 2>/dev/null || true)"
  if grep -q '"local_up":true' <<<"$health" && [ $gw_ready = 1 ]; then ready=1; break; fi
  sleep 2
done
log "$([ $ready = 1 ] && echo ready || echo degraded)" "\"router\":${health:-null},\"discord\":$gw_ready"
echo "{\"ts\":\"$(date -u +%FT%TZ)\",\"event\":\"start\",\"host\":\"$HOSTNAME\",\"ready\":$ready}" \
  >> "$STATE_MNT/lifecycle/events.jsonl"

# ── 10. supervisor: heartbeat, staged recovery, planned restarts (kairo_core/supervisor.py)
( cd "$APP/core" && exec .venv/bin/python -m kairo_core.supervisor ) &
SUP_PID=$!
PIDS+=($SUP_PID)

# ── 11. background upkeep and shutdown ─────────────────────────────────────────────────
backup() { "$PY" -m kairo_core.snapshot backup || log "backup_failed"; }
( while sleep "$BACKUP_EVERY"; do backup; done ) &
PIDS+=($!)
( while sleep "$AUTH_SYNC_EVERY"; do "$PY" -m kairo_core.snapshot sync-auth || log "auth_sync_failed"; done ) &
PIDS+=($!)

shutdown() {
  log "shutdown" "\"signal\":\"$1\""
  "$PY" -m kairo_core.snapshot sync-auth || true
  backup
  echo "{\"ts\":\"$(date -u +%FT%TZ)\",\"event\":\"stop\",\"signal\":\"$1\"}" >> "$STATE_MNT/lifecycle/events.jsonl"
  kill -TERM "${PIDS[@]}" 2>/dev/null || true
  wait || true
  exit 0
}
trap 'shutdown TERM' TERM
trap 'shutdown INT' INT

# If any process exits, save state and stop. The supervisor exits 10 for a planned restart
# (exit 0) and 3 for a recovery restart; anything else is a crash (exit 1). The restart policy
# is "always", so Cloud Run starts a fresh container in every case.
set +e
wait -n -p done_pid "${PIDS[@]}"
code=$?
if [ "${done_pid:-}" = "$SUP_PID" ] && [ $code = 10 ]; then reason=planned_restart; final=0
elif [ "${done_pid:-}" = "$SUP_PID" ] && [ $code = 3 ]; then reason=recovery_restart; final=1
else reason=child_exited; final=1; fi
log "$reason" "\"pid\":${done_pid:-0},\"code\":$code"
"$PY" -m kairo_core.snapshot sync-auth
backup
echo "{\"ts\":\"$(date -u +%FT%TZ)\",\"event\":\"stop\",\"reason\":\"$reason\",\"code\":$code}" \
  >> "$STATE_MNT/lifecycle/events.jsonl"
kill -TERM "${PIDS[@]}" 2>/dev/null
wait
exit $final
