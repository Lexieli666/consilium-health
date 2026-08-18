# CLAUDE.md — frozen decisions for `consilium-health`

**Read this before writing any code in this repository.** It records decisions that are already
made and are not to be re-litigated or silently contradicted. The build brief lives outside the
repository (`../CLAUDE_CODE_PROMPT.md`, not committed); where this file and the brief disagree,
**this file wins**, because every entry here is either a correction the owner made after reviewing
the brief or a detail the brief left open that has since been decided in code.

Keep this file current. When a phase freezes a new decision — a schema, a file format, an interface
— it is written down here in the same commit that introduces it.

---

## 1. Build protocol

Ten phases (§9 of the brief). The original protocol stopped at a checkpoint after every phase. That
was replaced by the following, on the owner's instruction:

- **Run autonomously through the phases.** At each phase boundary run `ruff check`,
  `ruff format --check`, `mypy`, `pytest`. If they pass, commit with a conventional-commit message
  and continue to the next phase without asking.
- **Two checkpoints survive and are mandatory.**
  - **Checkpoint A — Phase 2, at 20 documents.** ✅ **Cleared 2026-08-09**, approved with four
    changes, all applied: US spelling with alternate-spelling anchors; the lipid risk-band label
    corrected (5 to <7.5% is *borderline*, 7.5 to <20% is *intermediate*); "Guidance describes"
    rationed; `doc_id` patterns frozen for all five categories in `docs/CORPUS.md`. Approved as-is
    and not to be revisited: the `source:` field naming a kind of authority, the "Where guidance
    differs" sections, `doc_id` == filename stem, the five fixed front-matter keys, the
    byte-identical disclaimer, and the note-length band.
  - **Checkpoint B — Phase 8, at the golden set.** Generate `eval/data/golden.jsonl` (150 items)
    and `eval/data/multiturn.jsonl` (30 conversations) as **drafts only**. Do not label them, do
    not run the eval against them, do not proceed to Phase 9. The owner labels them by hand. This
    gate is not tradeable for speed: an eval set the system wrote and scored itself against is
    worth nothing, and being able to say the owner annotated it is the point of the harness.

    **Red-flag items must not reuse the strings in `data/red_flags.yaml`.** Owner's constraint,
    2026-08-09, recorded here because it governs how the draft is written and would otherwise be
    lost to compaction. If labelled red-flag questions echo the pattern strings, red-flag recall
    measures only whether the matcher matches itself, which is worth nothing. Those items are
    drafted the way users actually write:
    - hedged ("I think maybe something is wrong with my chest");
    - contracted and informally punctuated;
    - pluralised and inflected away from the canonical pattern;
    - misspelled, including plausible phonetic and keyboard errors;
    - described rather than named — "my chest feels like someone is sitting on it", "the left side
      of my face won't move properly" — avoiding the canonical term entirely;
    - and **at least a few where the red flag is buried inside a longer, mostly routine question**,
      because that is the realistic failure mode and the one a pattern table is worst at.
    Expect this to produce misses. The misses are the finding, not a bug to be papered over by
    editing the questions toward the patterns.
- **Stop and ask** — do not work around — if any of these happen:
  1. A phase's tests cannot be made to pass.
  2. A dependency is needed that is not in §2 of the brief (see §5 below).
  3. A contradiction in the brief would change an already-frozen schema.
  4. A design choice from an earlier phase turns out to be wrong.
  A compounding error found at Phase 10 is worse than an interruption at Phase 5.
- **Stop before any `git push`** and print the full file list for review.

## 2. Hard constraints

1. **No number in the README or in `docs/` may be unmeasured.** Anything not produced by
   `eval/run.py` is written as `not measured`. The published run's `summary.json` is committed
   under `eval/results/published/`; a published number whose evidence is gitignored is a number no
   reviewer can check.
2. **Nothing is copied from `../medix-agent-swarm/`.** It is a reference implementation, read for
   requirements only. `../MediX-R1/` is unrelated and ignored entirely. Every line here is
   independently authored. The sole deliberate overlap is the seven skill names and three agent
   names, which are domain-generic labels chosen on purpose.
3. **Never modify anything outside `consilium-health/`.** All git commands run from inside this
   directory.
4. **Never write an API key, token, or password into any file** — not into `.env`, not into a URL,
   not into a shell line. If a credential is needed, stop and ask.
5. **GitHub account confirmed 2026-08-09: `Lexieli666`.** At Phase 10, `gh repo create` runs
   `--private` and **without** `--push` — creating and publishing in one atomic command would make
   the file-list review unable to gate anything. The full file list is printed and reviewed before
   any push, and the repo goes public only on the owner's explicit say-so.
6. **No clinical claims.** No claim of clinical validity, accuracy on real patients, or regulatory
   readiness. The disclaimer appears at the top of the README, every document in `docs/`, every
   corpus note, and every generated answer.

## 3. Frozen: the trace event schema (`consilium/trace.py`)

`SCHEMA_VERSION = 1`, stamped on every record. **Changing the required fields of any event bumps
it.** Every metric in the brief's §5.2 is computed from these events and from nothing else; if a
metric cannot be derived from them, the honest response is to say so, not to approximate it from a
side channel.

Events are appended to `runs/<session_id>/<turn_index>.jsonl`, one JSON object per line, validated
by a discriminated union of Pydantic models. Common fields on every record: `schema_version`, `ts`,
`trace_id`, `session_id`, `turn_index`, `type`.

**`SCHEMA_VERSION = 2`.** Version 2 added `red_flag_matched_raw` and `red_flag_negation_suppressed`
to `turn` when the red-flag matcher acquired its negation guard. Version 1's `red_flag_matched`
means "matched ignoring negation", which is version 2's `red_flag_matched_raw` — the two cannot be
pooled without that translation, which is exactly why the number exists.

Seven event types:

| `type` | required fields |
|---|---|
| `route` | `mode` (`single`\|`parallel`), `agents[]`, `subtasks[]` (`PlannedSubtask`), `fallback`, `latency_ms` |
| `llm_call` | `caller`, `provider`, `model`, `prompt_tokens`, `completion_tokens`, `latency_ms`, `tools_offered[]`, `stop_reason` |
| `tool_call` | `agent`, `skill`, `args`, `ok`, `error`, `latency_ms`, `source_doc_ids[]` |
| `retrieval` | `skill`, `query`, `category_filter`, `fused_topk[]` (`FusedHit`), `returned_k`, `latency_ms` |
| `safety` | `event` (`violation`\|`repair`), `rule`, `scope` (`tool_call`\|`output`), `agent`, `detail`, `post_stream` |
| `blackboard` | `event` (`assigned`\|`started`\|`completed`\|`failed`\|`timeout`), `subtask_id`, `agent` |
| `turn` | `question`, `answer`, `risk_level`, `wall_ms`, `red_flag_matched`, `escalation_present_pre_repair`, `escalation_present_post_repair`, `repair_applied` |

Frozen details that are easy to break by accident:

- **`FusedHit` shape** — `{doc_id: str, chunk_index: int >= 0, rrf_score: float}`, `extra="forbid"`,
  frozen. `retrieval.fused_topk` carries the **full fused top-10 after per-`doc_id` dedup**, even
  though only the first `returned_k` entries reach the model. Without ranks 6–10, MRR@10 is
  uncomputable.
- **`llm_call.caller`** is pattern-validated against
  `^(planner|synthesizer|forced_answer|agent:[a-z][a-z0-9_]*)$`. Tokens-per-turn is reported split
  by caller; a typo in a caller label would silently create a new bucket.
- **The four `turn` escalation fields.** `red_flag_matched` is whether the *input* matched a
  red-flag pattern. The other three are the detector run against two different answers:
  - `escalation_present_pre_repair` — the model's own answer already told the user to seek care.
  - `escalation_present_post_repair` — the **delivered** answer escalates. **This is red-flag
    recall.**
  - `repair_applied` — the guard, not the model, is what saved it.
  These exist because `OutputRepair` prepends the banner *only* when the answer lacks a seek-care
  instruction, so a correctly-handled red flag emits no repair event; measuring the repair would
  score that as a false negative.
- **The three `red_flag_*` fields record both negation policies from one run.**
  `red_flag_matched_raw` is the match ignoring negation; `red_flag_matched` is the match after the
  guard and is the policy in force; `red_flag_negation_suppressed` is `raw and not matched` — the
  discordant set. Owner's instruction, 2026-08-09: the choice of default policy is settled by two
  measured numbers in `docs/EVALUATION.md`, not by an argument. **If the guard costs any recall on
  the labelled set, the default reverts to raw matching and `docs/DESIGN.md` says so with the
  data.**
- **`safety.post_stream`** marks a repair applied after tokens were already delivered to the
  client. Only the SSE path can set it. `docs/SAFETY.md` must state this plainly.
- **`PlannedSubtask`** (`subtask_id`, `agent`, `objective`, `why`) is declared in `trace.py`, not
  imported from the router: substrate may not depend on a layer above it. The router maps its own
  model onto this one.
- The `Tracer` is **turn-scoped and injected** into planner, router, agents, loop, skills, safety
  layer and synthesizer. It is not owned by the loop — the planner and synthesizer calls are
  exactly the overhead the multi-agent architecture adds, and leaving them untraced would
  undercount the cost of the thing the evaluation exists to measure.

## 4. Frozen: the seven refinements

These were decided by the owner after the Phase-0 critique and **override the brief**.

1. **Red-flag recall is measured on the delivered answer**, via the three `turn` fields above, and
   is reported with the **raw false-negative count**, not only a rate.
2. **SSE red-flag path is input-side.** On `POST /v1/chat`, red-flag detection runs on the *input*
   and the escalation banner is streamed **first**, before model tokens. Output-side repairs happen
   after the stream and are marked `post_stream: true`. `POST /v1/ask` and the CLI repair before
   delivery, so they have no post-stream case.
3. **The golden set is 150 items** (30 general health, 30 symptom/urgency, 30 condition-and-coding,
   30 guideline/evidence, 30 multi-dimensional labelled `mode: parallel`). The ablation table is
   4 presets × 150. `full_budget_6` is a **diagnostic on a 50-item stratified subset**, reported
   separately with its n stated. (The brief says "120 golden questions" once, in §4 — that is a
   stale number; 150 is the decision.)
4. **recall@5 is reported three ways**: union of the top-5s across all retrieval events in a turn
   (system-level), first-retrieval-event-only (config-independent, comparable across presets), plus
   `docs_retrieved_per_turn` so the two are interpretable together.
5. **RRF depth 20**: each retriever contributes its top **20** → fuse with `k=60` → dedup to the
   first chunk per `doc_id` → truncate to exactly **10** (recorded in the trace) and **5** (returned
   to the model) for every measured run. A retrieval depth that varies per call makes the headline
   metric's denominator vary too.
6. **Routing accuracy headlines the unconditional number** — fallbacks counted as their effective
   behaviour (single / `ConsultationAgent`) — with the fallback rate reported beside it, and the
   fallback-excluded number reported as the second column. Reporting only the fallback-excluded
   number would let a planner that fails half the time look perfect.
7. **Faithfulness gets a second oracle-grounded column**, judged against the golden set's
   `relevant_doc_ids` rather than against what the run retrieved, computed for **every** config
   including `baseline_llm` (which retrieves nothing and would otherwise be structurally n/a).

## 5. Frozen: toolchain and versions

- **Python floor is 3.12** (owner's decision, 2026-08-09, replacing the brief's 3.11–3.12 matrix).
  `requires-python = ">=3.12,<3.14"`, `ruff target-version = "py312"`, `mypy python_version =
  "3.12"`, **CI matrix 3.12 and 3.13**. The upper bound is `<3.14` and not `<3.15` on the owner's
  instruction: the bound must not advertise support for an interpreter CI never exercises. The principle: type-check at the floor, because a run that
  passes there is a statement about the oldest Python the package claims to support, and never ship
  code type-checked under a Python that is not actually run. Raising the floor to 3.12 made
  `UP040` fire, so the two `TypeAlias` aliases became PEP 695 `type` statements
  (`type AnyEvent = ...` in `trace.py`, `type ToolSchema = ...` in `llm/base.py`); pydantic
  resolves the resulting `TypeAliasType` in `TypeAdapter` without changes. Verified green on both
  3.12 and 3.13.
- **Dependencies are floor + upper bound**, and `uv.lock` is committed. §5 of the brief requires
  numbers reproducible at a named commit, which floors alone cannot give. `rank-bm25==0.2.2` is
  pinned exactly — unmaintained since 2022.
- **`[embeddings]` extra** (`sentence-transformers`, `chromadb`) is **never installed in CI**.
- **Adding any dependency not listed in §2 of the brief requires asking first.** `numpy` was added
  in Phase 1 as a core dependency because the `NumpyStore` seam the offline rule mandates cannot
  exist without it.
- **The offline rule**: `pytest -m "not network"` passes with no API key, no model download and no
  network, and that is the CI command. It is satisfied by the `Embedder` and `VectorStore` protocol
  seams — **never** by mocking `sentence_transformers` or `chromadb`.
- `addopts = "-m 'not network' --cov=consilium --cov-report=term-missing"`, `asyncio_mode = "auto"`,
  `filterwarnings = ["error"]`. mypy `strict = true`, `warn_unreachable = true`.

## 6. Frozen: layout and naming

- Five layers, strict boundaries: Interface (`cli.py`, `api/`) → Router → Agents (incl. `loop.py`)
  → Skills → Substrate (`retrieval`, `memory`, `safety`, `llm`, `trace`, `config`).
- **The API lives at `consilium/api/`, with no root-level `api/` shim.** The brief says `api/main.py`;
  a top-level package outside `consilium/` would sit outside `--cov=consilium` and its request
  validation would go uncovered. Import path: `consilium.api.main:app`.
- **It is planner–worker orchestration with a blackboard, never a "swarm."** The dedup+summarize
  step is **context compaction**, never "entropy management." Inflated names are a signal in the
  wrong direction, and neither term survives a follow-up question.
- Conflict precedence in the synthesizer is **deterministic, not LLM-arbitrated**: urgency and
  red-flag claims defer to `DiagnosticAgent`, factual/background claims to `ConsultationAgent`,
  evidence-strength claims to `ResearchAgent`.
- Session state is **never** a process-wide singleton: `WorkingMemory` is obtained from a
  `MemoryStore` keyed by `session_id` and injected for the duration of a turn.
- Each agent loads its permitted-skill list from `policy.yaml` at construction. All three agents are
  registered with all seven skills; the policy file is what narrows them.
- `RunConfig` and the five presets live in `consilium/config.py` (frozen in Phase 1 so the router
  and loop accept them natively rather than being retrofitted in Phase 8):
  `baseline_llm`, `single_agent_rag`, `full`, `full_no_memory`, `full_budget_6`.

## 7. Frozen: corpus conventions (Phase 2)

- **`doc_id` == the filename stem, and it is stable by contract.** The golden set labels `doc_id`s;
  renaming a corpus file silently invalidates every label pointing at it. Renaming one after
  Phase 8 requires re-labelling, not a `sed`.
- **Every file in `data/corpus/` is an ingestable note.** No READMEs, no index files, no
  subdirectories — so "every file is a document, `doc_id` is its stem" holds without exceptions.
  Provenance and authoring notes for the corpus live in `docs/CORPUS.md`.
- **Front matter is exactly five keys**, in this order, and the loader forbids extras:
  `doc_id`, `category`, `title`, `source`, `last_reviewed`.
- `category` is one of `lifestyle | coding | guideline | condition | red_flag` (the `Category`
  literal in `consilium/retrieval/types.py`).
- **`source` names the *kind* of authority the statement reflects** — "major cardiology society
  consensus guidance (educational summary)", "general clinical reference" — not a fabricated
  citation to a specific document, edition, or year. Nothing in this corpus is a quotation.
- **`doc_id` patterns are frozen for all five categories** — the full table with examples and slug
  rules is in `docs/CORPUS.md`, and it is the authority. In brief: `condition-<topic>`,
  `guideline-<topic>-<aspect>`, `lifestyle-<topic>-<domain>` (domain ∈ diet | activity | sleep |
  adherence), `coding-icd10-chapter-<nn>-<system>` and `coding-<topic>-<code-root>`,
  `red-flag-<presentation>`. The `red_flag` **category** keeps its underscore (it is a Python
  `Literal`); the `red-flag-` **doc_id prefix** uses a hyphen (it is a filename stem). Per-condition
  coding notes carry the code root without the decimal (`e11`, not `e11.9`).
- **US spelling throughout**, with a fixed allowlist of British alternate-spelling anchors appearing
  once in parentheses at first use (`oesophageal`, `haemoglobin`, `anaemia`, `apnoea`,
  `generalised`, `GORD`, extended as needed). This is a retrieval decision: BM25 is lexical, so
  "esophageal" cannot match a document that only says "oesophageal", and the golden set would
  inherit any mismatch as a measured retrieval failure with a purely orthographic cause. A corpus
  lint test asserts no British form outside the allowlist appears.
- **Note length 2,700–3,500 characters of body**, giving 3–4 chunks per note. A one-chunk corpus
  would never exercise RRF's per-`doc_id` dedup.
- **"Guidance describes" is rationed.** It belongs where a claim is contested or varies by body, not
  as a default sentence opener: it reads as evasive, and a stock phrase repeated across 80 documents
  adds a shared component to every embedding. Current density is 21 occurrences across 20 notes.
- **Every note carries the same one-line disclaimer blockquote** immediately after the front matter.
  The loader **requires** it (a missing disclaimer is an ingest error, which makes the requirement a
  test rather than a convention) and **excludes it from chunk text**: a constant string in every
  document is zero-IDF noise for BM25 and a real perturbation for dense vectors, and it carries no
  retrievable information.
- Content is **descriptive, never directive** — "guidance describes X as a first-line option", not
  "you should take X" — and **contains no doses**, because `policy.yaml` forbids the system from
  emitting dosing instructions and the corpus must not contain what the policy forbids.
- Guideline notes carry a **"Where guidance differs"** section wherever authorities genuinely
  diverge (US vs European hypertension thresholds, diabetes screening age, asthma reliever choice,
  the subclinical-hypothyroidism treatment band). This is deliberate: `deep_research` must produce
  an explicit "sources disagree" section, and it can only do that if the corpus actually disagrees
  with itself somewhere.
- No epidemiological or prevalence statistics. They would be unmeasured numbers of a different
  kind, and the corpus does not need them.

## 8. Phase status

| phase | state | commit |
|---|---|---|
| 1. Scaffold, trace schema, offline seams, CI | done | `92a47e3` |
| 2. Corpus, red flags, chunking, BM25, RRF, ingest | in progress — Checkpoint A cleared | |
| 3. Skills + registry | not started | |
| 4. ReAct loop + agents + `trace` CLI | not started | |
| 5. Planner, router, blackboard, synthesizer | not started | |
| 6. Memory | not started | |
| 7. Safety | not started | |
| 8. Eval harness + golden-set drafts | not started — **Checkpoint B** | |
| 9. API + SSE + CLI polish | not started | |
| 10. Docs, published eval run, README numbers | not started | |
