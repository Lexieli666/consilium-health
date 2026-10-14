# Changelog

> **Not medical advice.** This is an educational software project. It does not diagnose, treat, or
> provide clinical guidance, and it must not be used for real medical decisions. No patient data of
> any kind may be used with it.

All notable changes to this project are recorded here, in the format of
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Every entry names the commit it landed in and the date that commit carries, because the point of a
changelog in a repository whose README publishes measured numbers is that a reader can go and look.
**No date here is a release date**: `v0.1.0` has not been tagged, so its heading carries no date
rather than one chosen to look like a release. There is no comparison-link section for the same
reason `pyproject.toml` still has no `[project.urls]` — the repository has not been created, and a
link to a URL that does not resolve is worse than its absence.

## [0.1.0] — unreleased

The first release: ten build phases, one published evaluation run, and an MCP server over the same
skills. Everything below is in `main` and none of it is a plan.

### Added

- **Scaffold, the frozen trace schema, and the offline seams.** `SCHEMA_VERSION`, seven event types
  validated by a discriminated union, one turn-scoped `Tracer` writing
  `runs/<session>/<turn>.jsonl`, `Settings`/`RunConfig` with the five ablation presets, structured
  logging, and the `Embedder`/`VectorStore` protocols that let `pytest -m "not network"` pass with
  no key, no download and no network. (`ae02787`, 2026-08-09)
- **The corpus: 78 educational reference notes across five categories, and the contract they are
  held to.** Five front-matter keys in order, `doc_id` == filename stem, a byte-identical
  disclaimer stripped before chunking, US spelling with a fixed alternate-spelling allowlist, and
  per-category `doc_id` patterns — all enforced by the loader and linted by `tests/test_corpus.py`
  rather than upheld by hand. (`7d2a379`, `352dc34`, `897d674`, `a3fff0e`, 2026-08-17)
- **The red-flag table and its negation-guarded matcher.** Literal emergency phrases in
  `data/red_flags.yaml`, a guard that suppresses a match behind an explicit negation cue, and both
  policies recorded on every turn so the choice between them is settled by measurement rather than
  by argument. (`036e271`, 2026-08-17)
- **The symptom-to-body-system map** behind `analyze_symptoms`. (`683c622`, 2026-08-17)
- **Hybrid retrieval: chunking, BM25, RRF fusion, and `consilium ingest`.** Even-division chunking
  to exactly four chunks per note, one tokenizer shared by the lexical and dense paths, reciprocal
  rank fusion at depth 20 with per-`doc_id` dedup, and the full fused top-10 recorded in the trace
  so MRR@10 stays computable. (`352f490`, 2026-08-18)
- **The skills layer: seven atomic tools, one registry, one envelope.** Tool schemas derived from
  Pydantic argument models with no hand-written JSON anywhere; a skill never raises into its caller
  — every failure comes back as `ok=False` with a `tool_call` event. (`9d73129`, 2026-08-20)
- **The agent layer: the shared ReAct loop, three specialists, `data/policy.yaml`, and the turn
  boundary.** Budgets enforced by the loop rather than requested in the prompt, permitted skills
  read from the policy file at construction, and `consilium/runtime.py` as the one composition root
  every entry point goes through. (`b24de2d`, 2026-08-20)
- **The router: planner, blackboard, parallel execution, fixed-precedence synthesis.** One planner
  call with a total fallback, deterministic subtask ids, workers that can read only their own
  assignment, one shared deadline, and a merge whose conflict precedence is code rather than a
  second LLM call. (`7850b83`, 2026-08-20)
- **Memory: session-keyed working memory, deterministic context compaction, episodic recall.** A
  five-exchange verbatim window with everything older compacted by extraction rather than by a
  generated summary, session state injected per turn and never held process-wide, and SQLite-backed
  cross-session recall that is off in every measured run. (`3ad2373`, 2026-08-20)
- **The safety layer: policy schema 2, the validator, output repair, and `safety` events.**
  Detection and repair as two classes because they are two counts; a fixed repair order of redact →
  escalation banner → disclaimer; a forbidden sentence removed rather than rewritten.
  (`aa3c861`, 2026-08-20)
- **The evaluation harness: items, metrics, judge, report, runner, `consilium eval`.** Every metric
  computed from trace events and nothing else, `None` rendered as `not measured` and never as
  `0.0`, recall@5 reported three ways, routing accuracy reported unconditionally beside the
  fallback-excluded figure, and red-flag recall reported with its raw false-negative count and the
  item ids. (`de69d99`, 2026-08-20)
- **The golden set: 150 hand-labelled items and 29 multi-turn conversations, frozen with their
  digests.** `expected_route` and `red_flag` labelled blind; red-flag items stratified into 22
  hard-phrasing and 5 easy-phrasing questions whose recall is never pooled; the 30 coding items
  varied along six ICD-10 conventions rather than along the condition axis; `m-017` rejected whole
  and not replaced. (`2eaac4e` … `720045d`, 2026-08-20 to 2026-08-22)
- **The interfaces: `consilium chat`, the HTTP API, and the SSE stream.** `POST /v1/ask`,
  `POST /v1/chat`, `GET /v1/sessions/{id}` (structure, never content), `GET /healthz`; the
  escalation banner decided from the question and emitted before the first provider call; per-
  session locks so two requests on one session cannot write one trace file.
  (`cafa5ac`, 2026-08-22)
- **The single-file demo page**, served by the API itself at `GET /` so the page and the endpoint
  share an origin and no CORS policy is opened for a demo. (`d0a5d51`, 2026-08-22)
- **`--max-cost USD`, the sweep's spend guard.** Enforced between items so a killed turn cannot
  drop an item out of every denominator, with an abort recorded as a result and its own exit code.
  (`f19584a`, 2026-08-29)
- **The published evaluation run**, `eval/results/published/` — summary and report copied byte for
  byte, 679 traces refolded per session, a manifest of sha256 digests the test suite recomputes.
  `docs/SAFETY.md`, and the README's results with the negative headline finding stated as the
  headline. (`e725b55`, 2026-08-30)
- **Four worked failure cases** from the published run, `docs/FAILURE_CASES.md`, every quoted
  string re-verified byte for byte against the published tree. (`01461b5`, 2026-08-30)
- **The MCP server.** `consilium/mcp_server.py` and `consilium mcp-serve`, serving the same seven
  skills over stdio and streamable HTTP. Tool schemas are the registry's own
  `Skill.parameters_schema()` rather than a second derivation; every result goes through the same
  `PolicyValidator` and `OutputRepair` an answer does, because MCP callers are untrusted callers;
  every invocation emits the existing `tool_call` event carrying `transport: "mcp"`. Contract tests
  assert each schema through an SDK client, over both an in-memory transport and a real subprocess,
  offline. (this release)
- **`ORCHESTRATOR=langgraph`: the turn as a LangGraph `StateGraph`** (`consilium/graph/`) —
  planner with the seven MCP tools bound, `ToolNode` executing them in the project's own MCP
  server (in-process, stdio child or streamable-HTTP URL), synthesizer, validator with a bounded
  validator → synthesizer repair loop (`CONSILIUM_GRAPH_MAX_REPAIRS`, default 2), `MemorySaver`
  keyed by session id, LangSmith tracing from the `LANGSMITH_*` variables. Behind the same
  `Orchestrator` protocol and turn boundary as `legacy` and `adk`, with the server-side `tool_call`
  and `retrieval` events forwarded into the turn trace so the harness reads it unchanged.
  `langchain-mcp-adapters` is not used: every release pins `mcp<2.0`, and the server is on mcp 2.x.
  `python -m consilium.graph "<question>"` runs one turn and prints its trace. Not measured: no
  evaluation run has been executed for it. (`2c2f90f`, 2026-10-13)
- **`eval/graph_compare.py` and `docs/LANGGRAPH_EVAL.md`**: the published ablation rows beside a
  LangGraph run, with p95 latency from the traces and per-item deltas carrying evidence and a
  suggested failure category. (`6f0f200`, `46b1b71`, 2026-10-13)
- **`CONSILIUM_ANTHROPIC_MAX_TOKENS`**, because the Anthropic provider's 1500-token default
  truncated the faithfulness judge; and the graph's synthesizer no longer sends a transcript that
  ends on an assistant turn, which `claude-haiku-5-5` rejects. (`689d9a3`, 2026-10-13)
- **`eval/pricing.yaml`: `anthropic/claude-haiku-5-5`** at $0.10 / $0.50 per million tokens,
  platform.claude.com on 2026-10-13. (`e80a993`)
- **LangGraph v2 (`CONSILIUM_GRAPH_VERSION=2`, the default):** `search_knowledge` runs on every
  turn before the planner, by code, with the raw question, outside the planner's budget; v1 stays
  selectable. (`f5bae8a`, 2026-10-14)
- **Five `claude-haiku-5-5` evaluation runs** of the `full` preset — legacy and LangGraph v1 on
  the offline and the bge retriever, LangGraph v2 on bge — four of them published under
  `eval/results/published-haiku-*/` with manifests (`76eab63`); a first v2 run lost the API credit
  balance mid-judge and was rerun in full and published (`published-haiku-bge-langgraph-v2/`).
  The numbers and the per-item classification are in `docs/LANGGRAPH_EVAL.md`. (2026-10-14)

### Changed

- **A third evaluation arm attributes the red-flag regression to the agent model.** The legacy
  router on `gemini-2.5-flash` (`CONSILIUM_PROVIDER=vertexai`, new in this release) scores red-flag
  recall 82.1% — identical to the ADK tree on the same model, against the legacy router's 92.9% on
  `claude-haiku-5-5`. Changing only the model moves the metric 10.7 points; changing only the
  orchestrator moves it zero. `docs/gcp-migration-eval.md`; raw data in
  `eval/results/published-gcp-legacy-gemini/`. (this release)
- **A Vertex AI Gemini provider for the legacy path** (`consilium/llm/vertex_provider.py`,
  `CONSILIUM_PROVIDER=vertexai`). Before it, `CONSILIUM_PROVIDER` was `openai | anthropic | mock`
  and Vertex was reachable only through the ADK tree, so "the same orchestrator on a different
  model" was not a run anyone could make. Uses `google-genai`, already a `google-adk` dependency,
  so it adds no package; authenticates with Application Default Credentials, so it holds no key;
  labels its calls `vertexai` and counts thinking tokens as output, so one `eval/pricing.yaml` rate
  prices it and the ADK path alike. (this release)
- **The evaluation judge can be pinned away from the agent provider**
  (`CONSILIUM_JUDGE_PROVIDER` / `CONSILIUM_JUDGE_MODEL`). Unset, the judge is whatever the agents
  run on, which is right for one run and wrong for a comparison: the third arm moves
  `CONSILIUM_PROVIDER` itself, and without the pin its judge would have silently become
  `gemini-2.5-flash` and its faithfulness column would have compared two instruments rather than
  two systems. (this release)
- **`eval/gcp_compare.py` takes any number of runs**, one column each, with per-item deltas against
  the first. Three columns is what the attribution needs, and a hand-assembled third column would
  have been the one number in that document nobody could recompute. (this release)

- **The ADK orchestrator has its first evaluation run.** Both arms over the 150-item golden set
  and the 29-conversation multi-turn set, as Cloud Run Jobs on the same deployed image, judged by
  `claude-haiku-5-5` on both sides: red-flag recall 92.9% (legacy, `claude-haiku-5-5`) against
  82.1% (ADK, `gemini-2.5-flash`), recall@5 94.6% against 93.3%, $0.00244 against $0.00512 per
  turn, p95 20.92 s against 35.25 s. The regression is the project's existing documented
  mechanism — `assess_risk` returns a non-match and the draft restates it instead of escalating —
  reproduced on a different agent model, and the run moved orchestrator and model at once so it
  cannot attribute it to either. `docs/gcp-migration-eval.md`; raw data in
  `eval/results/20261014T050334Z-{legacy,adk}`. (this release)

- **The Cloud Run deployment is real.** `deploy/cloudrun/deploy.sh` and `bootstrap.sh` now default
  to `anthropic` / `claude-haiku-5-5` and mount `anthropic-api-key` from Secret Manager rather than
  requiring an OpenAI key; `bootstrap.sh` reads `CONSILIUM_ANTHROPIC_API_KEY` or `ANTHROPIC_API_KEY`
  so a shell that keeps the app's key under a project-scoped name does not have to shadow the one
  the Anthropic tooling reads. The service URL and the two verification responses are in the
  README's *Deployed service* block. (this release)
- **`eval/gcp_compare.py` prints the judge caveat itself.** When a run was graded by anything other
  than `gpt-4o-mini` — the only judge on this project whose agreement with a human was measured —
  the table is followed by a paragraph saying so and naming what re-validation would take. A caveat
  that has to be re-typed every time the table is regenerated is a caveat that will eventually be
  left off the regeneration that matters. (this release)

- **README rewritten as a handoff document**: overview, both orchestrators with the graph diagram,
  usage per interface, the results table with the LangGraph column marked `not run`, limitations
  with trace references, guardrails, the Cortex Search comparison, handoff guidance, next steps.
  (this release)

- **Python floor raised to 3.12**, `requires-python = ">=3.12,<3.14"`, CI on 3.12 and 3.13, and the
  two `TypeAlias` aliases converted to PEP 695 `type` statements. Type-checking happens at the
  floor, and the upper bound does not advertise an interpreter CI never runs. (`ef422e9`,
  2026-08-17)
- **`phrasing_stratum` promoted to a field on `GoldenItem`** from a marker string inside
  `draft_notes`, so a published per-stratum recall figure cannot move when prose is edited.
  (`25b7a2c`, 2026-08-20)
- **`ToolCallEvent` gained `transport`,** defaulting to `"internal"`. `SCHEMA_VERSION` stays at 2:
  it moves when an event's *required* fields change, and the default is a true statement about
  every `tool_call` written before the field existed, so records from either side pool without
  translation. (this release)
- **`configure_logging` gained a `stream` argument.** The MCP stdio transport owns stdout, where a
  log line is a protocol error rather than a cosmetic one. (this release)
- **Relicensed from MIT to Apache-2.0.** The repository has no forks and no outside contributors,
  so the copyright holder is the only party whose permission the change needs. Commits `e725b55`
  through `01461b5` were published under MIT and a copy taken in that window stays under it.
  (`60b232e`, 2026-08-30)

### Fixed

- **The first real Cloud Build could not parse the Dockerfile.** It used BuildKit-only syntax — a
  `# syntax=` frontend and three `RUN --mount=type=cache` lines — and the `gcr.io/cloud-builders/
  docker` step that `gcloud run deploy --source` generates runs the classic engine, where a cache
  mount is a hard parse error (`the --mount option requires BuildKit`) rather than an ignored
  optimisation. The build failed in ten seconds, before any layer. The mounts only ever sped up
  repeat local builds; the layer cache still covers the dependency layer. (this release)
- **`GET /healthz` is unreachable on Cloud Run, so the handler now answers at `/v1/healthz` too.**
  Google's frontend answers `/healthz` itself with its own HTML 404 and the request never reaches
  the container: on the deployed service every other path returns this app's
  `{"detail":"Not Found"}` and is written to the request log, while `/healthz` appears in no
  request log at all — reproduced both from outside GCP and from a Cloud Build step inside it. The
  route and the `$PORT` binding were never the problem. `/healthz` is kept for `docker compose`'s
  healthcheck and the local commands; `/v1/healthz` is what anything behind that frontend calls.
  (this release)
- **`eval-job.sh` could not deploy its own job.** The container argv was passed as `--args "-m,..."`
  and gcloud read the leading dash as another flag (`argument --args: expected one argument`); it is
  now `--args=...`, and extra arguments are appended only when there are any, because
  `printf ',%s'` with nothing to format still emits the separator. (this release)
- **The eval job's task timeout was 3 h, and the third arm finished 18 minutes inside it.** A Cloud
  Run job that hits its timeout writes no `summary.json` at all, losing the whole arm rather than
  its tail. `TASK_TIMEOUT` now defaults to 6 h — a runaway guard, not a schedule. (this release)
- **`gcloud run jobs deploy --args` rejects a repeated value**, so `--max-cost 3 --limit 3` failed
  to deploy with `argument --args: "3" cannot be specified multiple times`, naming neither flag.
  The cap is now formatted to two decimals so its token cannot collide with a small `--limit`.
  (this release)
- **`--limit` did not bound the multi-turn set.** A 5-item smoke run still drove all 29
  conversations: on the Cloud Run job the golden items took under a minute and the conversations
  that followed took twenty. It now limits both, and the multi-turn report states its own n.
  (this release)
- **`eval-all.sh` interpolated an unchecked project id** into the bucket name, the job environment
  and the `gsutil` URLs. An unset `gcloud config project` produced `gs://-consilium-eval` three
  commands later; the placeholder ids the setup docs use are now rejected by name. (this release)
- **`BgeEmbedder` called a method sentence-transformers renamed in v5.**
  `get_sentence_embedding_dimension` warns, the suite runs with warnings as errors, and the failure
  landed on whichever test happened to build a real embedder. The new name is tried first and the
  old one is the fallback. (this release)
- **Two tests asserted that `eval/pricing.yaml` ships empty**, which stopped being true when the
  file was filled in for the Haiku and Gemini runs. One now pins the bargain the file actually
  makes — every entry is a complete input/output pair, and an unpriced model stays unpriced rather
  than free — and the other refuses a cap on a model id nobody will ever price, so it keeps holding
  as the operator fills the file in. (this release)

- **`--human-sample` produced a sample nobody could score.** It wrote empty `judge_label` and
  `judge_rationale` columns and returned before the judge ran, so `--score-judge` would have
  reported a kappa against empty strings. The judge now runs on every sampled row against the same
  evidence `judge_config` uses, `--config` is required, and the draw is stratified over the five
  item-id blocks with its method written beside the CSV. (`6cf88f9`, 2026-08-29)
- **The faithfulness judge disagreed with a human labeller below the usable line.** Round 1 measured
  Cohen's kappa 0.350; `faithfulness_v2` is written against both halves of the disagreement and
  round 2 measured 0.592. `faithfulness_v1.md` stays on disk unedited, because the version that
  produced a published number has to remain readable beside it, and every faithfulness number is
  published with the kappa beside it. (`aceb391`, `f19584a`, 2026-08-29)
- **`SqliteEpisodicStore` leaked its SQLite connection**, which failed the suite on Python 3.13.
  A connection that is only garbage-collected raises `ResourceWarning`, and under
  `filterwarnings = ["error"]` that fails whichever unrelated test is running when the collector
  fires — it landed on `tests/test_eval_cost_cap.py`. The store and `EpisodicMemory` are now context
  managers and the tests close what they open. No behaviour changed. (this release)
- **Four golden-set route labels disagreed with the documents labelled beside them**, found by a
  check that derives the exclusive skill grants from `data/policy.yaml` rather than restating them.
  All four were corrected at the source and logged with the digest on both sides; `g-gh-001` moved
  to a parallel route, which is why an item may span a condition note and a guideline note.
  (`2412b44`, `3afa0b5`, `5d06bd4`, 2026-08-22)

### Known issues

Carried here because the README states them and a changelog that omitted them would read as more
finished than the software is.

- **Red-flag recall in the `full` configuration is 0.500 against the plain-LLM baseline's 0.893**,
  14 false negatives against 3. The mechanism is established from the run's own numbers in
  `docs/SAFETY.md` §7 and the fix is v0.2 roadmap. No number in this repository reflects an
  unimplemented fix.
- **The input-side red-flag matcher does not generalize**: 5 of 5 plainly-phrased questions, 0 of
  22 realistically-phrased ones.
- **Neither the HTTP API nor the MCP server has authentication.** `GET /v1/sessions/{id}` is
  therefore not an authorization boundary and returns no conversation content, and
  `mcp-serve --transport http` binds to localhost.
- **ICD-10 coverage is deliberately narrow**: seven of sixteen conditions have a code-selection
  note, the rest are covered at chapter level only.
