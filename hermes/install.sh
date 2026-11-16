#!/usr/bin/env bash
# Install Kairo's Hermes pieces into HERMES_HOME: SOUL.md, the personalization profile and the
# cron job wrappers.
# Hermes only runs cron scripts that live inside $HERMES_HOME/scripts (no symlinks), so
# the wrappers are copied with this checkout's absolute path filled in.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
KAIRO_ROOT="$(cd "$HERE/.." && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.a3-kairo}"
KAIRO_ENV_FILE="${KAIRO_ENV_FILE:-$HERMES_HOME/kairo.env}"
KAIRO_STATE_DIR="${KAIRO_STATE_DIR:-$KAIRO_ROOT/state}"

mkdir -p "$HERMES_HOME/scripts" "$HERMES_HOME/memories"
cp "$HERE/SOUL.md" "$HERMES_HOME/SOUL.md"

# Personalization profile (synthetic): add any seed entry that is missing from Hermes' memory files.
# Entries Hermes learned or the user changed are kept; nothing is overwritten or removed. Hermes keeps
# entries separated by a line with "§" and caps USER.md at 1375 and MEMORY.md at 2200 characters.
python3 - "$HERE/profile" "$HERMES_HOME/memories" <<'PY'
import pathlib, sys
src, dst = map(pathlib.Path, sys.argv[1:])
SEP, LIMIT = "\n§\n", {"USER.md": 1375, "MEMORY.md": 2200}
for seed in sorted(src.glob("*.md")):
    target = dst / seed.name
    have = [e for e in (target.read_text().split(SEP) if target.exists() else []) if e.strip()]
    added = 0
    for entry in (e for e in seed.read_text().split(SEP) if e.strip()):
        if entry.strip() in {h.strip() for h in have} or len(SEP.join(have + [entry])) > LIMIT[seed.name]:
            continue
        have.append(entry)
        added += 1
    if added:
        target.write_text(SEP.join(have))
    print(f"{seed.name}: {added} seed entries added, {len(have)} total")
PY
for kind in briefing eod followups interview-soon sweep; do
  out="$HERMES_HOME/scripts/kairo-$kind.sh"
  sed -e "s|__KAIRO_ROOT__|$KAIRO_ROOT|" -e "s|__KAIRO_ENV_FILE__|$KAIRO_ENV_FILE|" -e "s|__KAIRO_STATE_DIR__|$KAIRO_STATE_DIR|" -e "s|__KIND__|$kind|" \
    "$HERE/scripts/kairo-job.sh.in" > "$out"
  chmod 700 "$out"
done
echo "Installed SOUL.md and $(ls "$HERMES_HOME"/scripts/kairo-*.sh | wc -l | tr -d ' ') cron wrappers into $HERMES_HOME"
