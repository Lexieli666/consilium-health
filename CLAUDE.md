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
  - **Checkpoint B — Phase 8, at the golden set.** ✅ **Cleared 2026-08-22, both files.**
    `eval/data/golden.jsonl` and `eval/data/multiturn.jsonl` are both labelled and frozen, and both
    load without `allow_draft`. **Phase 9 is unlocked and has not been started.** The freeze record
    — both sha256 digests, the counts, the blind procedure, the per-stratum pattern-hit table, the
    unverified-reference limitation — is `docs/EVALUATION.md` §1.5, the multi-turn record is §1.7,
    and the digests are asserted in `tests/test_eval_drafts.py` in both directions.

    The original instruction, kept because it is what the drafts were written under: generate
    `eval/data/golden.jsonl` (150 items) and `eval/data/multiturn.jsonl` (30 conversations) as
    **drafts only**. Do not label them, do not run the eval against them, do not proceed to
    Phase 9. The owner labels them by hand. This gate is not tradeable for speed: an eval set the
    system wrote and scored itself against is worth nothing, and being able to say the owner
    annotated it is the point of the harness.

    **What the golden set actually carries, frozen 2026-08-22.** `expected_route` and `red_flag`
    were hand-labelled on all 150 items **blind** — the labeller saw the question and nothing
    else: not the `category`, not the `phrasing_stratum`, not the `id` (block-prefixed, so it
    leaks the category), and not the file order (block-ordered, so it leaks it too). Result: 121
    single, 29 parallel, 28 red-flag. **The current counts are 120 single / 30 parallel / 28
    red-flag**, the one difference being `g-gh-001`, moved post-freeze with the documents open (see
    the post-freeze note below); 121/29 stays written here because it is what the blind pass
    produced. Five items disagree with the drafting plan (three `multi_dimensional` labelled
    `single`, two `symptom_urgency` labelled `parallel`) and **the disagreements are kept** —
    correcting a blind label to match the block it came from discards the only signal the procedure
    exists to produce. `g-gh-001` is not among those five and is not evidence about the blind
    procedure: it was decided sighted, and it is a correction rather than a finding.

    `relevant_doc_ids` and `reference_answer` were **not verified**, by the owner's decision of
    2026-08-22. They stay machine-written and unverified on 144 and 148 of the 150 items
    respectively (the two counts differ because the fields are verified independently; see the
    post-freeze note below). **`recall@5` and `faithfulness` are
    therefore measured against a machine-constructed reference while routing accuracy and
    red-flag recall are not**, and that sentence is not to be softened anywhere it appears.

    **`unverified_fields` records provenance; `proposed_fields` remains the gate.** Owner's
    instruction, 2026-08-22. `proposed_fields` means "nobody has dispositioned this" and
    `missing_labels()` reports it as missing so `load_golden` refuses the file — which is right
    while labelling is pending and wrong once the owner has decided not to verify: clearing the
    marker would make the file assert a verification that did not happen, and leaving it set would
    block loading forever. So the 148 records moved to `unverified_fields`, same values, no gate.
    `proposed_fields` **stays in the schema, still gating**, for any future re-label. (It was
    written here as "for the multi-turn set" and that was wrong about the mechanism —
    `MultiturnConversation` never carried the field; see Checkpoint B's multi-turn record.) The two are **disjoint per record** (a field cannot be both dispositioned and not),
    **neither may ever name `expected_route` or `red_flag`** (`JUDGEMENT_FIELDS`, refused by the
    schema), and the counts are carried into `summary.json` as `unverified_labels` and printed by
    `report.md` **in the results table itself** — in the header of every affected column, in a line
    under the table, and in the per-config Retrieval and Judge paragraphs. Not a footnote: the
    misreading being prevented happens inside the table, in the row where the four numbers sit
    beside each other. Nothing marks the routing or red-flag columns, because nothing is wrong with
    them. The caveat is rendered from the data, so clearing `unverified_fields` removes it with no
    code change.

    **An empty `relevant_doc_ids` is a label, not a blank.** It means "no note in this corpus
    grounds this question", which is what `g-md-027` is in the set to record; `missing_labels()`
    reports it as missing only when the `reference_answer` beside it is also blank. Requiring a
    non-empty list would have made inventing a source the only way to load the file.

    **Labelling guidance must agree with the skill grants in `data/policy.yaml`.** The lesson of
    this checkpoint, recorded in `docs/DESIGN.md`. The labelling guide paraphrases the grants in
    prose; a labeller cannot see the file and a blind labeller cannot see the block either, so a
    route label can name agents that between them hold **none** of the dedicated skills the
    labelled documents imply. The check is derived and permanent in `eval/validate.py`: the
    exclusive grants are **read from `policy.yaml`** and the implied skills from the labelled
    notes' corpus categories, never restated.

    **It is a warning, not an error, and the first wording of it was wrong.** Owner's correction,
    2026-08-22. `search_knowledge` takes an optional category argument, defaults to searching
    everything, and is granted to all three agents (§10), so **no labelled route is structurally
    impossible** and nothing here can be an error. What the check detects is a mismatch between the
    **dedicated, category-filtered** skill the route carries and the corpus category of the
    labelled documents: those documents stay reachable, but only through the unfiltered search,
    competing with all 78 notes for a top-5 slot instead of with one category. It fires only where
    the route holds **none** of the implied skills. `eval/run.py` logs each one at warning level
    when a sweep starts; `tests/test_eval_drafts.py` pins the set to a **reviewed baseline**,
    asserted exactly — *requiring* empty would be a standing demand to relabel, unchecked would
    make the warning invisible. The baseline is empty as of 2026-08-22 because all four flagged
    items were resolved, which is a fact about the current labels and not a rule; the assertion
    form stays an exact set so a future mismatch can be reviewed and kept.

    **Post-freeze resolution, 2026-08-22.** All four flagged items were real labelling errors with
    **the route as the wrong half**, and all four were corrected: `g-gh-017` and `g-gh-026` to
    `consultation`, `g-gh-029` to `research` (modes unchanged), and `g-gh-001` from single
    `consultation` to **parallel `consultation` + `research`**. `g-gh-001` was decided last and
    separately, because both of its labels were verified by then — two hand-written labels
    disagreeing is a decision, not a fix. The owner read both notes: the quantified confirmation
    rule the reference answer's last sentence rests on is in
    `guideline-hypertension-diagnosis-and-bp-targets` alone, and `condition-hypertension` carries
    only the qualitative version and defers the rest, so dropping the guideline note would have
    left the reference answer unable to answer the second half of its own question. **An item may
    span a condition note and a guideline note, and when it does the route is parallel.** That
    moved the mode counts to **120 single / 30 parallel**, with three parallel items now outside
    the `multi_dimensional` block; `red_flag` stays 28. `relevant_doc_ids` is verified on those
    four, dropping its unverified count to 144 while `reference_answer` stays 148 — `g-gh-001`'s
    reference answer was left as it stands and stays unverified.

    **A LABEL note that disagrees with its own label is a defect, and the check reads half a
    field.** Owner's instruction, 2026-08-22, after scanning all 150 notes against the final routes.
    `draft_notes` splits on `|| LABEL:` into authoring intent (left) and the labeller's reasoning
    (right). `label_note_agent_mentions` reads **only the right half**: the left half is the
    drafting plan, which the blind pass was allowed to disagree with and did five times, so scanning
    the whole field would flag six `multi_dimensional` items for exactly the property the procedure
    exists to produce. Two notes were stale — `g-gh-017` and `g-gh-026` still said `research` after
    the owner overrode that reasoning on reading the documents — and were rewritten to the reason
    that actually decided them; `g-ge-001` is a **false positive** kept as the whole baseline,
    because its `diagnostic` is an adjective on `threshold`. It is a **warning against an exact
    `(item, agent)` baseline** and not an assertion: no lexical rule separates the adjective from
    the agent name, and a matcher fitted to this file's two examples would fail silently on the next
    stale note. Text-only edits; no label and no published count moved. `eval/run.py` does **not**
    log it — it bears on nothing a sweep measures, unlike the route-document warning.

    **A frozen data file can be edited; it cannot be edited silently.** Every post-freeze change to
    either data file is logged in `docs/EVALUATION.md` §1.6 with the item id, what changed, why,
    and the digest on both sides. The digests in §1.5 are always the current ones and the lint
    asserts them in both directions.

    **The multi-turn set, labelled 2026-08-22 — 29 conversations, not 30.** Full record in
    `docs/EVALUATION.md` §1.7. The frozen decisions:

    **`m-017` was rejected whole by the labeller and is dropped without replacement.** Its second
    turn asks "what would they check?" and **"they" has no antecedent in any earlier turn**, so the
    conversation does not test reference resolution — "they" resolves from world knowledge, not
    from the transcript, and the item would score the model's willingness to guess. Dropped
    **whole**, because two thirds of a rejected conversation is a different item that nobody
    drafted and nobody labelled; **no replacement was written**, because drafting a thirty-first
    after the labelling to restore a count is the drafting-to-fit this checkpoint exists to
    prevent; and **the ids are not renumbered**, so `m-016` is followed by `m-018` and the gap is
    the only durable record that something was rejected rather than never drafted. **The set is 29
    and 29 is accepted.** The labeller's `UNRESOLVED:` prefix in the referent column was a
    rejection annotation rather than a referent, and it left with the conversation.

    **`depends_on_turn` accepts `int | list[int] | None` and normalizes to a tuple.** The
    labeller's extension, kept because it is more accurate than forcing one referent: **11 of the
    83 annotated turns** name two, three or four earlier turns, and recording only the nearest
    would make the label say something the labeller did not mean. `expected_referent` stays **one
    string** even then — the prose does not decompose one-to-one onto the indices, and splitting it
    on punctuation would be a machine inventing the parts of a hand-written label. The file is
    written back with every value as a list, so a reader never handles two shapes. Two invariants
    travel with it, both refused at the loader rather than linted after: a turn index and its
    referent must **both** be present or **both** absent (a referent alone is how m-017 was
    rejected), and every referent must name a **strictly earlier** turn.

    **The resolution metric is all-or-nothing over the referents a turn names, and partial
    resolution scores `misresolved`.** One labelled turn is one item; the judge is given every
    referent index plus a numbered transcript and resolves the turn only if the answer accounts for
    all of them. `misresolved` and not `unresolved` because the answer **committed** to a reading
    and the reading is wrong — it answered a narrower question, and the reader cannot tell from the
    answer alone that something was dropped, which is the definition of that bucket. Grading each
    referent separately was rejected: it would weight the harder turns more heavily by construction
    and let a system that resolved three of four score 0.75 on a question it answered wrongly. The
    difficulty mix is published instead (72 turns name one referent, 7 two, 2 three, 2 four).
    `eval/judges/multiturn_v1.md` states the rule and **stays `v1`**: prompt versioning exists so
    numbers from two prompts are never pooled, and this prompt has produced no numbers at all.

    **Five conversations reach past the working-memory window, and the set is pinned exactly.**
    `m-021`, `m-023`, `m-026`, `m-027`, `m-030`. Reach is measured to the **earliest** turn a
    dependency names, because that is the one at risk; past-window is strictly greater than
    `WINDOW_EXCHANGES`, so `m-022` and `m-028` at exactly five are inside it. Pinned as an exact
    tuple in `tests/test_eval_drafts.py` rather than as a floor of five, for the same reason as the
    route-document baseline: a floor passes if one of the five loses its long-range dependency
    while an unrelated conversation gains one, which is a different set of items measuring a
    different thing with nothing failing. `past_window_conversations` in `eval/validate.py` reads
    `WINDOW_EXCHANGES` from `consilium.memory` rather than writing `5`.

    **The limitation, and it is not to be softened: the summarization path is exercised by five
    conversations.** Inside the window `full` and `full_no_memory` replay the same verbatim
    transcript, so only those five can distinguish them on the compaction path at all — **any
    effect size the memory ablation reports on that path rests on n=5**, which cannot separate a
    real effect from noise. `eval/run.py` computes the sentence from the file and writes it into
    every run's `summary.json` `notes`, so it travels with the numbers instead of living only in
    `docs/EVALUATION.md` §1.7 and §7.

    **`proposed_fields` now gates nothing, and the earlier claim about it was wrong.**
    `proposed_fields` is a `GoldenItem` field and always was; `MultiturnConversation` has never
    carried one, and the multi-turn set was gated by `missing_labels()` reporting a conversation
    with no annotated referent. This file and `eval/items.py` previously said `proposed_fields`
    "stays gating for the multi-turn set", which was wrong about the mechanism if right about the
    effect. It stays in the schema, still gating, **for any future re-label**, where a
    machine-written candidate would again need keeping from becoming a label by silence.

    **The four label fields split two ways, and the split is the substance of the checkpoint.**
    Owner's instruction, 2026-08-20. `expected_route` and `red_flag` ship **empty and stay empty**:
    they are pure judgement, they are the labels routing accuracy and red-flag recall are computed
    against, and a machine-written candidate there would anchor exactly the numbers this gate
    exists to protect — so nothing proposes them, not even as a suggestion in a second field.
    `relevant_doc_ids` and `reference_answer` ship holding **machine-written candidates**, because
    leaving them empty turns labelling into authoring and costs the owner nothing in independence:
    the corpus notes have to be opened either way. A reference answer is written **only** from the
    documents proposed beside it, keeps the corpus's own hedges rather than sharpening an
    approximate band, and where a proposal is uncertain it names fewer documents and says so in
    `draft_notes`. Two items ship with no candidate at all because the corpus cannot ground them.
    **A candidate is not a label**: every field holding one is named in `proposed_fields`,
    `missing_labels()` reports it as missing, and the loader refuses the file until the owner has
    cleared the marker — otherwise flipping `labeled: true` would promote 148 machine-written
    reference answers into ground truth by silence. `docs/EVALUATION.md` §1.1.1 is the table.

    **Red-flag items are stratified by phrasing difficulty, and recall is reported per stratum.**
    Owner's constraint of 2026-08-09 as amended 2026-08-20, recorded here because it governs how
    the draft is written and would otherwise be lost to compaction.

    The **hard-phrasing stratum** (22 items) must not reuse the strings in `data/red_flags.yaml`.
    If those questions echo the pattern strings, that stratum measures only whether the matcher
    matches itself, which is worth nothing. Those items are drafted the way users actually write:
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

    The **easy-phrasing stratum** (5 items) is the deliberate counterpart: plain, direct questions
    naming the symptom the ordinary way, across five different rules, exempt from the no-reuse rule
    by construction. It exists because a set with only the hard stratum yields a lower bound with
    nothing to compare it against, and real users do type the canonical terms. This is a designed
    comparison, not the leakage the no-reuse rule forbids — the difference being that the two
    strata are **never pooled**: a pooled figure would move with the ratio between them, which is a
    drafting choice rather than a property of the system. The set stays at 150 and the block at 30:
    five non-red-flag symptom items were dropped, each one either unsupported by any corpus note or
    drawing on documents another item already covers, and the dropped ids are **not reused**.

    **The stratum is a first-class field, `phrasing_stratum`, and never a marker inside
    `draft_notes`.** Owner's instruction, 2026-08-20, correcting the mechanism while leaving the
    constraint above untouched. Values are `hard | easy | null`, `null` meaning the item is not a
    red-flag candidate. `draft_notes` is the field the owner edits while labelling — the labelling
    guide asks for the reasoning to be recorded there — so a stratum read out of it would be a
    dimension a published metric splits on that depends on prose about to be rewritten: trimming a
    note would move an item between strata, change both per-stratum recall figures, and fail no
    test, because the item would still be valid. The field is populated at drafting time, it is
    **not** in `LABEL_FIELDS` and therefore never in `proposed_fields` or `missing_labels()`
    (authoring intent, not something the owner labels), and the marker strings were removed from
    the notes in the same commit so there is exactly one source.
    `tests/test_eval_drafts.py` asserts: every red-flag candidate carries a non-null stratum, no
    other item carries one, the retired markers never reappear in `draft_notes`, and the counts
    are **exactly** 22 hard and 5 easy — exact rather than a floor, because those are the
    denominators of the two published recall figures, so changing either is a deliberate act that
    updates the lint in the same commit.

    **The 30 condition-and-coding items vary along the CONVENTION axis, not the CONDITION axis.**
    Owner's constraint, 2026-08-17, recorded here for the same reason as the red-flag one: it
    governs how the draft is written and would otherwise be lost to compaction. The `coding`
    category carries seven conditions' worth of code-selection detail and chapter-level coverage
    for the other nine (see §7), so thirty questions differing only in which condition they name
    would be thirty near-duplicates drawn from seven documents. **Near-duplicate golden items
    inflate apparent performance the same way duplicate documents inflate retrieval scores.** The
    items therefore vary along the convention under test:
    - the "with" presumption — where a code assumes a causal link between two conditions, and
      where it does not;
    - combination codes, which fold two diagnoses into one code;
    - required second codes, and the order they are sequenced in;
    - three-character codes that are already complete and take no further character;
    - undecimalized roots versus the full code;
    - chapter boundaries — what a chapter deliberately excludes and which chapter holds it instead.
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
  client. Only the SSE path can set it. `docs/SAFETY.md` must state this plainly. **Phase 9 built
  the SSE path and it does not set the flag** -- it repairs before the first byte of the body -- so
  the field stays in the schema, `OutputRepair` still accepts it, and no caller in this repository
  passes `True`. See §4 refinement 2 and §15.
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

   **Phase 9 built the first half and departed from the second, and this is the one such departure
   in the phase.** The banner is decided on the input and is the first event of the stream, asserted
   twice: through HTTP against the wire order, and against `stream_turn` directly, which proves the
   banner is yielded *before the first provider call*. The body, however, is not a provider-token
   stream. The final text-producing call of a turn cannot be identified in advance on the
   single-worker path -- any call that offers tools may come back asking for one -- so streaming it
   would mean streaming tool-call deltas, which `consilium/llm/base.py` scoped out in Phase 1. The
   body is therefore assembled and then delivered incrementally, which means the server is holding
   the repaired text and the raw text at the same moment. It sends the repaired one: displaying a
   dose and retracting it buys nothing when the tokens were never going to arrive earlier.
   **`post_stream` is therefore never set**, `tests/test_api_stream.py` asserts that, and the
   reversal is one function -- `stream_turn` would stream `routed.answer` and repair afterwards with
   `post_stream=True`. Full argument in `docs/DESIGN.md`, "Phase 9 -- the interfaces".
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
  coding notes carry the code root without the decimal (`e11`, not `e11.9`); where a condition spans
  a block of roots rather than one, the `doc_id` carries the **lowest** root in the block
  (`coding-osteoarthritis-m15` for M15–M19) and the body states the block. A third `coding` form,
  `coding-icd10-<aspect>`, covers notes about the classification itself rather than a chapter or a
  condition; the chapter form requires two digits, so `chapter-9-...` is a lint failure rather than
  falling through to the classification form.
- **A chapter note is a map; a condition note owns code selection.** Frozen 2026-08-17 after the
  first chapter-note draft duplicated `coding-hypertension-i10` almost claim for claim. Two
  documents answering the same question make `relevant_doc_ids` ambiguous in the golden set, inflate
  the recall@5 denominator when both are labelled, and consume two of five top-k slots after RRF
  per-`doc_id` dedup — so the overlap is a measurement defect, not a style problem.
  - A **chapter note** states the letter and range, walks the block structure in order and says what
    each block is for, names what is deliberately *not* in the chapter and which chapter holds it
    instead, and lists the cross-chapter codes a question in that area commonly needs.
  - A **condition note** owns code selection for its condition: which code, which combination rules,
    which second codes are required, which conventions govern the choice.
  - Where a chapter note must gesture at a rule a condition note owns, **one clause naming the rule
    is the budget** — not a section, not a worked explanation.
  The general form of the rule: if the same rule is explained twice, one of the two notes is wrong.
- **Every condition note in the corpus is named, with its code root, in at least one `coding` note**
  — per-condition where the code selection is nuanced, chapter-level where it is not. This is what
  makes the coding and condition notes mutually retrievable, and it is the property that the empty
  `coding` category destroyed.
- **Coding coverage is deliberately narrow, and is accepted as-is.** Owner's decision, 2026-08-17.
  Seven of the sixteen conditions have a per-condition code-selection note — hypertension (`i10`),
  type 2 diabetes (`e11`), asthma (`j45`), COPD (`j44`), GERD (`k21`), heart failure (`i50`),
  insomnia (`g47`). The other nine are covered at chapter level only, and three of the seven
  (COPD, GERD, type 2 diabetes) are named in their own note rather than in their chapter map. The
  corpus is not reopened to level this up: the brief's §4 fixes no per-condition count, and writing
  nine more selection notes would be a second deviation from its coverage list to fix something
  that is a labelling constraint rather than a defect. The consequence is carried in Phase 8
  instead — see §1, Checkpoint B — and stated as a limitation in `docs/CORPUS.md`,
  `docs/EVALUATION.md` and the README.
- **US spelling throughout**, with a fixed allowlist of British alternate-spelling anchors appearing
  once in parentheses at first use (`oesophageal`, `haemoglobin`, `anaemia`, `apnoea`,
  `generalised`, `GORD`, extended as needed). This is a retrieval decision: BM25 is lexical, so
  "esophageal" cannot match a document that only says "oesophageal", and the golden set would
  inherit any mismatch as a measured retrieval failure with a purely orthographic cause.
  `tests/test_corpus.py` asserts no British form outside the allowlist appears, along with every
  other convention in this section: front-matter keys and order, `doc_id` == stem, the category
  `Literal`, the byte-identical disclaimer, the length band, and the per-category `doc_id` patterns.
  The spelling rule is **inverted** for the `-ise` family — the set of words spelled `-ise` in US
  English is closed and short, whereas a list of British forms would miss the one nobody thought of.
  The `oe`-digraph rules match only at a word start, because `gastro`+`esophageal` and
  `angio`+`edema` reproduce the digraph by accident and are the US spellings.
- **Note length 2,700–3,500 characters of body.** Measured against the chunker: **exactly 4 chunks
  per note, 312 chunks total**, mean 846 characters, none above the 1,000 ceiling. (The 3–4 written
  here before Phase 2's chunker existed was a prediction; 4 is the measurement, asserted in
  `tests/test_chunking.py`.) A one-chunk corpus would never exercise RRF's per-`doc_id` dedup.
- **"Guidance describes" is rationed.** It belongs where a claim is contested or varies by body, not
  as a default sentence opener: it reads as evasive, and a stock phrase repeated across 80 documents
  adds a shared component to every embedding. Current density is 29 occurrences across 22 notes of
  78.
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

## 8. Frozen: the retrieval pipeline (Phase 2)

Interfaces and constants that later phases consume and that the eval harness computes numbers from.
Rationale for each, with the rejected alternative, is in `docs/DESIGN.md`.

- **`consilium/retrieval/` module layout.** `corpus` (load) → `chunking` (split) → `embedder` +
  `store` (dense) and `bm25` (lexical) → `fusion` (RRF) → `hybrid` (sequence it, emit the trace
  event) → `index` (wire it up for the CLI). Nothing in the package imports from a layer above it.
- **`Document`** (`doc_id`, `category`, `title`, `source`, `last_reviewed`, `body`) is what the
  loader returns. `extra="forbid"`, frozen. `DISCLAIMER` and `FRONT_MATTER_KEYS` live in
  `consilium/retrieval/corpus.py` and are **imported** by `tests/test_corpus.py`, not restated
  there: two copies of a constant that rejects notes will drift, and the lint would then pass while
  ingest failed.
- **The loader enforces four conventions, it does not merely assume them**: the five front-matter
  keys in order with no extras, `doc_id` == filename stem, the `Category` literal, and the
  byte-identical disclaimer. A violation is a `CorpusError` at ingest. The disclaimer is required
  and then **stripped from the body**, so it never reaches a chunk.
- **Chunking is even division, not greedy filling.** `MAX_CHARS = 1000`, `MIN_CHARS = 800`,
  `OVERLAP_CHARS = 100`. The chunk count is `ceil(len / (max - overlap - 2))`, and each break is
  placed at the point dividing the remaining text evenly among the remaining chunks, snapped to the
  best boundary — paragraph, then sentence, then word — **within the size band**, never within the
  wider feasible window. No break may fall inside a heading line or immediately after one.
  `chunk_index` is assigned in `chunk_document` and is half of the trace's `FusedHit`.
- **`Bm25Index`** wraps `rank-bm25` and uses `consilium.retrieval.tokenize.tokenize` — the *same*
  function the hash embedder uses, never a second tokenizer. `k1 = 1.5`, `b = 0.75`, the library
  defaults, untuned. A chunk is a hit iff it **shares a token** with the query, not iff its score is
  positive: Okapi gives a negative IDF to a term carried by more than half the corpus. Category
  filtering happens **after scoring**, so IDF stays a corpus-level statistic.
- **Fusion constants.** `RRF_K = 60`, untuned. `dedupe_by_doc_id` keeps the first chunk per `doc_id`
  **in ranking order** and runs after fusion, before any truncation or scoring.
- **`HybridRetriever` depths are constructor state, not call arguments**: `CANDIDATE_DEPTH = 20` per
  retriever, `TRACE_DEPTH = 10` recorded in the `retrieval` event, `RETURNED_K = 5` returned to the
  model. `search()` takes no `k`. The category filter is passed to **both** retrievers before
  fusion, never applied to the fused result.
  - **Open discrepancy, flagged rather than silently resolved.** §4 refinement 5 of this file freezes
    the candidate depth at **20**; the brief's §3.6 says "dense top-10 + lexical top-10". This file
    wins, so `CANDIDATE_DEPTH = 20` is the default — but it is a named constructor argument, so
    reverting to 10 is one call site and no measured number depends on it yet.
- **`VectorStore` conformance for `ChromaStore` is checked statically.** `make_store` and
  `make_embedder` in `index.py` are annotated as returning the protocol types, so mypy verifies the
  structural match in CI **without `chromadb` or `sentence-transformers` installed**. The runtime
  contract is `tests/test_vector_store_contract.py`, one set of assertions parameterized over both
  implementations, with the Chroma parameterization `importorskip`-ed where the extra is absent.
  Neither library is ever mocked.
- **`ChromaStore` passes `embedding_function=None`** so Chroma cannot attach its default ONNX model,
  and converts Chroma's cosine *distance* to a similarity so a `ScoredChunk.score` means the same
  thing in both store implementations.
- **`BgeEmbedder` applies `prompt_name="query"`**, never a hand-written instruction prefix: the
  prefix string has changed between bge revisions and a stale copy degrades retrieval without
  raising. Its dimension is checked against `EMBEDDING_DIM = 384` at construction.
- **The CLI is `consilium/cli.py`, a Typer app with an explicit `@app.callback()`.** Typer collapses
  a single-command app into a bare top-level command, which would silently rename every invocation
  when the second command lands. `consilium ingest` is Phase 2's only command;
  `[project.scripts]` now exists because a module it can point at now exists.
- **Ingestion resets the store by default.** A persistent store outlives the process, and leaving
  the previous chunking of an edited note in the index produces stale hits that read as a
  retrieval-quality problem rather than an ingestion one.

## 9. Frozen: the skills layer (Phase 3)

Seven skills, one registry, one envelope. Rationale for each decision, with the rejected
alternative, is in `docs/DESIGN.md` under "Phase 3 — skills and the registry".

- **`consilium/skills/` module layout.** `base` (envelope, context, `@skill`) → `registry`
  (discovery, tool schemas, the one invocation site) → one module per skill (`knowledge`, `risk`,
  `symptoms`, `lifestyle`, `coding`, `guidelines`, `research`) → `symptom_map` (the data table
  `analyze_symptoms` consumes). Nothing in the package imports from a layer above it.
- **The seven names are fixed** and `SKILL_NAMES` in `consilium/skills/__init__.py` is the list:
  `search_knowledge`, `assess_risk`, `analyze_symptoms`, `recommend_lifestyle`,
  `lookup_disease_code`, `find_guideline`, `deep_research`. `policy.yaml` narrows per agent; it
  never adds.
- **`SkillResult`** (`skill`, `ok`, `data`, `error`, `latency_ms`, `sources`), `extra="forbid"`,
  frozen. `sources` is a tuple of `doc_id`s and is copied onto `tool_call.source_doc_ids` by the
  registry. `to_observation()` is what the ReAct loop feeds back to the model.
- **A skill never raises into the loop.** Unknown name, invalid arguments, retrieval disabled, and
  an exception inside the skill all return `ok=False` and all emit a `tool_call` event with
  `ok=False`. The handling is in `SkillRegistry._invoke`, once.
- **Tool schemas are derived**, via `model_json_schema()`, with only the class-name `title`
  stripped. There is no hand-written JSON in the package and `tests/test_skill_registry.py`
  asserts the emitted parameters equal what pydantic generates.
- **Skills are synchronous; `SkillRegistry.execute()` dispatches through `asyncio.to_thread`.**
  The parallel router's workers must overlap, and a skill holding the event loop would serialize
  them — which would make the parallel-versus-single latency comparison measure the harness.
- **`SkillContext`** is a frozen dataclass carrying `retriever`, `red_flags`, `symptoms`,
  `documents`, `tracer`, `agent`. It is built per turn and injected; skills hold no module-level
  state. `retriever=None` is a legitimate state (`RunConfig.retrieval=False`), not an error.
- **`DIFFERS_HEADING = "## Where guidance differs"` lives in `consilium/retrieval/corpus.py`**,
  with the other frozen corpus conventions, and is imported by `deep_research` and `find_guideline`
  rather than restated. 11 of the 19 guideline notes carry the section.
- **`deep_research` is corpus-only and issues no LLM call of its own.** `llm_call.caller` has no
  slot for a skill; adding one would change a frozen schema. Sub-queries arrive as tool arguments
  from the agent, capped at 5, with the question itself always first, and are retrieved
  **sequentially** so that "the turn's first `retrieval` event" stays deterministic.
- **`assess_risk` shares `RedFlagTable` with `OutputRepair`** and takes its tier from the table,
  never from retrieved prose. `NO_MATCH_ACTION` is a fixed string stating that a non-match is not a
  clearance.
- **`analyze_symptoms` returns documents, never a ranked differential**, and reports
  `unrecognized` rather than filing unparsed input under `constitutional`.

## 10. Frozen: the agent layer, the policy file, the turn boundary (Phase 4)

Rationale for each decision, with the rejected alternative, is in `docs/DESIGN.md` under
"Phase 4 — the ReAct loop, the agents, and the turn boundary".

- **`data/policy.yaml` exists from Phase 4, carrying only the per-agent permitted-skill lists.**
  Owner's pre-authorized resolution of an ordering contradiction in the brief: §3.2 requires
  `BaseAgent` to load its permitted skills from `policy.yaml` at construction, while §3.7 does not
  introduce the file until Phase 7. Phase 7 **expands the same file** with the full output policy
  (required elements, forbidden behaviours, and a path reference to `data/red_flags.yaml` — never a
  second copy of it) and bumps its `schema_version` from 1 to 2. The file lives in `data/` beside
  the other two runtime tables; the loader lives in the safety layer at
  `consilium/safety/policy.py`, exactly the split `red_flags.yaml`/`red_flags.py` already uses.
- **`consilium/safety/policy.py` does not validate skill names against the registry.** Substrate may
  not import the Skills layer above it. The check happens in `BaseAgent.__init__`, which calls
  `registry.subset(permitted)` and raises on an unknown name — so an agent whose policy names a
  skill that does not exist cannot be constructed.
- **The three agents declare exactly two class attributes each**, `name` and `system_prompt`. There
  is no per-agent tool wiring anywhere; `tests/test_agents.py` asserts the class bodies stay at two.
  Permitted lists: `consultation` = search_knowledge, recommend_lifestyle, lookup_disease_code;
  `diagnostic` = assess_risk, analyze_symptoms, search_knowledge; `research` = find_guideline,
  deep_research, search_knowledge. Each specialist owns its own three; all three share the
  unfiltered `search_knowledge`. `DEFAULT_AGENT` is `consultation`, and it is the planner fallback.
- **The system prompts live in `consilium/agents/prompts.py`**, assembled as
  `SHARED_RULES + specialty`. The shared block carries the non-negotiable constraints (no
  diagnosis, no doses, ground every claim, escalate rather than reassure) so that one of three
  prompts cannot quietly lose a rule.
- **`loop.py` is at `consilium/agents/loop.py`, not `consilium/loop.py`.** The brief writes the
  shorter path; the same brief requires that a reviewer be able to point at any file and name its
  layer, and a `loop.py` beside `cli.py` and `config.py` reads as substrate. Same class of decision
  as `consilium/api/` over a root-level `api/` in §6.
- **Two budgets, one binding.** `max_tool_calls = 2` decides grounding and cost;
  `max_iterations = 3` is an independent guard against a model that never calls a tool. Both are
  overridable **per call**, because `full_budget_6` differs from `full` in exactly this number and
  must not require a second loop instance.
- **Tool calls requested beyond the budget are refused with a `tool` message and emit no
  `tool_call` event.** They did not run; counting them would inflate the distribution that
  `full_budget_6` exists to measure honestly. Every requested call still gets a `tool` message,
  because a tool result with no matching request is rejected by every provider that supports tools.
- **`llm_call.caller` is `forced_answer` only for a call caused by exhaustion.** A run with
  `max_tool_calls=0` (`baseline_llm`) never offered tools, so its calls stay `agent:<name>` —
  otherwise the entire control condition would land in a bucket named after a failure mode.
- **`consilium/runtime.py` is the composition root *and* the turn boundary.** `Runtime` +
  `build_runtime()` wire the layers from `Settings`; `run_turn()` owns the invariant that one user
  turn is one `Tracer`, one trace file, and one `turn` event written last. All three entry points
  go through it. From Phase 5 the routing decision inside a turn belongs to `consilium/router/`;
  the boundary around it stays here.
- **`consilium/safety/escalation.py` is built in Phase 4.** Three `turn` fields are
  `escalation_present()` applied to two different strings, and `OutputRepair` is *defined in terms
  of it* (the banner is prepended only when it returns False), so it is the older of the two. It is
  deliberately **strict** — an explicit seek-care instruction, never a bare keyword — because
  `escalation_present_post_repair` is red-flag recall and a loose detector would flatter it.
  `ESCALATION_PHRASES` is part of the measurement: adding a phrase changes what recall means.
- **In Phase 4 `run_turn` writes the same value into both escalation fields and
  `repair_applied=False`.** Not a placeholder — with no repair in the system, the delivered answer
  *is* the model's own answer, which is what those two fields claim.
- **`Settings.data_dir`** (`CONSILIUM_DATA_DIR`, default `data`) locates `policy.yaml`,
  `red_flags.yaml` and `symptom_systems.yaml` through three properties. One setting rather than
  three: the files are edited and deployed together.
- **`OpenAIProvider` and `AnthropicProvider` land in Phase 4**, because `consilium ask` needs a
  provider factory (`consilium/llm/factory.py`, `make_provider`) and a factory that cannot build
  two of its three providers is not one. Modules are `openai_provider.py`/`anthropic_provider.py`,
  never `openai.py` — a file that shadows the library it imports is a trap. Every cross-provider
  difference is a pure function (`to_openai_messages`, `to_anthropic_messages`,
  `to_anthropic_tools`, `from_*`) and is tested without a client; Anthropic's tool schema is the
  OpenAI one **unwrapped**, never a second derivation from the Pydantic models.
- **The stub clients in `tests/test_provider_clients.py` are not the practice the brief rejects.**
  That rejection is about faking `sentence_transformers`/`chromadb` instead of using the `Embedder`
  and `VectorStore` seams. For the LLM layer the second real implementation is `MockProvider`, and
  it is what the rest of the suite runs against; the stubs exercise request assembly, the retry
  policy and streaming accumulation — code in this repository — through the provider's own `client`
  argument, and claim nothing about a live endpoint.

## 11. Frozen: the router layer (Phase 5)

Planner-worker orchestration with a blackboard. **Never a "swarm"** — §6 already freezes the
naming; this section freezes the mechanism. Rationale in `docs/DESIGN.md` under "Phase 5".

- **`consilium/router/` module layout.** `plan` (the plan model and subtask numbering) → `planner`
  (one LLM call, validation, fallback) → `blackboard` (assignments, statuses, results, event log)
  → `router` (dispatch, deadline, partial failure) → `synthesizer` (fixed-precedence merge).
- **`Subtask` is the router's model; `trace.PlannedSubtask` is substrate's.** `Subtask.to_trace()`
  is the one mapping. Substrate may not depend on a layer above it, so the direction is fixed.
- **Subtask ids are deterministic: `<index>-<agent>`.** A uuid would make two traces of the same
  plan textually different, and comparing traces across ablation presets is something this project
  does.
- **Every unusable planner reply produces the same fallback and sets `route.fallback=True`** —
  empty content, no JSON, unparseable JSON, schema failure, unknown agent, empty plan, provider
  error. An **unknown agent invalidates the whole plan** rather than being dropped; a **repeated
  agent is collapsed** to its first subtask.
- **`extract_json_object` is a brace counter that understands string literals and escapes**, not a
  regex. A regex mis-parse would land in the fallback bucket and be read as a planner that cannot
  produce JSON.
- **A `route` event is emitted only by `router="planner"`.** `router="single"`, `router="none"` and
  a pinned `--agent` emit none, so planner-fallback rate and routing accuracy are n/a for those
  configurations by absence of data rather than by a special case in the metric code.
- **Workers get a `SubtaskHandle`, never the `Blackboard`.** Its entire public surface is
  `subtask`, `agent`, `started`, `completed`, `failed`, `timed_out`. There is no accessor for
  another subtask's assignment or result, so "workers read only their own assignment and write only
  their own result" is a property of the API. Every transition emits a `blackboard` trace event.
- **One shared deadline for the turn** (`DEFAULT_DEADLINE_SECONDS = 90`), applied per worker via
  `asyncio.timeout_at`. A worker that fails or times out is recorded and the turn continues;
  `gather(..., return_exceptions=True)` guards the wrapper itself.
- **Fixed precedence, four parts of it in code**: sections ordered by `AGENT_ORDER`
  (diagnostic → consultation → research), ownership labels on each section, the
  missing-perspective note appended by code, and **no synthesizer call at all when only one worker
  completed**. A failed or empty merge falls back to concatenating the workers' answers in
  precedence order rather than losing them.
- **`TurnOutcome.risk_level` comes from the red-flag table applied to the question**, never from
  the answer or the merge. Nothing downstream can move it.
- **`Router` depends on a `Worker` protocol**, not on `BaseAgent`, and the agent factory is
  injected (importing `consilium/runtime.py` from the router would be a cycle).
- **`tests/__init__.py` exists** so `tests.stubs` has exactly one module name; mypy refuses to
  check a file reachable under two.

## 12. Frozen: the memory layer (Phase 6)

Rationale in `docs/DESIGN.md` under "Phase 6 — memory".

- **`consilium/memory/` module layout.** `working` (the per-session buffer and context compaction)
  → `store` (where a session lives) → `episodic` (cross-session recall in SQLite).
- **It is called *context compaction*, in every identifier, comment and document.** Never "entropy
  management": nothing in the module computes an entropy.
- **Session state is keyed by `session_id` and injected per turn; never a process-wide singleton.**
  `Runtime` holds the `MemoryStore`; nothing holds a session. Sharing between the workers of one
  turn is achieved by passing the same history object.
  `tests/test_memory_store.py` interleaves six sessions under `asyncio.gather` and asserts none
  sees another's turns.
- **`WINDOW_EXCHANGES = 5` replayed verbatim; everything older is compacted by deterministic
  extraction**, never by an LLM call. `llm_call.caller` has no slot for a summarizer, and a
  generated recap would put a nondeterministic string into every later turn's input — which would
  make the 29 multi-turn golden conversations irreproducible.
- **The recap is a `user` message tagged `[earlier in this conversation]`**, not a `system` one:
  Anthropic lifts system messages into a top-level parameter, where a recap would be glued onto the
  agent's own rules.
- **Tool observations are never replayed.** Memory carries the answer and the `doc_id` values, not
  the passages. Replaying them would need the matching assistant tool-call messages — the whole
  prior ReAct transcript, every turn — and would void the current turn's tool budget invisibly.
- **Dedup is `blake2b`, not `hash()`**, which is salted per interpreter run. A golden digest
  computed in another process is asserted in the tests.
- **Redis is a backend, not a dependency.** `SerializedStore` works over `KeyValueBackend`;
  `DictBackend` is a real implementation and is what the tests use; `RedisBackend` imports `redis`
  inside its constructor and `redis` appears nowhere in `pyproject.toml` (only in the mypy
  `ignore_missing_imports` list).
- **`session_id` is validated against the same pattern in `memory/store.py` and `trace.py`** — it
  becomes a cache key in one and a directory name in the other.
- **Episodic memory: one upserted row per session in SQLite, float32 blob, brute-force cosine,
  top 3, behind `EpisodicStore`.** `BRUTE_FORCE_ROW_CEILING = 10_000` is the stated limit past which
  a full-table scan per query stops being reasonable; the store logs a warning above it. The row is
  upserted per turn because neither the CLI nor a stateless API has a session-end signal.
- **`EpisodicMemory.recall_enabled` defaults to `False` and no measured run enables it.** Recall
  across a golden set of independent items would let item N answer from item N−1. The effect of
  episodic memory on answer quality is therefore reported as `not measured` in
  `docs/EVALUATION.md` — a deliberate measurement decision, not an omission.
- **`Settings.data_dir` / `Runtime.memory` / `Runtime.episodic`.** `build_runtime(episodic=True)`
  opts in; the default is `None`.

## 13. Frozen: the safety layer (Phase 7)

Rationale in `docs/DESIGN.md` under "Phase 7 — safety".

- **`data/policy.yaml` is `schema_version: 2`.** It gained an `output` block (escalation banner,
  required elements, forbidden behaviours) and a `red_flags:` **path reference**. It restates no
  emergency phrase; `tests/test_policy.py` loads both files and asserts the intersection is empty.
  `build_runtime` loads the red-flag table from the path the policy names, which is what makes the
  reference load-bearing rather than documentation.
- **`Policy.output` raises when the block is absent.** A permissive default would leave a failed
  load running with no output constraints and every metric still reporting a clean run.
- **Forbidden patterns are regexes; red-flag patterns stay literal phrases.** A dose is a number
  plus a unit, which literals cannot express; the red-flag table's auditability by a non-programmer
  is worth more than uniformity. Forbidden matching is per sentence.
- **Detection and repair are two classes: `PolicyValidator` and `OutputRepair`.** Violations and
  repairs are two counts, reported as two rates, never merged.
- **A forbidden sentence is removed and replaced by a marker naming the rule**, never rewritten. A
  rewrite would be text nobody wrote and nobody checked.
- **Repair order is fixed: redact → prepend the escalation banner → append the disclaimer.**
- **Escalation is decided on the *input*** (the red-flag assessment of the question) **and the
  banner is prepended only when the answer lacks a seek-care instruction**, so a correctly-handled
  red flag emits no repair event. This is why the `turn` event has three escalation fields.
- **The escalation banner must satisfy `escalation_present()`**, and a test asserts it. Otherwise
  the guard would fire and `escalation_present_post_repair` would still be False.
- **The ReAct loop refuses an unpermitted skill *and* `PolicyValidator.check_tool_call` counts it.**
  The loop's refusal is the enforcement; the validator's event is the measurement.
- **Memory records the delivered (post-repair) answer**, so a later turn's context matches what the
  user saw and a redacted sentence cannot return through memory.
- **`safety.post_stream` is set only by a caller that says so.** It was to be the SSE path in
  Phase 9; that path repairs before delivery instead, so **nothing in this repository sets it**
  (§4 refinement 2, §15). `docs/SAFETY.md` must still state plainly what a post-stream repair would
  be -- one the user already saw the unrepaired version of -- and that no shipped path produces one.
- **`consilium runs purge [--session ID] [--yes]`** is the retention mechanism `docs/SAFETY.md`
  documents. It refuses paths outside the configured runs directory and prompts unless `--yes`.

## 14. Frozen: the evaluation harness (Phase 8)

Rationale in `docs/DESIGN.md` under "Phase 8 — the evaluation harness", "Phase 8, closing
Checkpoint B — the labels, and what labelling them taught", and "Phase 8, closing Checkpoint B
— the multi-turn set".

- **`eval/` is a top-level package, not part of `consilium/`.** It depends on the golden set and on
  a live provider, neither of which belongs in an installed wheel. `consilium eval` therefore
  imports it **lazily** and says so when it is absent. This is the opposite call from `api/` (§6)
  and for the opposite reason: the API ships, the harness does not.
- **`addopts` gained `--cov=eval`** (and `coverage.source`, `ruff.src`, `mypy.files` gained `eval`).
  The harness computes every published number; leaving it as the one module whose coverage nobody
  measures would be the wrong place to save a flag. This supersedes the addopts line in §5.
- **Every metric in the brief's §5.2 was checked against the trace schema and all are computable.**
  Nothing is approximated from a side channel. Two carry stated caveats: latency split by
  `route.mode` is defined only where a `route` event exists, and faithfulness needs a judge.
- **`eval/metrics.py` takes trace events and labels, and has no access to the runtime.** That is
  what makes "computed from the trace and nothing else" checkable rather than claimed.
- **`None` means `not measured` and is never rendered as 0.0**, and it survives into
  `summary.json`. `n/a` is a different claim -- structurally undefined for that configuration --
  and `STRUCTURALLY_UNDEFINED` in `eval/report.py` is the list.
- **recall@5 is reported three ways** (union over the turn, first-retrieval-event-only,
  `docs_retrieved_per_turn` beside them); **routing accuracy** unconditionally *and*
  fallback-excluded, with the fallback rate; **red-flag recall** from
  `turn.escalation_present_post_repair` with the raw false-negative count *and the item ids*;
  **faithfulness** in two columns, the oracle one computed for every config including
  `baseline_llm`.
- **`eval/pricing.yaml` ships empty.** A rate card copied from a vendor page is not a measurement.
  An unpriced model makes cost `not measured` and is listed under `unpriced_models`; a partial cost
  is never reported, because it reads as a complete one.
- **`eval/judges/*.md` are versioned prompt files** and the judge model is recorded beside every
  number it produced. `--human-sample N` / `--score-judge CSV` is the validation loop; until it has
  run, `docs/EVALUATION.md` says the judge is unvalidated **in those words**.
- **`load_golden` refuses an unlabelled draft** unless `allow_draft=True`, which `eval/run.py`
  never passes. `labeled: true` is trusted *and* checked, so the flag cannot be a lie. This is
  Checkpoint B enforced in code. `load_multiturn` is the same gate over the other file, and **both
  files now pass without `allow_draft`**; `tests/test_eval_items.py` asserts each refusal against a
  constructed draft, so the gate is checked rather than merely out of the way.
- **`proposed_fields` is what keeps a machine-written candidate from becoming a label by silence.**
  `GoldenItem.missing_labels()` reports a field named there as missing even though it is populated,
  so the refusal above covers an unverified candidate exactly as it covers an empty field. The
  tuple is validated against `LABEL_FIELDS`, because a typo in a provenance marker would disable
  the gate rather than fail loudly.
- **`unverified_fields` is the same names and values with no gate**, and it is what the golden set
  carries now: see §1, Checkpoint B, for why the two are separate. Both markers are validated
  against `LABEL_FIELDS`, both refuse a `JUDGEMENT_FIELDS` name (`expected_route`, `red_flag`) at
  the schema level, and a model validator refuses a field named in both. `unverified_label_counts`
  and `unverified_item_count` are what `eval/run.py` publishes.
- **The unverified-reference caveat is rendered in the results table, not in a footnote.**
  `report.md`'s ablation header reads `recall@5 (vs. unverified ref)` and the same for both
  faithfulness columns; a bold line sits under the table; the per-config Retrieval and Judge
  paragraphs repeat it. Routing and red-flag columns are unmarked, because a blanket caveat would
  claim every number is equally soft and that is false. It is rendered from
  `RunSummary.unverified_labels`, so clearing the markers removes it with no code change.
- **`eval/validate.py` holds the cross-file checks**, run by `tests/test_eval_drafts.py`:
  `unknown_doc_ids` (an **error** — a `doc_id` naming no note is a label nobody can retrieve),
  `route_document_mismatches` (a **warning** against a reviewed baseline — see §1, Checkpoint B),
  `ungrounded_items`, and `label_note_agent_mentions`. The exclusive skill grants are **read from `data/policy.yaml`** and the
  implied skills from the labelled notes' corpus `category`; nothing is restated. `condition` is
  deliberately not in `CATEGORY_SKILL` (it is reachable through both `analyze_symptoms` and the
  unfiltered `search_knowledge`, so it names no owning agent), and `search_knowledge` can never be
  in it, which is exactly why the check is a warning.
- **`eval/run.py` refuses a `mock` provider.** Numbers from a scripted mock are not measurements.
- **The harness runs items through `run_turn`**, the same path the CLI and the API use, each item in
  **its own session** so working memory cannot carry item N-1 into item N. Multi-turn conversations
  are the only place a session spans turns. Items run **sequentially**: concurrency against a
  rate-limited endpoint turns p90 latency into a measurement of the queue.
- **Episodic recall is off in every measured run** and its effect on answer quality is reported as
  `not measured` (§12).
- **`full_budget_6` runs on a 50-item *stratified* subset**, reported separately with its n stated.
  The golden set is written in category blocks, so the first 50 items would be two categories.
- **The shipped `eval/data/*.jsonl` are unlabelled drafts** and `tests/test_eval_drafts.py` lints
  them: 150 items in five blocks of 30, ids prefixed by block, both judgement fields empty on every
  record, every populated field declared in `proposed_fields`, a proposed reference answer only
  where documents are proposed with it, every proposed `doc_id` naming a real corpus note, no
  proposal longer than three notes, `draft_notes` present on every record, and the drafting
  constraints enforced — **no hard-stratum red-flag candidate reuses a string from
  `data/red_flags.yaml`**, every candidate carries a non-null `phrasing_stratum` and no other item
  does, the retired marker strings never reappear in `draft_notes`, the strata hold exactly 22 and
  5, and the only items outside the easy stratum that contain a pattern string are marked
  `FALSE-POSITIVE PROBE`
  (a negated "chest pain" and three historical "heart attack" mentions, none of them red-flag
  items). The 30 `condition_coding` items are asserted to vary along **six named conventions**
  rather than along the condition axis. The multi-turn set is **29** conversations (132 turns, 83
  annotated) with **10 of 7+ turns**, of which **exactly five** carry a dependency reaching past
  the 5-exchange window — pinned as an exact tuple, with the referent-count mix, the turn-length
  distribution and the absence of any self, forward or turn-0 dependency asserted beside it.
- **`MultiturnTurn.depends_on_turn` is `int | list[int] | None` in, a tuple after** — see §1,
  Checkpoint B. A turn index and its referent must both be present or both absent, and every
  referent must name a strictly earlier turn; both are **refused at the loader**, because such a
  reference would reach the judge as a referent the model has not been shown and the resolution
  number would come back looking merely poor. `MultiturnConversation.dependency_reach()` measures
  to the **earliest** referent, and `eval/validate.py`'s `past_window_conversations` compares it
  against `WINDOW_EXCHANGES` read from `consilium.memory` rather than a literal `5`.
- **`eval/run.py` writes the five-conversation compaction caveat into every run's `summary.json`.**
  Computed from the file by `_compaction_note`, so it cannot go stale against it, and carried with
  the numbers rather than left in `docs/EVALUATION.md` alone.
- **`phrasing_stratum` is a field on `GoldenItem`, not a substring of `draft_notes`** — see §1,
  Checkpoint B, for why. It is outside `LABEL_FIELDS`, so it is never proposed, never reported
  missing, and never something the labelling gate waits on.
- **`g-md-027` is kept with no coverage on either side, and its `draft_notes` say so.** The
  presentation is deep vein thrombosis; there is no corpus note and no rule in
  `data/red_flags.yaml`. **No DVT rule and no DVT note were added** — inventing coverage to make an
  item pass inverts what the item measures. The note exists so that a false negative there is
  attributed to absent coverage rather than to matcher failure, and the item carries no stratum so
  it cannot be pooled into the hard-phrasing number.
- **Observed at drafting time and recorded in `docs/EVALUATION.md`: the matcher hits 0 of the 22
  hard-phrasing candidates and 5 of the 5 easy-phrasing ones**, under both negation policies. The
  0 is the finding the constraint was written to produce, not a defect in the questions; the 5 is
  what makes the 0 attributable to phrasing rather than to a broken table. The questions are not
  edited toward the patterns. The same run shows the negation guard suppressing `g-su-022` and
  **not** suppressing the three historical "heart attack" probes, which are measured false
  positives and are left in place to be reported.

## 15. Frozen: the interfaces -- the REPL, the API, the SSE stream (Phase 9)

Rationale for each decision, with the rejected alternative, is in `docs/DESIGN.md` under
"Phase 9 -- the interfaces: the REPL, the API, and the SSE stream".

- **`consilium/api/` module layout.** `models` (the request and response models) -> `app`
  (`create_app`, the four endpoints, the per-session locks) -> `main` (`app = create_app()`, the
  ASGI entry point). Nothing in the package reaches below the turn boundary: an endpoint builds a
  session id and a turn index, calls `run_turn`, and renders what comes back. Import path is
  `consilium.api.main:app` (§6); importing it builds no runtime and loads no corpus, so generating
  the OpenAPI schema does not need an embedding model.
- **Every answer carries `sources`, `route`, `risk_level` and `trace_id`**, as required fields of
  `AnswerResponse`, so an endpoint cannot forget one. `safety` (violations and repairs, as two
  lists) is carried beside them. `GET /healthz` and `GET /v1/sessions/{id}` are not answers and
  carry none of it.
- **Every request model is `extra="forbid"`.** A `sessionid` typo would otherwise start a fresh
  conversation on every request, which reads as a memory bug in a layer that is working.
- **`consilium chat` holds one session id for the whole REPL and opens no second memory path.**
  The session's `WorkingMemory` is the one `run_turn` fetches from `Runtime.memory` by the tracer's
  session id -- the same store the API and the harness use, keyed the same way -- so a conversation
  held by hand exercises the code the multi-turn numbers come from. `--session` also continues the
  **trace** numbering: the REPL starts at the first turn index with no file on disk, because a
  trace sink appends and starting again at 0 would interleave two turns' events in one file.
- **The SSE event sequence is `escalation`? -> `token`* -> `done`, or `error`.** The banner is
  decided from the question alone and is emitted **before routing starts**; `stream_turn` is public
  precisely so that guarantee is assertable directly (pull one event, assert the provider has not
  been called yet), because an ASGI test transport buffers the body and can only show wire order.
  The banner is delivered once: `OutputRepair` prepends exactly `banner + "\n\n"`, so the prefix is
  stripped from the body by construction, and `done.answer` still carries the complete delivered
  text -- byte-identical to the `turn` event's, which a test asserts against the trace file.
- **The answer body is delivered incrementally but is not a provider-token stream, and repair
  happens before the first byte.** `post_stream` is never set. See §4 refinement 2 for the argument
  and for what would have to change to reverse it.
- **A failure after the stream has started is a terminal `error` event, not a status code.** An SSE
  response commits to 200 with its first byte.
- **`GET /v1/sessions/{id}` returns the shape of a conversation and none of its content**:
  `turns`, `window_exchanges`, `compacted_turns`, `observations_deduplicated`. No question text, no
  answer text, no cited `doc_id` values, no risk levels. **An unknown id, a purged id and a
  malformed id all produce the same `404 {"detail": "no such session"}`.** The project has no
  authentication, so the endpoint cannot tell the caller who started a session from a caller who
  guessed its id; that fact decides what it may return rather than being a caveat attached to it.
  It reads the store's key list rather than `MemoryStore.get`, which creates on miss -- a probe
  through `get` would make every guessed id exist. **This is not an authorization boundary and the
  project does not claim one**; the README's limitations say so.
- **Server-minted session ids are `api-` plus 96 bits of hex.** An id is the only thing standing
  between a stranger and a session's metadata, so it must not be guessable.
- **Turns of one session are serialized by an `asyncio.Lock` held on the app instance; different
  sessions are not.** Two requests on one session would otherwise read the same buffer, derive the
  same turn index, and write two turns into one trace file. The lock dict grows with the number of
  sessions served, which is accepted for a demo server and written down.
- **`turn_index` is `len(runtime.memory.get(session_id))`, read under that lock, and `create_app`
  refuses a runtime with `memory=False`** -- with memory off nothing is recorded, so every turn of
  a session would be turn 0. The memory-off presets are for the ablation table, which runs through
  the harness with one session per item.
- **Nothing session-scoped is held on the app, on a module, or in a cached dependency.** The state
  is read off `request.app.state`, so two apps in one process do not share a `MemoryStore`.
  `tests/test_api_concurrency.py` asserts the property at this layer -- and asserts it about *what
  reached the model*, not about what came back: two responses can look right while the prompts that
  produced them were contaminated. It requires that the two conversations actually overlapped
  before it checks that no single LLM call saw two sessions' questions.
- **The single-file demo at `web/index.html` is served by the API itself, at `GET /`.** Same origin
  as the endpoint it calls, so no CORS policy is opened for a demo; it loads nothing from the
  network, has no build step, and is not part of the wheel -- so `GET /` is a 404 wherever the file
  is absent rather than a startup failure. It is resolved relative to `Settings.root_dir` and
  excluded from the OpenAPI schema.
- **The API tests drive the app through `httpx.ASGITransport`, not `starlette.testclient`.** The
  installed Starlette deprecates its test client with `httpx` and asks for a package this project
  does not depend on and §2 of the brief does not list; under `filterwarnings = ["error"]` that is
  a collection error. `httpx` is already the named dev dependency.

## 16. Phase status

| phase | state | commit |
|---|---|---|
| 1. Scaffold, trace schema, offline seams, CI | done | `92a47e3` |
| 2. Corpus, red flags, chunking, BM25, RRF, ingest | done | `1278c8e` |
| 3. Skills + registry | done | `48dcfbc` |
| 4. ReAct loop + agents + `trace` CLI | done | `b8caed0` |
| 5. Planner, router, blackboard, synthesizer | done | `1d6660e` |
| 6. Memory | done | `a36b160` |
| 7. Safety | done | `48a1dfa` |
| 8. Eval harness + golden set | **done — Checkpoint B cleared on both files.** Golden set hand-labelled blind and frozen; multi-turn set labelled, `m-017` dropped, 29 conversations frozen; provenance split from the gate; cross-file lint over both files. | |
| 9. API + SSE + CLI polish | **done.** `consilium chat`; `consilium/api/` with `/v1/ask`, `/v1/chat` (SSE), `/v1/sessions/{id}`, `/healthz`; banner-before-token, API-layer concurrency and session-leak tests. One departure from a frozen decision, §4 refinement 2. | |
| 10. Docs, published eval run, README numbers | not started -- `docs/SAFETY.md` still to be written | |
