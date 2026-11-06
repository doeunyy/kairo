#!/usr/bin/env bash
# Build the image with Cloud Build and print its digest. Tag = current git commit (+ -dirty).
set -euo pipefail
PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-us-west1}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TAG="$(git -C "$ROOT" rev-parse --short HEAD)$(git -C "$ROOT" diff --quiet HEAD -- . || echo -dirty)"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/kairo/kairo:${TAG}"

cd "$ROOT"
gcloud builds submit --project "$PROJECT_ID" --region "$REGION" \
  --config deploy/cloudbuild.yaml --substitutions "_IMAGE=${IMAGE}" .
DIGEST="$(gcloud artifacts docker images describe "$IMAGE" --format='value(image_summary.digest)')"
echo "IMAGE=${IMAGE%:*}@${DIGEST}"
echo "${IMAGE%:*}@${DIGEST}" > "$ROOT/deploy/.last-image"
