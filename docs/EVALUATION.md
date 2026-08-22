# Evaluation

> **Not medical advice.** This is an educational software project. It does not diagnose, treat, or
> provide clinical guidance, and it must not be used for real medical decisions. No patient data of
> any kind may be used with it.

Every number this project publishes is produced by `eval/run.py` and computed from the trace events
in `consilium/trace.py`. Anything not produced that way is written **`not measured`**, and anything
structurally undefined for a configuration is written **`n/a`**. Neither is ever filled in by hand.

**Current state: the golden set is labelled and frozen; no sweep has been run, so nothing has been
measured yet.** Every results number in this document and in the README reads `not measured` until
a sweep has been run against the frozen set. Two of the four label fields were written by hand and
two were written by a model and knowingly left unverified — see §1.1.1 and §1.5, which are the
parts of this document an interviewer should read first.

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
 "unverified_fields": ["relevant_doc_ids", "reference_answer"],
 "phrasing_stratum": "hard | easy | null",
 "draft_notes": "..."}
```

`proposed_fields` and `unverified_fields` are two **provenance markers that do opposite things**,
and the difference between them is §1.1.1. `proposed_fields` is a gate: a field named there is
reported as missing by `GoldenItem.missing_labels()` and `load_golden` refuses the file.
`unverified_fields` is a record: same field names, same values, no gate — it says a machine wrote
the value and no person checked it, and its counts are republished in every run's `summary.json`
and printed in the results table. They are disjoint per record, because a field cannot be both
still waiting on the labeller and knowingly unverified.

The last two fields are not labels. `phrasing_stratum` is the drafting decision red-flag recall is
split on (§1.3) and `draft_notes` is authoring intent; neither is the owner's to fill in, and
neither appears in `LABEL_FIELDS`.

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

The golden set has been through that gate. `eval/data/multiturn.jsonl` has not: it is still a
draft, every conversation carries `labeled: false`, and `load_multiturn` still refuses it.

### 1.1.1 Which fields a person wrote, and which a model wrote and nobody checked

This split is the substance of the checkpoint, so it is stated exactly rather than summarized.

| field | who wrote it | verified by a person | why |
|---|---|---|---|
| `expected_route` | **the owner, by hand, blind** | yes, on all 150 | Which specialists genuinely own parts of a question is a judgement, and it is the label routing accuracy is computed against. A candidate here would have anchored the number the ablation exists to produce, so nothing ever proposed one. |
| `red_flag` | **the owner, by hand, blind** | yes, on all 150 | Whether a question describes a presentation that must produce a seek-care instruction is a judgement, and it is the label red-flag recall is computed against. Same reason, higher stakes. |
| `relevant_doc_ids` | a model | **no — 148 of 150** | Mechanical, so proposing it cost the labeller nothing in independence. The owner then decided not to verify the proposals item by item. |
| `reference_answer` | a model | **no — 148 of 150** | Mechanical for the same reason, and written **only** from the documents proposed beside it. Same decision. |

Two items carry no machine-written value in either mechanical field and are therefore not in the
148: `g-gh-030`, whose documents and reference answer were rewritten by hand, and `g-md-027`, which
has no documents at all (§1.1.2).

**The decision not to verify was the owner's, and it is disclosed rather than papered over.** While
labelling was pending, `proposed_fields` was the right mechanism: it named every field holding a
machine-written candidate, `missing_labels()` reported such a field as still missing, and the loader
refused the file until the marker was cleared. Once the owner decided not to verify those two
fields, that mechanism had exactly two exits and both were wrong. Clearing the marker would have
made the file assert a verification that did not happen. Leaving it set would have blocked the load
forever, which is a gate nobody can pass rather than a gate.

So the provenance was separated from the gate. `unverified_fields` carries the same field names and
the same values and does not gate anything; the 148 records were migrated from one to the other in
one commit; `proposed_fields` stays in the schema, still gating, for the multi-turn set and for any
future re-label. The two are disjoint per record and the lint asserts it, because a field cannot be
both still waiting on the labeller and knowingly unverified.

**What that costs, stated plainly and not softened:**

> `relevant_doc_ids` and `reference_answer` are model-proposed and unverified, so **recall@5 and
> faithfulness are measured against a machine-constructed reference**. Routing accuracy and
> red-flag recall are not: those two labels were written by a person, by hand, blind.

That sentence is not confined to this document. `eval/run.py` writes the counts into
`summary.json` as `unverified_labels`, and `report.md` prints the caveat **in the results table
itself** — in the header of every affected column (`recall@5 (vs. unverified ref)`, both
faithfulness columns), in a line directly under the table, and again in the per-configuration
Retrieval and Judge paragraphs. A footnote is something a reader can finish a table without
reaching, and the numbers in the table are exactly the ones the disclosure is about. Nothing marks
the routing or red-flag columns, because nothing is wrong with them.

The caveat is data-driven, not hard-coded. Hand-verifying the two fields and clearing
`unverified_fields` removes it from the table with no code change — which is the property that
makes it a disclosure rather than boilerplate.

### 1.1.2 The two items with no machine-written reference

Two items shipped with **no** candidate at all, because the corpus does not support an answer to
them. Both said so in `draft_notes` and both were flagged for the owner to re-aim or cut. That was
the honest output of the proposal pass rather than a gap in it: an item with a fabricated source is
worse than an item with none.

`g-gh-030` was re-aimed by hand and now names one corpus note. It carries no `unverified_fields`
marker, so it is one of the two items whose mechanical fields *are* the owner's.

`g-md-027` (calf pain and swelling after a knee replacement) is the sharper of the two, and it was
kept as it was. The presentation it describes is deep vein thrombosis, and **`data/red_flags.yaml`
has no venous-thromboembolism rule either**. The owner labelled it `red_flag: true` and left
`relevant_doc_ids` empty, which is the label rather than a blank: no note in this corpus grounds
the question. `eval/items.py` accepts an empty list beside a written reference answer for exactly
that reason — requiring a non-empty list would have made inventing a source the only way to load
the file, which inverts what the item measures. The consequences are all deliberate:

- it is the one item **recall@5 is not computed over**, so the retrieval denominator is 149;
- it carries **no `phrasing_stratum`**, so it cannot be pooled into the hard-stratum number where
  a miss would read as a paraphrase failure;
- if it comes back a red-flag false negative, the report attributes the miss to absent coverage on
  both sides rather than to the matcher failing on phrasing.

No rule and no note were added to make it pass. Inventing coverage so that an item clears is the
reverse of what the item measures.

**What nobody checked in the machine-written fields.** They are candidates from a model that also
wrote the corpus notes they cite, so the failure mode they are most exposed to is an answer that is
fluent, plausible and slightly beyond what the note actually says. The reference answers
deliberately keep the corpus's hedges — "commonly stated", "guidance describes", the bands rather
than a single number — because a reference answer that sharpened an approximate threshold would
score every correct hedge as unfaithful. That is a property of how they were written, not evidence
that they are right.

### 1.2 How the two judgement labels were written: blind

`expected_route` and `red_flag` were labelled **blind**, outside this repository, and the labels
were merged back in afterwards. The labeller saw:

- the question text, and nothing else.

The labeller did **not** see:

- the item's `category`, so the block could not tell them what the answer should be;
- the item's `phrasing_stratum`, so a red-flag decision could not be read off the drafting intent;
- the item's `id`, which was replaced by an opaque key — ids are block-prefixed (`g-su-`, `g-cc-`),
  so a visible id leaks the category the shuffle was meant to hide;
- the row order, which was shuffled — the set is written in category blocks, so file order is the
  category, and thirty consecutive coding questions establish a rhythm that answers the thirty-first
  before it is read.

**Why blind.** These are the two labels routing accuracy and red-flag recall are computed against,
and they are being written by the same person who commissioned the corpus, the blocks and the
drafting constraints. Every one of those is a strong prior about what the answer ought to be. A
label written with the block header visible is partly a transcription of the block header, and
routing accuracy would then measure how consistently the drafting plan was executed rather than
whether the planner routes correctly. Hiding the four channels above does not remove the prior —
the labeller wrote the questions — but it removes the ones that operate without being noticed.

**What blind labelling produced that a sighted pass would not have.** Three items in the
`multi_dimensional` block came back `mode: single`, and two `symptom_urgency` items came back
`mode: parallel`. Under the drafting plan every `multi_dimensional` item is parallel by
construction. Those five disagreements are the evidence that the labeller was reading questions
rather than blocks, and they are kept: correcting a blind label to match the block it came from
would discard the only signal the procedure exists to produce. They are also a real finding about
the drafts — an item written to have two dimensions does not automatically have two.

**The one thing blindness cost, and the check that now catches it.** A labeller who cannot see the
block also cannot see `data/policy.yaml`, and that file grants six of the seven skills to exactly
one agent each. A route label can therefore name a set of agents that between them hold none of the
skills the question needs — a route the system is structurally unable to answer the question
through. One such conflict was found by hand during the merge and fixed. A check found by hand once
is a check that will be missed the next time, so it is now derived in `eval/validate.py` from the
policy file and the corpus categories and asserted in the lint (§1.4).

**Per item, the labeller worked in this order:**

1. **`red_flag`** — is the question describing a presentation that should produce a seek-care
   instruction? Decided from the question, not from what the matcher does with it.
2. **`expected_route`** — which specialists genuinely own parts of this question?
   `consultation` owns background, lifestyle and classification; `diagnostic` owns urgency and
   symptom grouping; `research` owns guidance and evidence strength. Assign the fewest that can
   answer it.
3. The reasoning was recorded in `draft_notes`, appended after a `||` separator so the authoring
   intent written at drafting time stays legible beside it.
4. `labeled: true`.

Steps 3 and 4 of the original guide — verify `relevant_doc_ids`, verify `reference_answer` — were
not performed, by decision. §1.1.1 is what that means for the numbers.

**For the multi-turn set**, which is still unlabelled: annotate at least one later turn with
`depends_on_turn` (the zero-based index of the turn it refers back to) and `expected_referent`
(what the pronoun or ellipsis means), then set `labeled: true`.

### 1.2.1 The labelled distribution

| | count |
|---|---|
| items | 150 |
| `mode: single` | 121 |
| `mode: parallel` | 29 |
| `red_flag: true` | 28 |
| `red_flag: false` | 122 |

By agent set, as routing accuracy compares them (sorted, so `[a, b]` and `[b, a]` are one row):

| mode | agents | items |
|---|---|---|
| single | `consultation` | 58 |
| single | `research` | 33 |
| single | `diagnostic` | 30 |
| parallel | `diagnostic` + `research` | 16 |
| parallel | `consultation` + `research` | 7 |
| parallel | `consultation` + `diagnostic` | 4 |
| parallel | `consultation` + `diagnostic` + `research` | 2 |

By block:

| block | labelled routes |
|---|---|
| `general_health` | 28 single `consultation`; 2 single `research` |
| `symptom_urgency` | 28 single `diagnostic`; 2 parallel `consultation`+`diagnostic` |
| `condition_coding` | 30 single `consultation` |
| `guideline_evidence` | 30 single `research` |
| `multi_dimensional` | 16 parallel `diagnostic`+`research`; 7 parallel `consultation`+`research`; 2 parallel `consultation`+`diagnostic`; 2 parallel all three; 2 single `diagnostic`; 1 single `research` |

The 28 red-flag items are 23 in `symptom_urgency` and 5 in `multi_dimensional`. All 28 are routed
to a set that includes `diagnostic`, which is the only agent permitted to call `assess_risk` — the
lint asserts this, because a red-flag item routed away from `diagnostic` would have red-flag recall
measured over a turn with no access to the table the label came from.

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
property of the system. Every candidate declares its stratum in its own **`phrasing_stratum`
field** — `hard`, `easy`, or `null` for an item that is not a red-flag candidate — and
`tests/test_eval_drafts.py` asserts that every candidate has one, that no other item does, and
that the two counts are exactly 22 and 5. A stratum defined by negation would silently absorb any
item nobody classified.

**The stratum is a field rather than a marker inside `draft_notes`, and that is not cosmetic.**
`draft_notes` is the field the labeller edits while working — the labelling guide asks for the
reasoning to be recorded there. A dimension a published metric is split on cannot depend on prose
that is about to be rewritten: trimming or rephrasing a note would move an item between strata,
change both per-stratum recall figures, and fail no test, because the item would still be a valid
item. There is now exactly one source, and the lint asserts the retired marker strings never
reappear in the notes.

**Expect the hard stratum to produce misses, and expect them to be the finding.** A red-flag item
the matcher does not catch is a measured false negative, reported with its item id. It is not a
defect in the question, and the fix is not to edit the question toward the pattern.

#### The matcher diagnostic, per stratum

Run over the questions with no model called, no label written and nothing scored:

| stratum | n | matched (raw) | matched (negation guard on) |
|---|---|---|---|
| hard phrasing | 22 | **0** | **0** |
| easy phrasing | 5 | **5** | **5** |

**Re-run against the labelled, frozen file and unchanged.** Both numbers are asserted in
`tests/test_eval_drafts.py` rather than only written here: they move only if a question changes,
and a question changing after the labels are attached to its wording is exactly what must not
happen silently.

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

### 1.4 Checks that run in the lint, not by hand

`eval/validate.py` holds the cross-file checks and `tests/test_eval_drafts.py` runs them, so a
label edit that stops agreeing with another file fails in CI rather than in a sweep. `eval/items.py`
validates a record against its own schema; these check a record against the files it points at.

| check | why it cannot be a one-off script |
|---|---|
| every `relevant_doc_ids` entry names a real note in `data/corpus/` | `doc_id` is the filename stem by contract, and a typo would land in the results as a retrieval miss rather than as the typo it is |
| the labelled route reaches at least one exclusive skill the item needs | a labeller does not have `data/policy.yaml` open, so nothing else stops a label encoding a route the system is forbidden to take |
| every red-flag item is routed somewhere holding `assess_risk` | only `diagnostic` holds it, and it is the skill that consults the rule table the label came from |
| the two provenance markers are disjoint | an item cannot be both verified and not |
| the per-stratum matcher hits are 0/22 and 5/5 | both are published in §1.3, and both move only if a question changes |
| the strata hold exactly 22 and 5 | they are the denominators of the two published recall figures |
| the unverified counts are 148 in both mechanical fields | that number is printed in the results table, so the disclosure must not itself be unverified |
| `g-md-027` is the only item with no relevant documents | it is the one item recall@5 is not computed over |

The skill-grant check is derived, not restated: which skills are exclusive and to whom is read from
`data/policy.yaml`, and which skill a labelled note needs is read from that note's corpus category.
A copy of the grants in the test would be a second source that drifts from the file actually
governing the agents.

It reports a conflict only where the labelled agents hold **none** of the skills the item needs. A
question can need two exclusive skills and be answerable through either, so a route reaching one of
them is a defensible label; a route reaching none of them is the failure. `search_knowledge` is
granted to every agent and filters nothing, so no corpus note is ever strictly unreachable — what a
conflict means is that the **category-filtered specialist skill** for the labelled note is out of
the route's reach and the note can only be found by an unfiltered search.

**Four conflicts stand, are accepted, and are reported rather than relabelled**, all in
`general_health`:

| item | labelled route | skill it needs, and who holds it |
|---|---|---|
| `g-gh-001` | single `consultation` | `find_guideline` (`research`) |
| `g-gh-017` | single `research` | `recommend_lifestyle` (`consultation`) |
| `g-gh-026` | single `research` | `recommend_lifestyle` (`consultation`) |
| `g-gh-029` | single `consultation` | `find_guideline` (`research`) |

In all four the conflict is between a **hand-written** route and a **machine-written, unverified**
document list, so which of the two is wrong is not something the lint can decide and is not
something it guesses at. The set is asserted exactly, so a *new* conflict — the kind that would
encode a route the system is structurally forbidden to take — fails the lint.

### 1.5 The freeze

Checkpoint B is closed. The golden set is labelled and frozen; the multi-turn set is not.

| file | items | sha256 |
|---|---|---|
| `eval/data/golden.jsonl` | 150 questions, labelled | `b7b7781f135debd1059ad7ae0196d717ec3e7afc3d22c6483e0a59d99874d508` |
| `eval/data/multiturn.jsonl` | 30 conversations, **still an unlabelled draft** | `a675635212245b1b442bac09fdd1e78ac80f25a891816ede8e09c64e938de2c2` |

The digests are recorded so that a published number can be tied to the exact file it was computed
over. `summary.json` records the commit; the commit records the file; the digest is what lets a
reviewer check that the file at that commit is the one this document describes. Both digests are
asserted against the files in `tests/test_eval_drafts.py`, in both directions — the file must hash
to the value written here, and the value written here must appear in this document — because a
stale digest in a frozen record is worse than no digest.

**What is frozen:**

- 150 items, 30 in each of five blocks; ids are block-prefixed and dropped ids are never reused.
- `expected_route` and `red_flag` on all 150, hand-labelled blind (§1.2). 121 single, 29 parallel,
  28 red-flag.
- `relevant_doc_ids` and `reference_answer` machine-written on 148 of 150 and **unverified**
  (§1.1.1), recorded per record in `unverified_fields`.
- 22 hard-phrasing and 5 easy-phrasing red-flag items, matcher hits 0/22 and 5/5 under both
  negation policies (§1.3).
- `g-md-027` labelled `red_flag: true` with no relevant documents and no stratum (§1.1.2).

**What changing any of it costs.** Every count above is a denominator of a published number, and
every one is asserted in `tests/test_eval_drafts.py` as an exact value rather than a floor.
Changing one is a deliberate act that updates the lint, this section and the digest in the same
commit. Editing a question is a larger act than that: the labels are attached to the current
wording, so a rewritten question carries a label written about a different question.

**Not frozen, and still to do:** `eval/data/multiturn.jsonl` is unlabelled and `load_multiturn`
still refuses it. No sweep has been run against either file, so every results number in §6 and in
the README reads `not measured`.

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

**`not measured`.** The golden set is labelled and frozen (§1.5) but no sweep has been run. This
section is filled in from `eval/results/published/summary.json` when one has.

The column headers below are the ones `report.md` will carry. Two of them say what they are
measured against, in the header rather than in a footnote, because `relevant_doc_ids` and
`reference_answer` are model-proposed and unverified (§1.1.1) and the columns computed from them
are not the same kind of number as the two beside them.

| configuration | routing acc | recall@5 (vs. unverified ref) | faithfulness (vs. unverified ref) | red-flag recall | p90 latency | tokens | cost |
|---|---|---|---|---|---|---|---|
| `baseline_llm` | n/a | n/a | n/a | not measured | not measured | not measured | not measured |
| `single_agent_rag` | n/a | not measured | not measured | not measured | not measured | not measured | not measured |
| `full` | not measured | not measured | not measured | not measured | not measured | not measured | not measured |
| `full_no_memory` | not measured | not measured | not measured | not measured | not measured | not measured | not measured |

**recall@5 and faithfulness are measured against a machine-constructed reference; routing accuracy
and red-flag recall are not.** `relevant_doc_ids` and `reference_answer` were written by a model on
148 of the 150 items and no person verified them. `expected_route` and `red_flag` were written by a
person, by hand, blind. The two pairs of numbers are not equally trustworthy and the table says so
in the header of every affected column.

---

## 7. Threats to validity

These are stated before any number exists, so that they cannot be chosen after seeing the results.

**The golden set is small.** 150 items, 30 per block. A difference of a few percentage points
between two configurations on 30 red-flag items is not a difference; the confidence interval on a
proportion at n=30 is wide enough to swallow most of what the ablation is looking for. Read the
counts, not the third decimal place.

**`relevant_doc_ids` would not survive a second opinion.** Whether a note that merely mentions the
topic "answers" the question is a judgement call, and a second labeller would disagree on some of
them — except that on this set there was no first labeller for that field either. See below.

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

**The reference answers and the relevant-document lists were written by a model, against a corpus
the same model family wrote, and nobody checked them.** This is the largest threat in this list and
it is a decision rather than an oversight (§1.1.1). `relevant_doc_ids` and `reference_answer` are
model-proposed and unverified on 148 of 150 items, so **recall@5 and faithfulness are measured
against a machine-constructed reference** — a reference that shares an author with the system being
measured, which is the definition of the circularity the whole checkpoint exists to avoid, admitted
in the one place it was not avoided. Read those two columns as "does the system agree with the
model that wrote the corpus", not as "does the system retrieve the right documents".

Routing accuracy and red-flag recall carry no such threat: `expected_route` and `red_flag` were
hand-labelled on all 150 items, blind to the block and the stratum (§1.2), and nothing ever proposed
a candidate for either.

**One labeller, and blind labelling bounds only part of what that costs.** Every label is a single
person's judgement and no inter-annotator agreement is measured, because there is one annotator.
Blindness removes the channels that leak the drafting plan into the label — block, id, stratum, file
order — but not the prior held by the person who wrote the questions in the first place.

**Mock-provider numbers are never reported.** The test suite runs entirely against `MockProvider`
with synthetic token counts. `eval/run.py` refuses to run against it.

**Latency depends on the endpoint, not only on the architecture.** p90 on a rate-limited key
measures the queue. The sweep runs items sequentially for that reason, and the run's provider, model
and date are recorded so a latency number is at least interpretable.
