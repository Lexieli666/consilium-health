#!/usr/bin/env bash
# Deploy Consilium-Health to Cloud Run from source (Cloud Build builds the Dockerfile at the repo
# root).  Idempotent: re-running updates the service in place.
#
# Usage:
#   PROJECT_ID=my-project ./deploy/cloudrun/deploy.sh
#
# Knobs (env vars, all optional):
#   REGION            default us-west1
#   SERVICE           default consilium-health
#   PROVIDER          CONSILIUM_PROVIDER for the service: anthropic | openai | mock (default anthropic)
#   MODEL             CONSILIUM_MODEL                                 (default claude-haiku-5-5)
#   ANTHROPIC_MAX_TOKENS  CONSILIUM_ANTHROPIC_MAX_TOKENS                          (default 8192)
#   ORCHESTRATOR      legacy | adk                                                   (default legacy)
#   ADK_MODEL         Gemini model for ORCHESTRATOR=adk                              (default gemini-2.5-flash)
#   MIN_INSTANCES     default 0;  MAX_INSTANCES default 3
#   MEMORY            default 2Gi; CPU default 1; CONCURRENCY default 10
#
# ------------------------------------------------------------------------------------------------
# ONE-TIME SETUP (run once per project, by hand):
#
#   gcloud config set project "$PROJECT_ID"
#   gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
#       artifactregistry.googleapis.com secretmanager.googleapis.com aiplatform.googleapis.com
#
#   # API keys live in Secret Manager and reach the container via --set-secrets; never in an env
#   # var on the command line, never in source.  Only the key for PROVIDER is required (the
#   # script mounts whichever of the two secrets exist).  Read from your shell environment:
#   printf '%s' "$CONSILIUM_ANTHROPIC_API_KEY" | gcloud secrets create anthropic-api-key --data-file=- --replication-policy=automatic
#   printf '%s' "$OPENAI_API_KEY"              | gcloud secrets create openai-api-key    --data-file=- --replication-policy=automatic
#   # To rotate:  printf '%s' "$NEW" | gcloud secrets versions add openai-api-key --data-file=-
#
#   # The service runs as a dedicated service account with exactly two grants: read those secrets,
#   # and call Vertex AI (for ORCHESTRATOR=adk).  Nothing else.
#   gcloud iam service-accounts create consilium-run --display-name="Consilium-Health Cloud Run"
#   SA="consilium-run@${PROJECT_ID}.iam.gserviceaccount.com"
#   gcloud secrets add-iam-policy-binding anthropic-api-key --member="serviceAccount:$SA" --role=roles/secretmanager.secretAccessor
#   gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$SA" --role=roles/aiplatform.user
#
# Cost: Cloud Build ~2-4 min on the default e2 pool per deploy (free tier covers 120 min/day);
# Cloud Run at min-instances 0 bills only while serving; the image is ~1.5 GB in Artifact Registry.
# ------------------------------------------------------------------------------------------------

set -euo pipefail

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
: "${PROJECT_ID:?set PROJECT_ID or gcloud config set project}"
REGION="${REGION:-us-west1}"
SERVICE="${SERVICE:-consilium-health}"
PROVIDER="${PROVIDER:-anthropic}"
MODEL="${MODEL:-claude-haiku-5-5}"
ANTHROPIC_MAX_TOKENS="${ANTHROPIC_MAX_TOKENS:-8192}"
ORCHESTRATOR="${ORCHESTRATOR:-legacy}"
ADK_MODEL="${ADK_MODEL:-gemini-2.5-flash}"
MIN_INSTANCES="${MIN_INSTANCES:-0}"
MAX_INSTANCES="${MAX_INSTANCES:-3}"
MEMORY="${MEMORY:-2Gi}"
CPU="${CPU:-1}"
CONCURRENCY="${CONCURRENCY:-10}"
SA="consilium-run@${PROJECT_ID}.iam.gserviceaccount.com"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

ENV_VARS="CONSILIUM_PROVIDER=${PROVIDER}"
ENV_VARS+=",CONSILIUM_MODEL=${MODEL}"
ENV_VARS+=",CONSILIUM_ANTHROPIC_MAX_TOKENS=${ANTHROPIC_MAX_TOKENS}"
ENV_VARS+=",ORCHESTRATOR=${ORCHESTRATOR}"
ENV_VARS+=",CONSILIUM_ADK_MODEL=${ADK_MODEL}"
ENV_VARS+=",GOOGLE_GENAI_USE_VERTEXAI=true"
ENV_VARS+=",GOOGLE_CLOUD_PROJECT=${PROJECT_ID}"
ENV_VARS+=",GOOGLE_CLOUD_LOCATION=${REGION}"
ENV_VARS+=",CONSILIUM_LOG_FORMAT=json"

# Mount only the secrets that exist, so a project with one provider key deploys without a
# placeholder for the other.
SECRETS=""
for pair in OPENAI_API_KEY=openai-api-key ANTHROPIC_API_KEY=anthropic-api-key; do
    if gcloud secrets describe "${pair#*=}" --project "$PROJECT_ID" >/dev/null 2>&1; then
        SECRETS+="${SECRETS:+,}${pair}:latest"
    fi
done
[ -n "$SECRETS" ] || { echo "no provider secret found; see the header of this script" >&2; exit 2; }

echo ">> deploying ${SERVICE} to ${REGION} in ${PROJECT_ID} (provider=${PROVIDER} orchestrator=${ORCHESTRATOR})"
gcloud run deploy "$SERVICE" \
    --project "$PROJECT_ID" \
    --region "$REGION" \
    --source . \
    --platform managed \
    --allow-unauthenticated \
    --service-account "$SA" \
    --min-instances "$MIN_INSTANCES" \
    --max-instances "$MAX_INSTANCES" \
    --memory "$MEMORY" \
    --cpu "$CPU" \
    --concurrency "$CONCURRENCY" \
    --timeout 300 \
    --port 8080 \
    --set-env-vars "$ENV_VARS" \
    --set-secrets "$SECRETS" \
    --quiet

URL="$(gcloud run services describe "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --format='value(status.url)')"
echo ">> deployed: ${URL}"
echo ">> health:"
# /v1/healthz, not /healthz: Cloud Run's frontend answers `/healthz` itself with its own HTML 404
# and the request never reaches the container.  Same handler, second path (consilium/api/app.py).
curl -fsS "${URL}/v1/healthz"; echo
