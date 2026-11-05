#!/usr/bin/env bash
# Install Kairo's Hermes pieces into HERMES_HOME: SOUL.md and the cron job wrappers.
# Hermes only runs cron scripts that live inside $HERMES_HOME/scripts (no symlinks), so
# the wrappers are copied with this checkout's absolute path filled in.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
KAIRO_ROOT="$(cd "$HERE/.." && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.a3-kairo}"
KAIRO_ENV_FILE="${KAIRO_ENV_FILE:-$HERMES_HOME/kairo.env}"

mkdir -p "$HERMES_HOME/scripts"
cp "$HERE/SOUL.md" "$HERMES_HOME/SOUL.md"
for kind in briefing eod followups interview-soon sweep; do
  out="$HERMES_HOME/scripts/kairo-$kind.sh"
  sed -e "s|__KAIRO_ROOT__|$KAIRO_ROOT|" -e "s|__KAIRO_ENV_FILE__|$KAIRO_ENV_FILE|" -e "s|__KIND__|$kind|" \
    "$HERE/scripts/kairo-job.sh.in" > "$out"
  chmod 700 "$out"
done
echo "Installed SOUL.md and $(ls "$HERMES_HOME"/scripts/kairo-*.sh | wc -l | tr -d ' ') cron wrappers into $HERMES_HOME"
