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
#   PROJECT_ID=my-project ./deploy/cloudrun/eval-job.sh legacy     # claude-haiku-5-5, in-repo router
#   PROJECT_ID=my-project ./deploy/cloudrun/eval-job.sh adk        # Vertex AI Gemini, ADK tree
#   PROJECT_ID=my-project ./deploy/cloudrun/eval-job.sh adk --limit 5   # smoke run first
#
# Then:  gsutil -m cp -r "gs://${BUCKET}/${RUN_NAME}" eval/results/
#
# THE JUDGE.  `Judge(runtime.provider)` grades with whatever CONSILIUM_PROVIDER/CONSILIUM_MODEL the
# process is configured with, and both modes below set the same pair -- anthropic /
# claude-haiku-5-5 -- so the two runs are graded by one instrument.  That instrument is **not** the
# one the published baseline used: eval/results/published/ was graded by gpt-4o-mini, the judge
# whose agreement with a human was measured (Cohen's kappa 0.592, n=40, docs/EVALUATION.md §4.2).
# No kappa has been measured for claude-haiku-5-5 on eval/judges/faithfulness_v2.md.  So the
# faithfulness numbers these two runs produce are comparable **to each other** and not to the
# published column, and every table that prints them has to say so.  Re-validating would be
# `--human-sample 40 --sample-seed <new> --exclude-sample <both prior CSVs>` plus 40 human labels;
# that has not been done.
#
# CONSILIUM_ANTHROPIC_MAX_TOKENS: the Anthropic Messages API requires max_tokens and the provider's
# default (1500) truncated the judge's per-claim JSON mid-object on the first smoke run, which the
# harness then scored as "judge could not be parsed".  8192 is the value the README and the LangGraph
# runbook document for an Anthropic sweep; the OpenAI provider the published run used sent no cap.
#
# Cost: both sweeps together are expected well under $2 at the rates in eval/pricing.yaml; the job
# itself is cents.  --max-cost caps the traced spend of the sweep (not the judge) and stops
# between items.

set -euo pipefail

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
: "${PROJECT_ID:?set PROJECT_ID or gcloud config set project}"
MODE="${1:?legacy|adk}"; shift || true
REGION="${REGION:-us-west1}"
SERVICE="${SERVICE:-consilium-health}"
JOB="${JOB:-consilium-eval}"
BUCKET="${BUCKET:-${PROJECT_ID}-consilium-eval}"
PROVIDER="${PROVIDER:-anthropic}"
MODEL="${MODEL:-claude-haiku-5-5}"
ANTHROPIC_MAX_TOKENS="${ANTHROPIC_MAX_TOKENS:-8192}"
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

ENV_VARS="CONSILIUM_PROVIDER=${PROVIDER},CONSILIUM_MODEL=${MODEL},ORCHESTRATOR=${MODE}"
ENV_VARS+=",CONSILIUM_ANTHROPIC_MAX_TOKENS=${ANTHROPIC_MAX_TOKENS}"
ENV_VARS+=",CONSILIUM_ADK_MODEL=${ADK_MODEL},GOOGLE_GENAI_USE_VERTEXAI=true"
ENV_VARS+=",GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=${REGION}"
ENV_VARS+=",CONSILIUM_LOG_FORMAT=json,CONSILIUM_LOG_LEVEL=WARNING,CONSILIUM_RUNS_DIR=/tmp/runs"

# Mount only the provider secret the run needs; the judge and (on `legacy`) the agents both talk to
# CONSILIUM_PROVIDER, so `adk` needs it too even though its agents run on Vertex AI.
case "$PROVIDER" in
  anthropic) SECRET="ANTHROPIC_API_KEY=anthropic-api-key:latest" ;;
  openai)    SECRET="OPENAI_API_KEY=openai-api-key:latest" ;;
  *) echo "PROVIDER must be anthropic or openai for a paid sweep; got ${PROVIDER}" >&2; exit 2 ;;
esac

# The container's argv, comma-joined for --args.  Two details gcloud is strict about: the value
# starts with `-m`, so it has to be passed as `--args=...` (with `--args ...` gcloud reads the
# leading dash as another flag and fails with "expected one argument"), and extra arguments are
# appended only when there are any, because `printf ',%s'` with nothing to format still emits the
# separator and would send an empty argv entry.
ARGS="-m,eval.run,--config,full,--max-cost,${MAX_COST},--out,/results/${RUN_NAME}"
if [ "$#" -gt 0 ]; then
    ARGS+="$(printf ',%s' "$@")"
fi

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
    --set-secrets "$SECRET" \
    --command python \
    --args="${ARGS}" \
    --quiet

echo ">> executing ${JOB} (${MODE}) -> gs://${BUCKET}/${RUN_NAME}"
gcloud run jobs execute "$JOB" --project "$PROJECT_ID" --region "$REGION" --wait
echo ">> done: gsutil -m cp -r gs://${BUCKET}/${RUN_NAME} eval/results/"
