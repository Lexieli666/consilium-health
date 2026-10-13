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
PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
export PROJECT_ID
BUCKET="${BUCKET:-${PROJECT_ID}-consilium-eval}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

RUN_NAME="${STAMP}-legacy" ./deploy/cloudrun/eval-job.sh legacy "$@"
RUN_NAME="${STAMP}-adk"    ./deploy/cloudrun/eval-job.sh adk "$@"

mkdir -p eval/results
gsutil -m cp -r "gs://${BUCKET}/${STAMP}-legacy" "gs://${BUCKET}/${STAMP}-adk" eval/results/
echo ">> results in eval/results/${STAMP}-legacy and eval/results/${STAMP}-adk"
