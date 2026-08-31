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
  no key, no download and no network. (`503c089`, 2026-08-09)
- **The corpus: 78 educational reference notes across five categories, and the contract they are
  held to.** Five front-matter keys in order, `doc_id` == filename stem, a byte-identical
  disclaimer stripped before chunking, US spelling with a fixed alternate-spelling allowlist, and
  per-category `doc_id` patterns — all enforced by the loader and linted by `tests/test_corpus.py`
  rather than upheld by hand. (`e61297d`, `2bfd61a`, `e7c68c7`, `fa3e795`, 2026-08-17)
- **The red-flag table and its negation-guarded matcher.** Literal emergency phrases in
  `data/red_flags.yaml`, a guard that suppresses a match behind an explicit negation cue, and both
  policies recorded on every turn so the choice between them is settled by measurement rather than
  by argument. (`3d5eb95`, 2026-08-17)
- **The symptom-to-body-system map** behind `analyze_symptoms`. (`f081d74`, 2026-08-17)
- **Hybrid retrieval: chunking, BM25, RRF fusion, and `consilium ingest`.** Even-division chunking
  to exactly four chunks per note, one tokenizer shared by the lexical and dense paths, reciprocal
  rank fusion at depth 20 with per-`doc_id` dedup, and the full fused top-10 recorded in the trace
  so MRR@10 stays computable. (`e90252d`, 2026-08-18)
- **The skills layer: seven atomic tools, one registry, one envelope.** Tool schemas derived from
  Pydantic argument models with no hand-written JSON anywhere; a skill never raises into its caller
  — every failure comes back as `ok=False` with a `tool_call` event. (`b555337`, 2026-08-20)
- **The agent layer: the shared ReAct loop, three specialists, `data/policy.yaml`, and the turn
  boundary.** Budgets enforced by the loop rather than requested in the prompt, permitted skills
  read from the policy file at construction, and `consilium/runtime.py` as the one composition root
  every entry point goes through. (`cc53c32`, 2026-08-20)
- **The router: planner, blackboard, parallel execution, fixed-precedence synthesis.** One planner
  call with a total fallback, deterministic subtask ids, workers that can read only their own
  assignment, one shared deadline, and a merge whose conflict precedence is code rather than a
  second LLM call. (`358e144`, 2026-08-20)
- **Memory: session-keyed working memory, deterministic context compaction, episodic recall.** A
  five-exchange verbatim window with everything older compacted by extraction rather than by a
  generated summary, session state injected per turn and never held process-wide, and SQLite-backed
  cross-session recall that is off in every measured run. (`af2cca6`, 2026-08-20)
- **The safety layer: policy schema 2, the validator, output repair, and `safety` events.**
  Detection and repair as two classes because they are two counts; a fixed repair order of redact →
  escalation banner → disclaimer; a forbidden sentence removed rather than rewritten.
  (`917b0b2`, 2026-08-20)
- **The evaluation harness: items, metrics, judge, report, runner, `consilium eval`.** Every metric
  computed from trace events and nothing else, `None` rendered as `not measured` and never as
  `0.0`, recall@5 reported three ways, routing accuracy reported unconditionally beside the
  fallback-excluded figure, and red-flag recall reported with its raw false-negative count and the
  item ids. (`ec1408c`, 2026-08-20)
- **The golden set: 150 hand-labelled items and 29 multi-turn conversations, frozen with their
  digests.** `expected_route` and `red_flag` labelled blind; red-flag items stratified into 22
  hard-phrasing and 5 easy-phrasing questions whose recall is never pooled; the 30 coding items
  varied along six ICD-10 conventions rather than along the condition axis; `m-017` rejected whole
  and not replaced. (`cf392e1` … `c2c7cf6`, 2026-08-20 to 2026-08-22)
- **The interfaces: `consilium chat`, the HTTP API, and the SSE stream.** `POST /v1/ask`,
  `POST /v1/chat`, `GET /v1/sessions/{id}` (structure, never content), `GET /healthz`; the
  escalation banner decided from the question and emitted before the first provider call; per-
  session locks so two requests on one session cannot write one trace file.
  (`837aa6c`, 2026-08-22)
- **The single-file demo page**, served by the API itself at `GET /` so the page and the endpoint
  share an origin and no CORS policy is opened for a demo. (`374f479`, 2026-08-22)
- **`--max-cost USD`, the sweep's spend guard.** Enforced between items so a killed turn cannot
  drop an item out of every denominator, with an abort recorded as a result and its own exit code.
  (`c1436bd`, 2026-08-29)
- **The published evaluation run**, `eval/results/published/` — summary and report copied byte for
  byte, 679 traces refolded per session, a manifest of sha256 digests the test suite recomputes.
  `docs/SAFETY.md`, and the README's results with the negative headline finding stated as the
  headline. (`57ac66d`, 2026-08-30)
- **Four worked failure cases** from the published run, `docs/FAILURE_CASES.md`, every quoted
  string re-verified byte for byte against the published tree. (`c2a0777`, 2026-08-30)
- **The MCP server.** `consilium/mcp_server.py` and `consilium mcp-serve`, serving the same seven
  skills over stdio and streamable HTTP. Tool schemas are the registry's own
  `Skill.parameters_schema()` rather than a second derivation; every result goes through the same
  `PolicyValidator` and `OutputRepair` an answer does, because MCP callers are untrusted callers;
  every invocation emits the existing `tool_call` event carrying `transport: "mcp"`. Contract tests
  assert each schema through an SDK client, over both an in-memory transport and a real subprocess,
  offline. (this release)

### Changed

- **Python floor raised to 3.12**, `requires-python = ">=3.12,<3.14"`, CI on 3.12 and 3.13, and the
  two `TypeAlias` aliases converted to PEP 695 `type` statements. Type-checking happens at the
  floor, and the upper bound does not advertise an interpreter CI never runs. (`c8369aa`,
  2026-08-17)
- **`phrasing_stratum` promoted to a field on `GoldenItem`** from a marker string inside
  `draft_notes`, so a published per-stratum recall figure cannot move when prose is edited.
  (`351aeb6`, 2026-08-20)
- **`ToolCallEvent` gained `transport`,** defaulting to `"internal"`. `SCHEMA_VERSION` stays at 2:
  it moves when an event's *required* fields change, and the default is a true statement about
  every `tool_call` written before the field existed, so records from either side pool without
  translation. (this release)
- **`configure_logging` gained a `stream` argument.** The MCP stdio transport owns stdout, where a
  log line is a protocol error rather than a cosmetic one. (this release)
- **Relicensed from MIT to Apache-2.0.** The repository has no forks and no outside contributors,
  so the copyright holder is the only party whose permission the change needs. Commits `57ac66d`
  through `c2a0777` were published under MIT and a copy taken in that window stays under it.
  (`abb3779`, 2026-08-30)

### Fixed

- **`--human-sample` produced a sample nobody could score.** It wrote empty `judge_label` and
  `judge_rationale` columns and returned before the judge ran, so `--score-judge` would have
  reported a kappa against empty strings. The judge now runs on every sampled row against the same
  evidence `judge_config` uses, `--config` is required, and the draw is stratified over the five
  item-id blocks with its method written beside the CSV. (`4de26a1`, 2026-08-29)
- **The faithfulness judge disagreed with a human labeller below the usable line.** Round 1 measured
  Cohen's kappa 0.350; `faithfulness_v2` is written against both halves of the disagreement and
  round 2 measured 0.592. `faithfulness_v1.md` stays on disk unedited, because the version that
  produced a published number has to remain readable beside it, and every faithfulness number is
  published with the kappa beside it. (`f655785`, `c1436bd`, 2026-08-29)
- **`SqliteEpisodicStore` leaked its SQLite connection**, which failed the suite on Python 3.13.
  A connection that is only garbage-collected raises `ResourceWarning`, and under
  `filterwarnings = ["error"]` that fails whichever unrelated test is running when the collector
  fires — it landed on `tests/test_eval_cost_cap.py`. The store and `EpisodicMemory` are now context
  managers and the tests close what they open. No behaviour changed. (this release)
- **Four golden-set route labels disagreed with the documents labelled beside them**, found by a
  check that derives the exclusive skill grants from `data/policy.yaml` rather than restating them.
  All four were corrected at the source and logged with the digest on both sides; `g-gh-001` moved
  to a parallel route, which is why an item may span a condition note and a guideline note.
  (`f723eb9`, `a241309`, `2746a91`, 2026-08-22)

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
