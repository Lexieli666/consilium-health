#!/usr/bin/env bash
# Phase 3 in one command: both 150-item sweeps as Cloud Run Jobs on the deployed image, then the
# results copied into eval/results/ for docs/gcp-migration-eval.md.  Sequential on purpose (the
# harness measures latency).  Expected spend: well under $2 total; each sweep is capped at $3
# of traced calls by eval-job.sh.
#
#   ./deploy/cloudrun/eval-all.sh            # full runs
#   ./deploy/cloudrun/eval-all.sh --limit 5  # smoke: 5 items per mode first
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."

# The project id is the one thing every step below interpolates -- into the bucket name, the job's
# environment and the gsutil URLs -- so it is checked here rather than failing three commands later
# with `gs://-consilium-eval`.  An unset `gcloud config project` yields the empty string, and the
# placeholder ids the setup docs use are rejected by name: a bucket called
# `your-real-project-id-consilium-eval` is a typo that costs a deploy to discover.
PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"
case "${PROJECT_ID:-}" in
    "" | "(unset)" | your-real-project-id | your-project-id | my-project | PROJECT_ID)
        cat >&2 <<EOF
!! PROJECT_ID is ${PROJECT_ID:-empty}, which is not a real project.  Set one of:

     gcloud config set project <your-project-id>
     PROJECT_ID=<your-project-id> ./deploy/cloudrun/eval-all.sh $*

   Nothing has been run and nothing has been billed.
EOF
        exit 2
        ;;
esac
export PROJECT_ID
BUCKET="${BUCKET:-${PROJECT_ID}-consilium-eval}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

echo ">> project ${PROJECT_ID}, results bucket gs://${BUCKET}, run stamp ${STAMP}"

RUN_NAME="${STAMP}-legacy" ./deploy/cloudrun/eval-job.sh legacy "$@"
RUN_NAME="${STAMP}-adk"    ./deploy/cloudrun/eval-job.sh adk "$@"

mkdir -p eval/results
gsutil -m cp -r "gs://${BUCKET}/${STAMP}-legacy" "gs://${BUCKET}/${STAMP}-adk" eval/results/
echo ">> results in eval/results/${STAMP}-legacy and eval/results/${STAMP}-adk"
echo ">> next: uv run python -m eval.gcp_compare eval/results/${STAMP}-legacy eval/results/${STAMP}-adk"
