#!/usr/bin/env bash
# Upload Kairo's runtime secrets to Secret Manager without printing them.
#   kairo-env           cloud copy of ~/.a3-kairo/kairo.env (laptop-only lines removed)
#   kairo-approval-key  32 random bytes, created once (never reused from the laptop)
# Generates API_SERVER_KEY in the local kairo.env if it is empty (graders get it via Brightspace).
set -euo pipefail
PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
ENV_FILE="${KAIRO_ENV_FILE:-$HOME/.a3-kairo/kairo.env}"
HERE="$(cd "$(dirname "$0")" && pwd)"

python3 - "$ENV_FILE" <<'EOF'
import re, secrets, sys
path = sys.argv[1]
text = open(path).read()
m = re.search(r"^API_SERVER_KEY=([^\s#]*)", text, re.M)
if m and not m.group(1):
    text = text[:m.start()] + f"API_SERVER_KEY={secrets.token_urlsafe(32)}" + text[m.end():]
    open(path, "w").write(text)
    print("generated API_SERVER_KEY in local kairo.env")
EOF

# Cloud copy: drop laptop paths (HERMES_HOME) and router settings, which the deploy sets as env vars.
cloud_env="$(grep -v -E '^(HERMES_HOME|A3_[A-Z_]+|KAIRO_STATE_DIR|KAIRO_HERMES_BIN)=' "$ENV_FILE")"
python3 - <<EOF
import sys
sys.path.insert(0, "$HERE/../core")
from kairo_core.config import read_env_file
env = read_env_file("$ENV_FILE")
missing = [k for k in ("TAILSCALE_AUTHKEY", "API_SERVER_KEY", "DISCORD_BOT_TOKEN", "KAIRO_MAIL_APP_PASSWORD",
                       "KAIRO_NOTION_TOKEN", "KAIRO_CALENDAR_ICS_URL") if not env.get(k)]
if missing:
    sys.exit(f"kairo.env is missing: {', '.join(missing)}")
EOF
printf '%s\n' "$cloud_env" | gcloud secrets versions add kairo-env --project "$PROJECT_ID" --data-file=- \
  --format="value(name)" | sed 's|.*/versions/|kairo-env version |'

if [ -z "$(gcloud secrets versions list kairo-approval-key --project "$PROJECT_ID" --filter=state=enabled --format='value(name)')" ]; then
  head -c 32 /dev/urandom | gcloud secrets versions add kairo-approval-key --project "$PROJECT_ID" --data-file=- \
    --format="value(name)" | sed 's|.*/versions/|kairo-approval-key version |'
else
  echo "kairo-approval-key already set; kept"
fi
