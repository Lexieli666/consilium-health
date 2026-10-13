# GCP migration — status and handoff (2026-10-13)

Branch `gcp-migration` (pushed to `origin`). Nothing below is a measured result; the eval has
**not** run yet. This file is the resume point.

## Done

**Phase 0 — read.** Entry points, orchestration, Chroma wiring, eval harness and env vars are
summarised in the session log; nothing changed.

**Phase 1 — Cloud Run (code complete, not yet deployed).**
- `Dockerfile`: python:3.12-slim, non-root, bge-small model **and the ChromaDB index baked at
  build time** from `data/corpus`; honours Cloud Run's `$PORT`; ships `eval/` so the golden set
  runs as a Cloud Run Job on the deployed image. `.dockerignore` updated; compose on port 8080.
- `deploy/cloudrun/deploy.sh`: `gcloud run deploy --source`, us-west1, min 0 / 2Gi / concurrency
  10, unauthenticated, provider keys via `--set-secrets` (mounts whichever secrets exist).
- `deploy/cloudrun/bootstrap.sh`: one idempotent command — project, billing link, APIs, Secret
  Manager secret from `$OPENAI_API_KEY`, service account + the two grants, deploy, `/healthz`,
  one real `/v1/ask`.
- README "Deployment" section rewritten; it still contains the literal placeholder
  `{{DEPLOYMENT_VERIFICATION}}` to be replaced with the real URL + responses after the deploy.
- Not validated: no Docker daemon in the sandbox and pytorch.org / huggingface.co are blocked
  there, so **the first Cloud Build is the first real build of this Dockerfile.**

**Phase 2 — ADK orchestrator (complete, green).**
- `consilium/adk/`: `ConsiliumRootAgent` (ADK `BaseAgent`) delegating to `planner`,
  `consultation`, `diagnostic`, `research`, `synthesizer` `LlmAgent`s; the 7 skills,
  `validate_output` and `repair_output` wrapped as ADK tools over the existing registry /
  validator / repair objects (`tools.py`). Budgets enforced in `before_tool` / `before_model`.
- Trace compatibility via callbacks: `llm_call` (planner / agent:* / forced_answer /
  synthesizer, `provider=vertexai`, thinking tokens counted as completion), `tool_call`,
  `retrieval`, `safety`, `blackboard`, `turn` — schema v2 unchanged, harness unchanged.
- `ORCHESTRATOR=legacy|adk` (default legacy), `CONSILIUM_ADK_MODEL` (default `gemini-2.5-flash`;
  Google lists its retirement as "not before 2026-10-16"). `/healthz` reports `orchestrator`.
- MCP server needed no change (serves the registry, not the router) — not stubbed.
- `google-adk>=2.0,<3` added as a core dependency; `uv.lock` updated.
- ruff, `ruff format`, mypy `--strict`: clean. 1589 tests pass; 4 new offline ADK smoke tests
  (`tests/test_adk_orchestrator.py`, scripted `BaseLlm` registered in ADK's model registry).
- Design calls (agreed): delegation is code-driven from the planner's parsed JSON, not
  `transfer_to_agent` (fan-out to ≤3 workers + fixed-precedence merge; `route` event needs an
  explicit plan); validator/repair run at the turn boundary in code on both paths, the ADK tool
  wrappers exist for ADK-native deployments only.

**Phase 3 — prepared, not run.**
- `deploy/cloudrun/eval-job.sh <legacy|adk>`: Cloud Run Job on the deployed image, results to
  `gs://<project>-consilium-eval/<run>`; judge stays gpt-4o-mini on both runs; `--max-cost 3`.
- `deploy/cloudrun/eval-all.sh [--limit N]`: both sweeps sequentially, then `gsutil cp` into
  `eval/results/`.
- `eval/pricing.yaml`: dated list prices — openai/gpt-4o-mini (both ids) 0.15/0.60,
  vertexai/gemini-2.5-flash 0.30/2.50 per 1M tokens.
- `eval/gcp_compare.py`: side-by-side table (recall@5, faithfulness, red-flag recall, p50/p90/
  **p95**, cost/request) plus per-item deltas with trace paths; reads runs through
  `eval/metrics.py`, touches no existing eval code.

## Where it stopped

`bootstrap.sh` exited at its first check: `OPENAI_API_KEY` was not exported in the shell. gcloud
was installed (Homebrew `gcloud-cli`) and authenticated as `lexieli@engineering.upenn.edu`, but
that account owns **no GCP project** and `gcloud config` still holds the placeholder project id
`your-real-project-id`. Expected spend for the whole day: well under $2 of model calls.

## To resume (your terminal, repo root)

```bash
gcloud config unset project
export OPENAI_API_KEY=sk-...                 # type it here, never paste it anywhere else
./deploy/cloudrun/bootstrap.sh               # stops with instructions if no billing account
./deploy/cloudrun/eval-all.sh --limit 5      # smoke
./deploy/cloudrun/eval-all.sh                # full: ~60-90 min, two folders in eval/results/
```

Then: fill the README `{{DEPLOYMENT_VERIFICATION}}` block with the URL and the two responses;
run `uv run python -m eval.gcp_compare eval/results/<legacy> eval/results/<adk>` and write
`docs/gcp-migration-eval.md` (model id + date in the table, regressions with trace-level cause);
commit the raw traces; finish with 5 resume bullets from measured numbers only.
