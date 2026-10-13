#!/usr/bin/env bash
# Run the 150-item golden set as a Cloud Run Job on the *deployed* image -- same container, same
# environment, same region as the service -- and leave the results (summary.json, report.md and
# every per-turn trace) in a GCS bucket.
#
# The harness is in-process by design (eval/harness.py drives `run_turn`, the function the HTTP
# API calls), so "against the Cloud Run deployment" means "inside the Cloud Run image", not "over
# HTTP": a harness that drove the system over a different path would measure a system nobody
# ships, and the traces it needs are written by the process that runs the turn.
#
# Usage:
#   PROJECT_ID=my-project ./deploy/cloudrun/eval-job.sh legacy     # gpt-4o-mini, in-repo router
#   PROJECT_ID=my-project ./deploy/cloudrun/eval-job.sh adk        # Vertex AI Gemini, ADK tree
#   PROJECT_ID=my-project ./deploy/cloudrun/eval-job.sh adk --limit 5   # smoke run first
#
# Then:  gsutil -m cp -r "gs://${BUCKET}/${RUN_NAME}" eval/results/
#
# The judge is gpt-4o-mini (the kappa-validated judge, eval/judges/faithfulness_v2.md) on BOTH
# runs: `Judge(runtime.provider)` uses CONSILIUM_PROVIDER, which stays openai; ORCHESTRATOR=adk
# changes only the routing layer and the model the agents run on.
#
# Cost: both sweeps together are expected well under $2 at the rates in eval/pricing.yaml (the
# published 650-item sweep on gpt-4o-mini cost $0.53); the job itself is cents.  --max-cost caps
# the traced spend of the sweep (not the judge) and stops between items.

set -euo pipefail

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
: "${PROJECT_ID:?set PROJECT_ID or gcloud config set project}"
MODE="${1:?legacy|adk}"; shift || true
REGION="${REGION:-us-west1}"
SERVICE="${SERVICE:-consilium-health}"
JOB="${JOB:-consilium-eval}"
BUCKET="${BUCKET:-${PROJECT_ID}-consilium-eval}"
MODEL="${MODEL:-gpt-4o-mini}"
ADK_MODEL="${ADK_MODEL:-gemini-2.5-flash}"
MAX_COST="${MAX_COST:-3}"
SA="consilium-run@${PROJECT_ID}.iam.gserviceaccount.com"
RUN_NAME="${RUN_NAME:-$(date -u +%Y%m%dT%H%M%SZ)-${MODE}}"

case "$MODE" in
  legacy|adk) ;;
  *) echo "mode must be legacy or adk" >&2; exit 2 ;;
esac

IMAGE="$(gcloud run services describe "$SERVICE" --project "$PROJECT_ID" --region "$REGION" \
          --format='value(spec.template.spec.containers[0].image)')"
echo ">> image: ${IMAGE}"

gsutil ls -b "gs://${BUCKET}" >/dev/null 2>&1 || gsutil mb -p "$PROJECT_ID" -l "$REGION" "gs://${BUCKET}"
gsutil iam ch "serviceAccount:${SA}:roles/storage.objectAdmin" "gs://${BUCKET}" >/dev/null

ENV_VARS="CONSILIUM_PROVIDER=openai,CONSILIUM_MODEL=${MODEL},ORCHESTRATOR=${MODE}"
ENV_VARS+=",CONSILIUM_ADK_MODEL=${ADK_MODEL},GOOGLE_GENAI_USE_VERTEXAI=true"
ENV_VARS+=",GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=${REGION}"
ENV_VARS+=",CONSILIUM_LOG_FORMAT=json,CONSILIUM_LOG_LEVEL=WARNING,CONSILIUM_RUNS_DIR=/tmp/runs"

# `deploy` creates or updates; the job's spec is identical between the two modes except the
# environment, which is the point.
gcloud run jobs deploy "$JOB" \
    --project "$PROJECT_ID" --region "$REGION" \
    --image "$IMAGE" \
    --service-account "$SA" \
    --memory 2Gi --cpu 1 --tasks 1 --max-retries 0 --task-timeout 3h \
    --add-volume "name=results,type=cloud-storage,bucket=${BUCKET}" \
    --add-volume-mount "volume=results,mount-path=/results" \
    --set-env-vars "$ENV_VARS" \
    --set-secrets "OPENAI_API_KEY=openai-api-key:latest" \
    --command python \
    --args "-m,eval.run,--config,full,--max-cost,${MAX_COST},--out,/results/${RUN_NAME}$(printf ',%s' "$@")" \
    --quiet

echo ">> executing ${JOB} (${MODE}) -> gs://${BUCKET}/${RUN_NAME}"
gcloud run jobs execute "$JOB" --project "$PROJECT_ID" --region "$REGION" --wait
echo ">> done: gsutil -m cp -r gs://${BUCKET}/${RUN_NAME} eval/results/"
