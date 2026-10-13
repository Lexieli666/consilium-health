#!/usr/bin/env bash
# One command from a fresh Google account to a running Cloud Run service.  Idempotent: every step
# checks before it creates, so re-running after a failure continues where it stopped.
#
#   OPENAI_API_KEY must be exported in your shell (it is read from the environment, never typed).
#
#   ./deploy/cloudrun/bootstrap.sh                      # project id defaults to consilium-health-<your-initials-from-email>
#   PROJECT_ID=my-project ./deploy/cloudrun/bootstrap.sh
#
# What it does, in order: project (create if missing) -> billing (link the first billing account the
# account can see; stops with instructions if there is none) -> APIs -> Secret Manager secret from
# $OPENAI_API_KEY -> service account + the two grants -> deploy.sh -> /healthz -> one real /v1/ask.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

ACCOUNT="$(gcloud config get-value account 2>/dev/null || true)"
if [ -z "$ACCOUNT" ]; then
    echo ">> not logged in; opening the browser for gcloud auth login"
    gcloud auth login --update-adc
    ACCOUNT="$(gcloud config get-value account)"
fi
echo ">> account: ${ACCOUNT}"

: "${OPENAI_API_KEY:?export OPENAI_API_KEY in this shell first (never paste it into a command)}"

# ---- project ---------------------------------------------------------------------------------
if [ -z "${PROJECT_ID:-}" ]; then
    local_part="${ACCOUNT%%@*}"
    PROJECT_ID="consilium-health-$(printf '%s' "$local_part" | tr -cd 'a-z0-9' | cut -c1-12)"
fi
export PROJECT_ID
REGION="${REGION:-us-west1}"

if gcloud projects describe "$PROJECT_ID" >/dev/null 2>&1; then
    echo ">> project ${PROJECT_ID} exists"
else
    echo ">> creating project ${PROJECT_ID}"
    gcloud projects create "$PROJECT_ID" --name="Consilium Health"
fi
gcloud config set project "$PROJECT_ID" >/dev/null

# ---- billing ---------------------------------------------------------------------------------
if gcloud billing projects describe "$PROJECT_ID" --format='value(billingEnabled)' 2>/dev/null | grep -q True; then
    echo ">> billing already enabled"
else
    BILLING="${BILLING_ACCOUNT:-$(gcloud billing accounts list --filter='open=true' --format='value(name)' | head -1)}"
    if [ -z "$BILLING" ]; then
        cat >&2 <<EOF

!! No billing account is visible to ${ACCOUNT}.  Cloud Run, Cloud Build and Vertex AI need one.
   Open https://console.cloud.google.com/billing in a browser, start the free trial (or add a
   card), then re-run this script.  Nothing has been deployed; the project ${PROJECT_ID} exists
   and is empty.
EOF
        exit 2
    fi
    echo ">> linking billing account ${BILLING##*/}"
    gcloud billing projects link "$PROJECT_ID" --billing-account="${BILLING##*/}"
fi

# ---- APIs ------------------------------------------------------------------------------------
echo ">> enabling APIs (first time takes 1-2 minutes)"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
    secretmanager.googleapis.com aiplatform.googleapis.com storage.googleapis.com \
    iam.googleapis.com cloudresourcemanager.googleapis.com

# ---- secret ----------------------------------------------------------------------------------
if gcloud secrets describe openai-api-key >/dev/null 2>&1; then
    echo ">> secret openai-api-key exists (add a version to rotate)"
else
    echo ">> creating secret openai-api-key from \$OPENAI_API_KEY"
    printf '%s' "$OPENAI_API_KEY" | gcloud secrets create openai-api-key --data-file=- --replication-policy=automatic
fi

# ---- service account -------------------------------------------------------------------------
SA="consilium-run@${PROJECT_ID}.iam.gserviceaccount.com"
if gcloud iam service-accounts describe "$SA" >/dev/null 2>&1; then
    echo ">> service account exists"
else
    gcloud iam service-accounts create consilium-run --display-name="Consilium-Health Cloud Run"
    sleep 5  # IAM propagation before the bindings below
fi
gcloud secrets add-iam-policy-binding openai-api-key --member="serviceAccount:${SA}" \
    --role=roles/secretmanager.secretAccessor --quiet >/dev/null
gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:${SA}" \
    --role=roles/aiplatform.user --quiet >/dev/null
echo ">> grants in place for ${SA}"

# ---- deploy ----------------------------------------------------------------------------------
PROVIDER="${PROVIDER:-openai}" MODEL="${MODEL:-gpt-4o-mini}" REGION="$REGION" ./deploy/cloudrun/deploy.sh

# ---- verify ----------------------------------------------------------------------------------
URL="$(gcloud run services describe consilium-health --region "$REGION" --format='value(status.url)')"
echo
echo ">> ONE REAL QUERY against ${URL}/v1/ask"
curl -sS -X POST "${URL}/v1/ask" -H 'content-type: application/json' \
    -d '{"question":"What is the ICD-10 code for type 2 diabetes, and when is E11.9 the right choice?"}' \
    | python3 -m json.tool
echo
echo ">> done.  URL: ${URL}"
echo ">> next: ./deploy/cloudrun/eval-job.sh legacy && ./deploy/cloudrun/eval-job.sh adk"
