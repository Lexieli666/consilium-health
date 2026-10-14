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
#   PROJECT_ID=my-project ./deploy/cloudrun/eval-job.sh legacy        # claude-haiku-5-5, in-repo router
#   PROJECT_ID=my-project ./deploy/cloudrun/eval-job.sh adk           # Vertex AI Gemini, ADK tree
#   PROJECT_ID=my-project ./deploy/cloudrun/eval-job.sh legacy-gemini # the in-repo router ON Gemini
#   PROJECT_ID=my-project ./deploy/cloudrun/eval-job.sh adk --limit 5 # smoke run first
#
# `legacy-gemini` is the third arm of docs/gcp-migration-eval.md: it holds the orchestrator fixed
# and moves only the agent model, which is what separates "the orchestrator changed" from "the
# model changed" in the legacy-against-adk comparison.  It needs no provider secret -- Vertex
# authenticates with the job's service account -- but it still mounts the Anthropic one, because
# the judge does not move with the agents (see below).
#
# Then:  gsutil -m cp -r "gs://${BUCKET}/${RUN_NAME}" eval/results/
#
# THE JUDGE.  Pinned with CONSILIUM_JUDGE_PROVIDER / CONSILIUM_JUDGE_MODEL to anthropic /
# claude-haiku-5-5 on every arm, so all three runs are graded by one instrument.  Without the pin
# the judge is whatever the agents run on, which is harmless for one run and wrong for a
# comparison: the `legacy-gemini` arm sets CONSILIUM_PROVIDER=vertexai, so its judge would silently
# become gemini-2.5-flash and its faithfulness column would compare two instruments rather than two
# systems.  That instrument is **not** the
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
MODE="${1:?legacy|adk|legacy-gemini}"; shift || true
REGION="${REGION:-us-west1}"
SERVICE="${SERVICE:-consilium-health}"
JOB="${JOB:-consilium-eval}"
BUCKET="${BUCKET:-${PROJECT_ID}-consilium-eval}"
PROVIDER="${PROVIDER:-anthropic}"
MODEL="${MODEL:-claude-haiku-5-5}"
JUDGE_PROVIDER="${JUDGE_PROVIDER:-anthropic}"
JUDGE_MODEL="${JUDGE_MODEL:-claude-haiku-5-5}"
ANTHROPIC_MAX_TOKENS="${ANTHROPIC_MAX_TOKENS:-8192}"
ADK_MODEL="${ADK_MODEL:-gemini-2.5-flash}"
MAX_COST="${MAX_COST:-3}"
# 6h, not the 3h this started at.  A measured arm is 1h30m-2h, but the third arm
# (legacy router on gemini-2.5-flash) ran the 150-item sweep in 45 min and then spent over an hour
# on the 29-conversation multi-turn set, finishing within minutes of a 3h cap -- and a task that
# hits the cap writes no summary.json at all, losing the whole arm rather than part of it.  The cap
# is a runaway guard, not a schedule, so it is set well clear of the work.
TASK_TIMEOUT="${TASK_TIMEOUT:-6h}"
SA="consilium-run@${PROJECT_ID}.iam.gserviceaccount.com"
RUN_NAME="${RUN_NAME:-$(date -u +%Y%m%dT%H%M%SZ)-${MODE}}"

# ORCHESTRATOR is not the same thing as MODE: the third arm runs the *legacy* orchestrator on a
# different provider, so the run name and the environment have to be set separately.
case "$MODE" in
  legacy|adk)
    ORCHESTRATOR="$MODE"
    ;;
  legacy-gemini)
    ORCHESTRATOR="legacy"
    PROVIDER="vertexai"
    MODEL="${GEMINI_MODEL:-gemini-2.5-flash}"
    ;;
  *) echo "mode must be legacy, adk or legacy-gemini" >&2; exit 2 ;;
esac

IMAGE="$(gcloud run services describe "$SERVICE" --project "$PROJECT_ID" --region "$REGION" \
          --format='value(spec.template.spec.containers[0].image)')"
echo ">> image: ${IMAGE}"

gsutil ls -b "gs://${BUCKET}" >/dev/null 2>&1 || gsutil mb -p "$PROJECT_ID" -l "$REGION" "gs://${BUCKET}"
gsutil iam ch "serviceAccount:${SA}:roles/storage.objectAdmin" "gs://${BUCKET}" >/dev/null

ENV_VARS="CONSILIUM_PROVIDER=${PROVIDER},CONSILIUM_MODEL=${MODEL},ORCHESTRATOR=${ORCHESTRATOR}"
ENV_VARS+=",CONSILIUM_JUDGE_PROVIDER=${JUDGE_PROVIDER},CONSILIUM_JUDGE_MODEL=${JUDGE_MODEL}"
ENV_VARS+=",CONSILIUM_ANTHROPIC_MAX_TOKENS=${ANTHROPIC_MAX_TOKENS}"
ENV_VARS+=",CONSILIUM_ADK_MODEL=${ADK_MODEL},GOOGLE_GENAI_USE_VERTEXAI=true"
ENV_VARS+=",GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=${REGION}"
ENV_VARS+=",CONSILIUM_LOG_FORMAT=json,CONSILIUM_LOG_LEVEL=WARNING,CONSILIUM_RUNS_DIR=/tmp/runs"

# The secret the run needs is the *judge's*, plus the agents' when they differ.  Vertex needs no
# secret at all (the service account authenticates it), which is why `legacy-gemini` mounts only
# the Anthropic key its judge uses.
SECRET=""
for who in "$PROVIDER" "$JUDGE_PROVIDER"; do
    case "$who" in
      anthropic) pair="ANTHROPIC_API_KEY=anthropic-api-key:latest" ;;
      openai)    pair="OPENAI_API_KEY=openai-api-key:latest" ;;
      vertexai)  continue ;;
      *) echo "provider must be anthropic, openai or vertexai; got ${who}" >&2; exit 2 ;;
    esac
    case ",${SECRET}," in *",${pair},"*) continue ;; esac
    SECRET+="${SECRET:+,}${pair}"
done
[ -n "$SECRET" ] || { echo "no provider secret needed? refusing to run unjudged" >&2; exit 2; }

# The container's argv, comma-joined for --args.  Two details gcloud is strict about: the value
# starts with `-m`, so it has to be passed as `--args=...` (with `--args ...` gcloud reads the
# leading dash as another flag and fails with "expected one argument"), and extra arguments are
# appended only when there are any, because `printf ',%s'` with nothing to format still emits the
# separator and would send an empty argv entry.
# The cap is formatted to two decimals so its token can never be character-identical to a small
# `--limit`: gcloud parses --args as a comma-separated list and rejects a repeated value outright
# (`argument --args: "3" cannot be specified multiple times`), so `--max-cost 3 --limit 3` fails to
# deploy with an error that says nothing about either flag.
MAX_COST_ARG="$(printf '%.2f' "$MAX_COST")"
ARGS="-m,eval.run,--config,full,--max-cost,${MAX_COST_ARG},--out,/results/${RUN_NAME}"
if [ "$#" -gt 0 ]; then
    ARGS+="$(printf ',%s' "$@")"
fi

# `deploy` creates or updates; the job's spec is identical between the two modes except the
# environment, which is the point.
gcloud run jobs deploy "$JOB" \
    --project "$PROJECT_ID" --region "$REGION" \
    --image "$IMAGE" \
    --service-account "$SA" \
    --memory 2Gi --cpu 1 --tasks 1 --max-retries 0 --task-timeout "${TASK_TIMEOUT}" \
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
