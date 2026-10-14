#!/usr/bin/env bash
# One command from a fresh Google account to a running Cloud Run service.  Idempotent: every step
# checks before it creates, so re-running after a failure continues where it stopped.
#
# The provider key is read from the environment, never typed into a command.  For the default
# provider (anthropic) the script accepts either name, so a shell that keeps the app's key under a
# project-scoped name does not have to shadow the one the Anthropic tooling itself reads:
#
#     export CONSILIUM_ANTHROPIC_API_KEY=sk-ant-...     # preferred
#     export ANTHROPIC_API_KEY=sk-ant-...               # also accepted
#
#   ./deploy/cloudrun/bootstrap.sh                      # project id defaults to consilium-health-<your-initials-from-email>
#   PROJECT_ID=my-project ./deploy/cloudrun/bootstrap.sh
#   PROVIDER=openai ./deploy/cloudrun/bootstrap.sh      # reads $OPENAI_API_KEY instead
#
# What it does, in order: project (create if missing) -> billing (link the first billing account the
# account can see; stops with instructions if there is none) -> APIs -> Secret Manager secret from
# the provider key -> service account + the two grants -> deploy.sh -> /healthz -> one real /v1/ask.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

PROVIDER="${PROVIDER:-anthropic}"
MODEL="${MODEL:-claude-haiku-5-5}"

ACCOUNT="$(gcloud config get-value account 2>/dev/null || true)"
if [ -z "$ACCOUNT" ]; then
    echo ">> not logged in; opening the browser for gcloud auth login"
    gcloud auth login --update-adc
    ACCOUNT="$(gcloud config get-value account)"
fi
echo ">> account: ${ACCOUNT}"

# ---- provider key ----------------------------------------------------------------------------
case "$PROVIDER" in
    anthropic)
        SECRET_NAME="anthropic-api-key"
        API_KEY="${CONSILIUM_ANTHROPIC_API_KEY:-${ANTHROPIC_API_KEY:-}}"
        [ -n "$API_KEY" ] || {
            echo "export CONSILIUM_ANTHROPIC_API_KEY (or ANTHROPIC_API_KEY) in this shell first" >&2
            exit 2
        }
        ;;
    openai)
        SECRET_NAME="openai-api-key"
        API_KEY="${OPENAI_API_KEY:-}"
        [ -n "$API_KEY" ] || { echo "export OPENAI_API_KEY in this shell first" >&2; exit 2; }
        ;;
    *) echo "PROVIDER must be anthropic or openai; got ${PROVIDER}" >&2; exit 2 ;;
esac

# ---- project ---------------------------------------------------------------------------------
if [ -z "${PROJECT_ID:-}" ]; then
    PROJECT_ID="$(gcloud config get-value project 2>/dev/null || true)"
fi
if [ -z "${PROJECT_ID:-}" ] || [ "$PROJECT_ID" = "(unset)" ]; then
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
if gcloud secrets describe "$SECRET_NAME" >/dev/null 2>&1; then
    echo ">> secret ${SECRET_NAME} exists (add a version to rotate)"
else
    echo ">> creating secret ${SECRET_NAME} from the environment"
    printf '%s' "$API_KEY" | gcloud secrets create "$SECRET_NAME" --data-file=- --replication-policy=automatic
fi

# ---- service account -------------------------------------------------------------------------
SA="consilium-run@${PROJECT_ID}.iam.gserviceaccount.com"
if gcloud iam service-accounts describe "$SA" >/dev/null 2>&1; then
    echo ">> service account exists"
else
    gcloud iam service-accounts create consilium-run --display-name="Consilium-Health Cloud Run"
    sleep 5  # IAM propagation before the bindings below
fi
gcloud secrets add-iam-policy-binding "$SECRET_NAME" --member="serviceAccount:${SA}" \
    --role=roles/secretmanager.secretAccessor --quiet >/dev/null
gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:${SA}" \
    --role=roles/aiplatform.user --quiet >/dev/null
echo ">> grants in place for ${SA}"

# ---- deploy ----------------------------------------------------------------------------------
PROVIDER="$PROVIDER" MODEL="$MODEL" REGION="$REGION" ./deploy/cloudrun/deploy.sh

# ---- verify ----------------------------------------------------------------------------------
URL="$(gcloud run services describe consilium-health --region "$REGION" --format='value(status.url)')"
echo
echo ">> ONE REAL QUERY against ${URL}/v1/ask"
curl -sS -X POST "${URL}/v1/ask" -H 'content-type: application/json' \
    -d '{"question":"What is the ICD-10 code for type 2 diabetes, and when is E11.9 the right choice?"}' \
    | python3 -m json.tool
echo
echo ">> done.  URL: ${URL}"
echo ">> next: ./deploy/cloudrun/eval-all.sh --limit 5   (smoke), then ./deploy/cloudrun/eval-all.sh"
