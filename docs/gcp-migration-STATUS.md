# GCP migration — status and handoff (2026-10-14)

Branch `gcp-migration`. **Phases 1, 2 and 3 are done and measured.** The results are in
`docs/gcp-migration-eval.md`; this file is the state of the migration itself.

## Done

**Phase 0 — read.** Entry points, orchestration, Chroma wiring, eval harness and env vars.

**Phase 1 — Cloud Run (deployed and verified, 2026-10-14).**
- Service `consilium-health` in `us-west1`, project `project-36f5f6ea-edf1-4d0c-817`, image
  `…/cloud-run-source-deploy/consilium-health@sha256:d3f4aca2`. URL, `/v1/healthz` output and a
  real `/v1/ask` response are in the README's *Deployed service* block.
- `CONSILIUM_PROVIDER=anthropic`, `CONSILIUM_MODEL=claude-haiku-5-5`,
  `CONSILIUM_ANTHROPIC_MAX_TOKENS=8192`; `ANTHROPIC_API_KEY` from Secret Manager
  (`anthropic-api-key`) under the `consilium-run` service account, whose only grants are reading
  that secret and `roles/aiplatform.user`. min-instances 0, 2 GiB, concurrency 10, unauthenticated.
- The bge-small model and the ChromaDB index are baked at build time; `/v1/healthz` reports
  `corpus_documents: 78` on a cold start, so no start-up rebuild happens.

**Phase 2 — ADK orchestrator (complete, and now measured).** `consilium/adk/`, `ORCHESTRATOR=adk`,
`CONSILIUM_ADK_MODEL` (default `gemini-2.5-flash`), trace-compatible with the legacy path. The
design calls recorded in the previous revision of this file stand: delegation is code-driven from
the planner's parsed JSON rather than `transfer_to_agent`, and validator/repair run at the turn
boundary in code on both paths.

**Phase 3 — the comparison (run 2026-10-14).** Both arms as Cloud Run Jobs on the deployed image,
sequentially, `--max-cost 3` each, judge `claude-haiku-5-5` on both.
`eval/results/20261014T050334Z-{legacy,adk}`. Headline: red-flag recall 92.9% (legacy,
`claude-haiku-5-5`) against 82.1% (ADK, `gemini-2.5-flash`), at 2.1× the cost per turn and a worse
p95. Full table, trace-level causes and the limits of the claim: `docs/gcp-migration-eval.md`.

## What the first real build and the first real deploy cost in fixes

None of this was known before the code ran on GCP; all of it is fixed on this branch.

1. **The Dockerfile had never been built.** It used BuildKit-only syntax (a `# syntax=` frontend
   and three `RUN --mount=type=cache` lines); the `gcr.io/cloud-builders/docker` step that
   `gcloud run deploy --source` generates runs the classic engine, where a cache mount is a parse
   error. The build failed in 10 seconds, before any layer.
2. **Cloud Build's default service account had no permissions** on a fresh project — it could not
   read its own uploaded source (`storage.objects.get` denied). Fixed with
   `roles/cloudbuild.builds.builder` on `<project-number>-compute@developer.gserviceaccount.com`.
3. **`GET /healthz` is unreachable through Cloud Run's frontend**, which answers it with its own
   HTML 404; the request reaches no container and appears in no request log, while every other
   path on the same service returns the app's `{"detail":"Not Found"}` and is logged. Reproduced
   from outside GCP and from a Cloud Build step inside it. The handler is now registered at
   `/healthz` *and* `/v1/healthz`; deployments behind that frontend must use the second.
4. **`eval-job.sh` could not deploy its job**: the container argv was passed as `--args "-m,…"`
   and gcloud read the leading dash as a flag. Now `--args=…`.
5. **`eval-all.sh` interpolated an unchecked project id** into the bucket name, the job environment
   and the `gsutil` URLs; an unset `gcloud config project` produced `gs://-consilium-eval`. It now
   fails fast and rejects placeholder ids by name.
6. **`--limit` did not bound the multi-turn set**, so a 5-item "smoke run" still drove all 29
   conversations — under a minute of golden items followed by twenty minutes of conversations. It
   now limits both.

## Known gaps

- **The comparison moved two variables at once** (orchestrator and agent model), so it cannot
  attribute the red-flag regression to either. A third arm would.
- **The `claude-haiku-5-5` judge has no measured kappa.** Faithfulness from these runs is
  comparable within the pair only. `eval/gcp_compare.py` prints that caveat itself.
- **`summary.json` records `commit: unknown`** for anything run inside the image, because `.git`
  is not shipped. The image digest is the identifier to quote.
- **`eval/gcp_compare.py` reads only the raw run layout** (`runs/<session>/0.jsonl`). Pointed at a
  `published-*` directory it reports `0/150 items` and `p95: not measured` rather than failing.
- The ADK arm's own smoke run was cancelled part-way once its Vertex path was proven; the full
  ADK arm that followed is complete and is what the numbers come from.

## Cost

Traced model spend for the measured pair: $0.366 (legacy) + $0.768 (ADK) = **$1.13**, against a $3
cap per arm that neither approached. The judge's calls are not traced and are not in that figure.
Cloud Build, Cloud Run and storage for the whole exercise are a few dollars more.
