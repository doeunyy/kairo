#!/usr/bin/env bash
# Create Kairo's Google Cloud resources (Setup §2.9 order). Safe to re-run: existing resources
# are left alone. DRY_RUN=1 prints the commands without running them.
#
#   PROJECT_ID=kairo-dk2026 REGION=us-west1 DRY_RUN=1 deploy/setup-gcp.sh
set -euo pipefail
PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-us-west1}"
AR_REPO="${AR_REPO:-kairo}"
BUCKET="${BUCKET:-${PROJECT_ID}-kairo-state}"
SA_NAME="${SA_NAME:-kairo-agent}"
SA="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
SECRETS=(kairo-env kairo-chatgpt-auth kairo-approval-key)

run() {
  printf '+ %s\n' "$*" >&2
  [ "${DRY_RUN:-0}" = 1 ] || "$@"
}
exists() { "$@" >/dev/null 2>&1; }

echo "project=$PROJECT_ID region=$REGION repo=$AR_REPO bucket=gs://$BUCKET runtime_sa=$SA"
[ -n "$PROJECT_ID" ] || { echo "PROJECT_ID is empty" >&2; exit 1; }

# 1. APIs
run gcloud services enable --project "$PROJECT_ID" \
  run.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com \
  secretmanager.googleapis.com storage.googleapis.com iam.googleapis.com

# 2. Image repository
exists gcloud artifacts repositories describe "$AR_REPO" --location "$REGION" --project "$PROJECT_ID" ||
  run gcloud artifacts repositories create "$AR_REPO" --repository-format docker \
    --location "$REGION" --project "$PROJECT_ID" --description "Kairo images"

# 3. Private state bucket (no public access, uniform IAM)
exists gcloud storage buckets describe "gs://$BUCKET" --project "$PROJECT_ID" ||
  run gcloud storage buckets create "gs://$BUCKET" --project "$PROJECT_ID" --location "$REGION" \
    --uniform-bucket-level-access --public-access-prevention

# 4. Runtime service account with only bucket and secret access
exists gcloud iam service-accounts describe "$SA" --project "$PROJECT_ID" ||
  run gcloud iam service-accounts create "$SA_NAME" --project "$PROJECT_ID" \
    --display-name "Kairo runtime (Cloud Run Instance)"
run gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" --project "$PROJECT_ID" \
  --member "serviceAccount:$SA" --role roles/storage.objectUser --condition None >/dev/null

# 5. Secret containers (values are uploaded separately by deploy/upload-secrets.sh)
for s in "${SECRETS[@]}"; do
  exists gcloud secrets describe "$s" --project "$PROJECT_ID" ||
    run gcloud secrets create "$s" --project "$PROJECT_ID" --replication-policy user-managed \
      --locations "$REGION"
  run gcloud secrets add-iam-policy-binding "$s" --project "$PROJECT_ID" \
    --member "serviceAccount:$SA" --role roles/secretmanager.secretAccessor --condition None >/dev/null
done
# The container writes refreshed ChatGPT logins back, so this one secret also allows adding versions.
run gcloud secrets add-iam-policy-binding kairo-chatgpt-auth --project "$PROJECT_ID" \
  --member "serviceAccount:$SA" --role roles/secretmanager.secretVersionAdder --condition None >/dev/null

echo "setup done"
