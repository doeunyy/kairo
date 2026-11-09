#!/usr/bin/env bash
# Deploy (or update) the Kairo Cloud Run Instance from the image deploy/build.sh produced.
#   LAPTOP_TS_IP=100.x.y.z deploy/deploy.sh
set -euo pipefail
PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-us-west1}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE="${IMAGE:-$(cat "$ROOT/deploy/.last-image")}"
BUCKET="${BUCKET:-${PROJECT_ID}-kairo-state}"
SA="kairo-agent@${PROJECT_ID}.iam.gserviceaccount.com"
: "${LAPTOP_TS_IP:?set LAPTOP_TS_IP to the laptop tailnet IPv4 (tailscale ip -4)}"

# The default HTTPS URL serves the course web chat (web_ui.py on $PORT), which proxies /v1/* and
# /health to the Hermes API server on 127.0.0.1:8081; every API call still requires API_SERVER_KEY.
gcloud beta run instances deploy kairo \
  --project "$PROJECT_ID" --region "$REGION" \
  --image "$IMAGE" \
  --service-account "$SA" \
  --cpu 1 --memory 2Gi \
  --restart-policy always \
  --no-invoker-iam-check \
  --port 8080 \
  --startup-probe "tcpSocket.port=8080,initialDelaySeconds=10,periodSeconds=10,failureThreshold=30,timeoutSeconds=5" \
  --add-volume "name=state,type=cloud-storage,bucket=${BUCKET},mount-options=uid=1000;gid=1000;file-mode=600;dir-mode=700" \
  --add-volume-mount "volume=state,mount-path=/mnt/agent-state" \
  --set-secrets "/secrets/env/kairo.env=kairo-env:latest,/secrets/chatgpt/auth.json=kairo-chatgpt-auth:latest,/secrets/approval/key=kairo-approval-key:latest" \
  --set-env-vars "^@^GOOGLE_CLOUD_PROJECT=${PROJECT_ID}@A3_LOCAL_MODEL=gemma4:e4b@A3_OLLAMA_BASE_URL=http://${LAPTOP_TS_IP}:11434@A3_OLLAMA_PROXY=socks5h://127.0.0.1:1055@TS_HOSTNAME=kairo-cloud@KAIRO_HEARTBEAT_EVERY_S=${KAIRO_HEARTBEAT_EVERY_S:-300}"

gcloud beta run instances describe kairo --project "$PROJECT_ID" --region "$REGION" --format=yaml \
  | grep -E "^ *(uri|url|image|serviceAccount|cpu|memory|restartPolicy):" | head -12
