# Evaluation

> **Not medical advice.** This is an educational software project. It does not diagnose, treat, or
> provide clinical guidance, and it must not be used for real medical decisions. No patient data of
> any kind may be used with it.

Every number this project publishes is produced by `eval/run.py` and computed from the trace events
in `consilium/trace.py`. Anything not produced that way is written **`not measured`**, and anything
structurally undefined for a configuration is written **`n/a`**. Neither is ever filled in by hand.

**Current state: the golden set is an unlabelled draft, so nothing has been measured yet.** Every
results number in this document and in the README reads `not measured` until the sets are labelled
and a sweep has been run. Two of the four label fields ship holding machine-written candidates for
the owner to verify, and the loader counts an unverified candidate as an unlabelled field — see
§1.1.1, which is the part of this document an interviewer should read first.

---

## 1. The golden set

`eval/data/golden.jsonl` — **150 questions**, thirty in each of five blocks:

| block | what it tests |
|---|---|
| `general_health` | condition explanation, mechanism, background |
| `symptom_urgency` | how urgent a described symptom is |
| `condition_coding` | ICD-10 code selection and the conventions that govern it |
| `guideline_evidence` | what guidance recommends, and where authorities differ |
| `multi_dimensional` | questions with parts that different specialists own, labelled `mode: parallel` |

The last block is not optional. With only single-specialty questions a router that always answers
"single" scores 100% and the routing metric measures nothing.

Each record:

```json
{"id": "...", "question": "...", "category": "...",
 "expected_route": {"mode": "single|parallel", "agents": ["..."]},
 "relevant_doc_ids": ["..."],
 "reference_answer": "...",
 "red_flag": false,
 "labeled": true,
 "proposed_fields": [],
 "draft_notes": "..."}
```

`eval/data/multiturn.jsonl` — **30 conversations** in which a later turn contains a pronoun or an
ellipsis that only resolves against an earlier turn. **Ten run seven turns or more**, so that they
exceed the five-exchange working-memory window and actually exercise the compaction path. Thirty
two-turn conversations would test the window by never reaching it.

### 1.1 The labelling gate

`eval/items.py` **refuses to load an unlabelled file** unless the caller explicitly asks for a
draft, and `eval/run.py` never asks. That is deliberate: an evaluation set the system wrote and
graded itself against measures nothing, and the only thing that makes these numbers mean anything is
that a person annotated the labels by hand. A gate in a document is a request; a gate in the loader
is a constraint.

The shipped draft has `labeled: false` on every record. What it carries besides the question is
`draft_notes` — one line per item saying what the item was written to test, and, where a proposal
was uncertain, what it could not settle. That is authoring intent, not an answer key.

### 1.1.1 Which fields are machine-proposed, and which are the owner's alone

This split is the substance of the checkpoint, so it is stated exactly rather than summarized.

| field | who writes it | why |
|---|---|---|
| `expected_route` | **the owner, alone** | Which specialists genuinely own parts of a question is a judgement, and it is the label routing accuracy is computed against. A candidate here would anchor the number the ablation exists to produce. |
| `red_flag` | **the owner, alone** | Whether a question describes a presentation that must produce a seek-care instruction is a judgement, and it is the label red-flag recall is computed against. Same reason, higher stakes. |
| `relevant_doc_ids` | machine-proposed candidate | Mechanical: naming the notes that answer a question is what reading the corpus consists of, and the owner has to open the same documents either way. |
| `reference_answer` | machine-proposed candidate | Mechanical for the same reason, and it is written **only** from the documents proposed beside it. |

A candidate is not a label. Every field holding one is named in the record's `proposed_fields`, and
`GoldenItem.missing_labels()` reports a proposed field as **still missing**, so the loader refuses a
file holding candidates exactly as it refuses one holding empty fields. Labelling a field means
verifying or overwriting the candidate and removing its name from `proposed_fields`; there is no
way to accept the 148 machine-written reference answers by leaving them alone. `proposed_fields` is
validated against the four label names, because a typo in a provenance marker would silently
disable the gate it holds open.

Two items ship with **no** candidate at all — `g-gh-030` and `g-md-027` — because the corpus does
not support an answer to them. Both say so in `draft_notes` and both are flagged for the owner to
re-aim or cut. That is the honest output of the proposal pass rather than a gap in it: an item with
a fabricated source is worse than an item with none.

**What the owner still has to check in the machine-written fields.** They are candidates from a
model that also wrote the corpus notes they cite, so the failure mode they are most exposed to is
an answer that is fluent, plausible and slightly beyond what the note actually says. The reference
answers deliberately keep the corpus's hedges — "commonly stated", "guidance describes", the bands
rather than a single number — because a reference answer that sharpens an approximate threshold
would score every correct hedge as unfaithful.

### 1.2 Labelling process

For each item, in this order:

1. **`red_flag`** — is the question describing a presentation that should produce a seek-care
   instruction? Decide from the question, not from what the matcher does with it.
2. **`expected_route`** — which specialists genuinely own parts of this question?
   `consultation` owns background, lifestyle and classification; `diagnostic` owns urgency and
   symptom grouping; `research` owns guidance and evidence strength. Assign the fewest that can
   answer it. Everything in the `multi_dimensional` block should end up `mode: parallel`.
3. **`relevant_doc_ids`** — a candidate is already there. Read the notes it names, correct it, and
   remove `relevant_doc_ids` from `proposed_fields`. `docs/CORPUS.md` lists every `doc_id`. Prefer
   being strict: a document that merely mentions the topic is not relevant, and inflating this list
   deflates recall. Proposals are at most three notes for that reason, and `draft_notes` says where
   a proposal deliberately named fewer.
4. **`reference_answer`** — a candidate is already there, written only from the documents proposed
   beside it. Check it against those notes, correct it, and remove `reference_answer` from
   `proposed_fields`. It is a reference for a human reading the report, not a string the harness
   matches against.
5. Set `labeled: true`. The loader will still refuse the file while any `proposed_fields` entry
   remains, which is what makes step 3 and step 4 non-skippable.

For a multi-turn conversation, annotate at least one later turn with `depends_on_turn` (the
zero-based index of the turn it refers back to) and `expected_referent` (what the pronoun or
ellipsis means), then set `labeled: true`.

### 1.3 The drafting constraints that the labelling must not undo

#### The red-flag items are stratified by phrasing difficulty, and recall is reported per stratum

**Hard-phrasing stratum — 22 items, and they do not reuse the strings in `data/red_flags.yaml`.**
They are written the way users actually type: hedged, contracted, misspelled, pluralised, described
rather than named, and in several cases buried inside a longer and mostly routine question. If a
hard-stratum item echoed a pattern string, that stratum's recall would measure only whether the
matcher matches itself.

**Easy-phrasing stratum — 5 items, in plain, direct phrasing that names the symptom the ordinary
way.** These are exempt from the rule above by construction, and they exist because a set with only
the hard stratum produces a number with nothing to compare it against: it would be a lower bound
whose distance from the upper bound is unknown, and real users do type the canonical terms. The
five cover five different rules — cardiac chest pain, stroke, anaphylaxis, severe breathing
difficulty, and suicidal ideation.

**Red-flag recall is therefore reported per stratum and never pooled into one figure.** A pooled
number would move with the ratio between the strata, which is a drafting choice rather than a
property of the system. Every candidate declares its stratum in `draft_notes`, and
`tests/test_eval_drafts.py` asserts that each declares exactly one — a stratum defined by negation
would silently absorb any item nobody classified.

**Expect the hard stratum to produce misses, and expect them to be the finding.** A red-flag item
the matcher does not catch is a measured false negative, reported with its item id. It is not a
defect in the question, and the fix is not to edit the question toward the pattern.

#### The matcher diagnostic, per stratum

Run over the questions with no model called, no label written and nothing scored:

| stratum | n | matched (raw) | matched (negation guard on) |
|---|---|---|---|
| hard phrasing | 22 | **0** | **0** |
| easy phrasing | 5 | **5** | **5** |

That contrast is the designed comparison. The 0 measures what the rule table does with paraphrase,
and the 5 confirms that the 0 is a property of the phrasing rather than of the table being broken.
Neither number predicts red-flag recall, which is measured on the *delivered answer* and which the
model may well produce unaided; what they predict is that the input-side guard contributes close to
nothing on realistically-phrased input, and that the gap between `escalation_present_pre_repair`
and `escalation_present_post_repair` will be small on the hard stratum and could be anything on the
easy one.

Several hard-stratum misses are near misses that a slightly broader pattern would catch — "worst
hedache ive ever had" against `worst headache ever`, "ribs pulling in" against `ribs sucking in`,
"rash that does not go pale" against `rash that does not fade`. That is the actionable form of the
finding, and it belongs in the results rather than in an edit to the questions.

#### The false-positive probes

**Four items reuse a pattern string on purpose** and are not red-flag items: `g-su-022` (a negated
"chest pain"), `g-ge-018` and `g-md-011` (a historical "heart attack" in a guideline question and
in a family-history question), and `m-018` in the multi-turn set (the same, about a parent). They
probe the matcher's **false-positive** behaviour, which the hard stratum cannot measure.

The same diagnostic run reports what they do today: `g-su-022` matches raw and is **suppressed by
the negation guard**, which is the guard working as designed; the three historical "heart attack"
mentions match and are **not** suppressed, because nothing in them is negated. Those three are
measured false positives of the input-side rule table, they are what the probes exist to surface,
and they are not a reason to remove "heart attack" from the table — deciding that is a
post-measurement question for `docs/DESIGN.md`, with the recall column beside it.

#### Five items were dropped to make room for the easy stratum

The block stays at 30 and the set stays at 150, so five non-red-flag symptom items came out. Each
was chosen because it was the weakest kind of item to lose — one with no supporting note, or one
whose answer draws on documents another item already covers, since near-duplicate items inflate
apparent performance the same way duplicate documents inflate retrieval scores:

| dropped | why |
|---|---|
| `g-su-020` (knee pain on stairs) | Same notes as `g-md-015`, which asks the knee question and adds the urgency dimension. |
| `g-su-023` (waking at 3am for two months) | Same notes as `g-gh-010` and `g-gh-021`. |
| `g-su-025` (pins and needles in the fingers) | No corpus note covers hand paresthesia; `relevant_doc_ids` would have been empty or a stretch. |
| `g-su-026` (ankles puffy by evening) | Same note as `g-md-007`, which asks it with the context that makes it answerable. |
| `g-su-029` (cold symptoms, day four) | Same note as `g-su-019`, and the cold is already covered twice in `general_health`. |

The five new items are `g-su-031` to `g-su-035`. The gaps in the id sequence are deliberate: a
dropped id is not reused, so a trace or a note referring to `g-su-025` cannot silently come to mean
a different question.

**The 30 `condition_coding` items vary along the *convention* axis, not the *condition* axis.** The
`coding` category carries per-condition code-selection detail for seven conditions and chapter-level
coverage for the rest (see `docs/CORPUS.md`), so thirty questions differing only in which condition
they name would be thirty near-duplicates drawn from seven documents — and near-duplicate golden
items inflate apparent performance the same way duplicate documents inflate retrieval scores. The
items vary instead along: the "with" presumption; combination codes; required second codes and their
sequencing; three-character codes that take no further character; undecimalized roots versus the
full code; and chapter boundaries.

---

## 2. Configurations

`RunConfig` has independent toggles — `retrieval`, `router`, `memory`, `max_tool_calls`,
`max_iterations` — threaded from `eval/run.py` through the router and the loop, so a reduced mode is
a flag and not a code edit.

| preset | retrieval | router | memory | max tool calls |
|---|---|---|---|---|
| `baseline_llm` | off | none | off | 0 |
| `single_agent_rag` | on | single | off | 2 |
| `full` | on | planner | on | 2 |
| `full_no_memory` | on | planner | off | 2 |
| `full_budget_6` | on | planner | on | 6 |

The first four are the rows of the paired ablation table. **`full_budget_6` is a diagnostic**, run
on a 50-item stratified subset and reported separately with its n stated: it exists to show the
untruncated tool-call distribution, because a distribution measured at the cap cannot justify the
cap.

**Episodic memory is disabled in every measured run.** The golden set's items are independent
questions, so cross-session recall would let item N be answered from item N−1's stored summary,
contaminating faithfulness, recall@5 and the ablation together. The effect of episodic memory on
answer quality is therefore **`not measured`** — the truthful label for a component deliberately
switched off while measuring.

---

## 3. Metrics

Every metric below is computed in `eval/metrics.py` from the trace events listed beside it. **All of
them were checked against the event schema before the module was written, and all are computable
from it**; none is approximated from a side channel.

| metric | definition | source events |
|---|---|---|
| routing accuracy | exact match on `(mode, sorted(agents))` | `route` |
| planner fallback rate | share of turns where `route.fallback` is true | `route` |
| retrieval recall@5 | `\|retrieved ∩ relevant\| / \|relevant\|` after per-`doc_id` dedup | `retrieval` |
| retrieval hit@5 | share of items with ≥1 relevant document in the top 5 | `retrieval` |
| retrieval MRR@10 | reciprocal rank of the first relevant document in the fused top-10 | `retrieval` |
| red-flag recall | `turn.escalation_present_post_repair` over items labelled `red_flag: true`, reported per phrasing stratum | `turn` |
| safety violation rate | `safety` events with `event="violation"`, per 100 turns | `safety` |
| safety repair rate | `safety` events with `event="repair"`, per 100 turns | `safety` |
| latency p50 / p90 | `turn.wall_ms`, with n stated, split by `route.mode` where one exists | `turn`, `route` |
| tool calls per turn | mean and full distribution | `tool_call` |
| tokens per turn | summed over **all** `llm_call` events, split by caller and by mode | `llm_call` |
| cost per turn | tokens × the rates in `eval/pricing.yaml`, keyed on provider + model | `llm_call` |
| answer faithfulness | share of answer claims supported by a source | LLM judge |
| multi-turn resolution | whether a later turn resolved its annotated referent | LLM judge |

### 3.1 Definitions that are easy to get subtly wrong

**recall@5 is reported three ways**, because a single number would hide which system produced it:

- **union over the turn** — every top-5 from every retrieval in the turn. The system-level number:
  what the answer could have been grounded in.
- **first retrieval event only** — configuration-independent, and therefore the number that is
  comparable across presets that perform different numbers of retrievals.
- **`docs_retrieved_per_turn`**, reported beside both, so they are interpretable together. A union
  recall that beats the first-event number because the turn retrieved four times is not the same
  result as one that did it in a single call.

**Routing accuracy headlines the unconditional number**, with fallback turns counted as their
effective behaviour — a fallback runs a single `ConsultationAgent`, and if the label said that, it
was right. The fallback rate is reported beside it and the fallback-excluded number as the second
column. Reporting only the excluded number would let a planner that fails half the time look perfect.

**Red-flag recall is measured on the delivered answer**, is reported **per phrasing stratum**
(§1.3) rather than pooled, and comes **with the raw false-negative count and the item ids**, not
only a rate. A rate of 0.93 over 30 items hides two
people. It is computed from `turn.escalation_present_post_repair` rather than from repair events,
because `OutputRepair` prepends the banner *only* when the answer lacks a seek-care instruction — so
a correctly-handled red flag emits no repair at all, and measuring the repair would score the
system's best behaviour as a false negative. `escalation_present_pre_repair` is reported beside it,
so the model's unaided performance and the guard's contribution are visible separately.

**Faithfulness has two columns.** The first judges the answer against what the run actually
retrieved. The second judges it against the golden set's `relevant_doc_ids` — the *oracle* column —
and is computed for **every** configuration including `baseline_llm`, which retrieves nothing and
would otherwise be structurally n/a on the only faithfulness column, leaving the control condition
unjudged on the metric the comparison is about.

**Violations and repairs are two rates and are never summed.** One says how often the model produced
non-compliant output; the other says how often the guard had to act. Merged, they would hide a model
getting worse behind a guard that kept working.

**Latency is split by `route.mode` only where a `route` event exists.** `single_agent_rag` and
`baseline_llm` make no routing decision, so the split is undefined for them rather than attributed to
a mode nobody chose.

**No p99.** With ~150 items split two ways it is one order statistic over a few dozen samples.
Percentiles are nearest-rank, so a reported p90 is a latency some turn actually had.

### 3.2 The negation guard, settled by measurement

The red-flag matcher suppresses a match when an explicit negation cue precedes it within three
tokens ("no chest pain", "denies chest pain"). Both policies are recorded from a single run: the
`turn` event carries `red_flag_matched_raw` (ignoring negation), `red_flag_matched` (the policy in
force) and `red_flag_negation_suppressed` (the discordant set).

**If the guard costs any recall on the labelled set, the default reverts to raw matching**, and
`docs/DESIGN.md` says so with the two numbers. Until a sweep has been run, this is `not measured`.

---

## 4. The judge

Judge prompts are versioned files in `eval/judges/`, and the judge model name is recorded in
`summary.json` beside every number it produced. A prompt change is a change to the measurement, and
it belongs in a diff.

**A judge whose agreement with a human was never measured is an unvalidated instrument**, and
reporting faithfulness from it without saying so is the same error as writing an unmeasured number
into the README. Validation is a two-command loop:

```bash
python -m eval.run --human-sample 40      # writes eval/results/<ts>/judge_sample.csv
python -m eval.run --score-judge <path>   # reports raw agreement and Cohen's kappa
```

The CSV has columns `item_id, question, answer, retrieved_doc_ids, judge_label, judge_rationale,
human_label`, with the last column blank. Rows left blank are skipped rather than counted as
disagreements — otherwise the agreement number would depend on how far the labeller got.

Cohen's kappa is reported alongside raw agreement because the label distribution is skewed: two
raters who both mostly say "supported" agree about 90% of the time by chance, and a raw number alone
would read as a validated judge.

**Judge agreement: `not measured`.** No sample has been labelled yet.

---

## 5. Running it

`eval/run.py` **requires a live API key and is never invoked by `pytest`.** It is the one part of
this repository that costs money.

```bash
python -m eval.run --help
python -m eval.run --config full --limit 10      # smoke test before spending on the sweep
python -m eval.run                                # the full ablation
consilium eval --config full --limit 10           # the same thing through the CLI
```

It refuses to run against a `mock` provider: numbers from a scripted mock are not measurements.

### 5.1 Sizing a run

The call count is exact arithmetic and is stated here as such. The token count and therefore the
cost are **`not measured`** until a run happens, and `eval/run.py` computes them from the trace.

Per turn, by configuration:

| configuration | LLM calls per turn |
|---|---|
| `baseline_llm` | 1 (no planner, no tools, no synthesizer) |
| `single_agent_rag` | 1–3 (the specialist, plus up to two more if it uses its tool budget) |
| `full` | 2–5 (planner + specialist round trips + a synthesizer call on a parallel turn) |
| `full_no_memory` | as `full` |

A full sweep is 150 items × 4 configurations, plus a 50-item `full_budget_6` diagnostic, plus 30
multi-turn conversations at the `full` configuration. That is **650 golden turns** plus roughly 150
conversation turns. At the per-turn call counts above the sweep is on the order of **1,500–2,500
provider calls**, before the judge.

The judge adds up to two faithfulness calls per item per configuration — up to **1,200 calls** — plus
one per annotated multi-turn turn. `--no-judge` skips them entirely.

**Cost: `not measured`.** `eval/pricing.yaml` ships empty on purpose. A rate card copied from a
vendor page at some past date is not a measurement: it looks measured, goes stale silently, and would
put a fabricated dollar figure in the results table. Fill it in from your provider's current price
list at the time of the run; `summary.json` records the rates that were used. When a model has no
entry, cost is reported as `not measured` and the model is listed under `unpriced_models` — never as
a partial total, which reads as a complete one.

### 5.2 Results

`eval/run.py` writes `eval/results/<timestamp>/{report.md, summary.json}` plus the full traces under
`runs/`. The run that is published is copied to **`eval/results/published/`, which is committed while
the timestamped directories are not**. A published number whose evidence is gitignored is a number
no reviewer can check.

`summary.json` records the commit, the date, the provider and model, the judge model, the golden-set
path, the Python version and platform, and the pricing source, alongside every metric.

---

## 6. Results

**`not measured`.** The golden set is an unlabelled draft; see §1.1. This section is filled in from
`eval/results/published/summary.json` when a sweep has been run against a labelled set.

| configuration | routing acc | recall@5 | faithfulness | red-flag recall | p90 latency | tokens | cost |
|---|---|---|---|---|---|---|---|
| `baseline_llm` | n/a | n/a | n/a | not measured | not measured | not measured | not measured |
| `single_agent_rag` | n/a | not measured | not measured | not measured | not measured | not measured | not measured |
| `full` | not measured | not measured | not measured | not measured | not measured | not measured | not measured |
| `full_no_memory` | not measured | not measured | not measured | not measured | not measured | not measured | not measured |

---

## 7. Threats to validity

These are stated before any number exists, so that they cannot be chosen after seeing the results.

**The golden set is small.** 150 items, 30 per block. A difference of a few percentage points
between two configurations on 30 red-flag items is not a difference; the confidence interval on a
proportion at n=30 is wide enough to swallow most of what the ablation is looking for. Read the
counts, not the third decimal place.

**One labeller.** Every label is a single person's judgement, and no inter-annotator agreement is
measured because there is only one annotator. `relevant_doc_ids` in particular is a judgement call —
whether a note that mentions the topic "answers" the question — and a second labeller would disagree
on some of them.

**The corpus was written by the author of the system being evaluated.** The retrieval numbers are
therefore an upper bound in a way that a public corpus would not be: the questions and the documents
share vocabulary because the same person chose both. `scripts/ingest_medquad.py` exists so the system
can be pointed at an external corpus, and any run against one should be reported separately.

**The judge is the same family of model as the system.** Self-preference in LLM judging is
documented and real. This is what the human-agreement loop in §4 exists to bound, and until it has
been run the faithfulness numbers come from an unvalidated instrument.

**The coding block is narrower than it looks.** Seven of sixteen conditions have a per-condition
code-selection note; the rest are covered at chapter level (`docs/CORPUS.md`). The 30
`condition_coding` items therefore vary along the convention axis rather than the condition axis, and
the block measures coverage of coding *conventions*, not of conditions.

**The red-flag items were written with the pattern list in view, and the strata are unbalanced.**
The drafting constraint required it for the hard stratum — those items must not reuse the pattern
strings — so that stratum is probably harder than a blind sample of real user input would be, and
its number is a lower bound. The easy stratum bounds it from the other side, but with five items
against twenty-two the ratio is a drafting choice and not an estimate of how real traffic is
phrased, which is exactly why the two are reported separately and never pooled. Neither stratum is
a sample of anything: both were written by one person, with the rule table open.

**The reference answers were written by a model, against a corpus the same model family wrote.**
They ship as candidates rather than labels, and the loader refuses to score anything until a person
has cleared each one (§1.1.1). Until that happens, "the owner labelled it" is true of
`expected_route` and `red_flag` by construction and true of the other two fields only once the
`proposed_fields` markers are gone.

**Mock-provider numbers are never reported.** The test suite runs entirely against `MockProvider`
with synthetic token counts. `eval/run.py` refuses to run against it.

**Latency depends on the endpoint, not only on the architecture.** p90 on a rate-limited key
measures the queue. The sweep runs items sequentially for that reason, and the run's provider, model
and date are recorded so a latency number is at least interpretable.
