#!/usr/bin/env bash
# Create a SEPARATE ChatGPT (Codex) login for the cloud deployment and upload it to Secret Manager.
# Codex refresh tokens are single-use with reuse detection, so the cloud must never share the
# laptop's login file. Run in your own terminal (the login opens a browser):
#
#   deploy/cloud-login.sh
#
# Afterwards the temporary Codex and Hermes homes are deleted; only Secret Manager holds the login.
set -euo pipefail
PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
HERMES_PY="${KAIRO_HERMES_PY:-$HOME/.local/share/kairo/hermes-agent/.venv/bin/python}"
HERMES_SRC="$(dirname "$(dirname "$(dirname "$HERMES_PY")")")"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
chmod 700 "$WORK"
export CODEX_HOME="$WORK/codex" HERMES_HOME="$WORK/hermes"
mkdir -p "$CODEX_HOME" "$HERMES_HOME"

echo "1/3 Sign in to ChatGPT for the cloud deployment (same account, new login)."
codex login ${CODEX_LOGIN_FLAGS:-}

echo "2/3 Converting the login to Hermes' auth.json"
( cd "$HERMES_SRC" && "$HERMES_PY" - <<'EOF'
from hermes_cli.auth_codex import _import_codex_cli_tokens, _save_codex_tokens
from hermes_cli.auth import _update_config_for_provider
tokens = _import_codex_cli_tokens()
if not tokens:
    raise SystemExit("no valid Codex login found in the temporary CODEX_HOME")
_save_codex_tokens(tokens, label="kairo-cloud")
_update_config_for_provider("openai-codex", "https://chatgpt.com/backend-api/codex", "gpt-5.6-sol")
print("converted")
EOF
)
[ -s "$HERMES_HOME/auth.json" ] || { echo "auth.json was not created" >&2; exit 1; }

echo "3/3 Uploading to Secret Manager (kairo-chatgpt-auth)"
gcloud secrets versions add kairo-chatgpt-auth --project "$PROJECT_ID" --data-file="$HERMES_HOME/auth.json" \
  --format="value(name)" | sed 's|.*/versions/|Uploaded kairo-chatgpt-auth version |'
echo "Local temporary copies deleted. Do not run 'codex' with this login again."
