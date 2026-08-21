# Design decisions

> **Not medical advice.** This is an educational software project. It does not diagnose, treat, or
> provide clinical guidance, and it must not be used for real medical decisions. No patient data of
> any kind may be used with it.

One section per non-obvious decision: what was chosen, what was rejected, and why. Sections are
added as each phase lands, so this file grows with the build rather than being written at the end.

---

## Phase 1 — substrate

### The trace schema is defined before anything that emits events

**Chosen.** A versioned, Pydantic-validated event schema in `consilium/trace.py`, written first,
with every metric in `docs/EVALUATION.md` derived from those events and from nothing else.

**Rejected.** Adding logging as each component was built and deriving metrics from whatever
happened to be recorded.

**Why.** Metrics defined after the fact get quietly reshaped to fit the artifacts that exist. Fixing
the schema first inverts that: if a metric cannot be computed from these events, that is a finding
to report, not a gap to paper over with an estimate. Two metrics already forced schema changes
before a line of measurement code existed — see the next two sections.

### The tracer is turn-scoped and injected, not owned by the ReAct loop

**Chosen.** One `Tracer` per user turn, passed into the planner, router, agents, loop, skills,
safety layer and synthesizer.

**Rejected.** A tracer created and owned by the ReAct loop, which is where most events originate.

**Why.** The planner call and the synthesizer call are exactly the overhead the multi-agent
architecture adds over a single LLM call. A loop-owned tracer would miss both, and the ablation
table would then compare a fully-costed baseline against an under-costed multi-agent system. The
cost of the architecture is the thing being measured, so the meter has to sit outside the part being
measured.

### Red-flag recall is measured on the delivered answer, not on the repair event

**Chosen.** Three fields on the `turn` event — `escalation_present_pre_repair`,
`escalation_present_post_repair`, `repair_applied` — with red-flag recall defined as the share of
`red_flag: true` items where `escalation_present_post_repair` is true, reported with the raw
false-negative count.

**Rejected.** Counting `safety` repair events, which is the obvious reading of "did the escalation
banner fire".

**Why.** `OutputRepair` prepends the banner only when the answer lacks a seek-care instruction. An
answer that already told the user to seek emergency care is a *correct* handling of the red flag and
emits no repair event at all, so counting repairs would score the system's best behaviour as a false
negative. Running the same detector twice — against the model's own answer and against the delivered
answer — also separates two claims worth making separately: how often the model handled it unaided,
and how often the guard is what saved it.

### The escalation banner is decided on the input, and streamed first

**Chosen.** On the SSE path, red-flag detection runs against the user's question before generation
and the banner is emitted as the first event of the stream. Output-side violations that only surface
in the generated text are repaired after the stream and marked `post_stream: true` in the `safety`
event. `POST /v1/ask` and the CLI, which do not stream, repair before delivery as specified.

**Rejected.** Buffering the whole answer, validating it, and only then streaming — which removes the
reason to stream at all.

**Why.** A banner cannot be prepended to text the user has already read. The trigger for the banner
is a pattern match on the user's input, so it can be decided before the first token, which makes the
correct-by-construction path also the fastest one. The residual case — a red flag that appears only
in the model's own answer — is real and is handled late; `docs/SAFETY.md` states plainly which path
repairs after delivery rather than leaving a reader to infer it.

### Two protocol seams, each with a second real implementation

**Chosen.** `Embedder` (`BgeEmbedder` / `HashEmbedder`) and `VectorStore` (`ChromaStore` /
`NumpyStore`). `pytest -m "not network"` runs entirely on the second implementation of each.

**Rejected.** Monkeypatching `sentence_transformers` and `chromadb` in `conftest.py` to make the
suite run without downloads.

**Why.** A mock asserts that the code called a library in a particular way; a second implementation
asserts that the code works against the interface. The second is what a swap to a different vector
store actually needs, so the same work that makes the suite offline also makes "why Chroma rather
than Milvus or pgvector" a question with code behind the answer instead of a preference.

`HashEmbedder` is a lexical embedder wearing a dense interface — it measures weighted token overlap,
not meaning. It exists to make the pipeline testable end to end offline and must never be the source
of a retrieval quality number; every measured retrieval result comes from `BgeEmbedder`.

### `blake2b` with an explicit key, not the built-in `hash()`

**Chosen.** Keyed `hashlib.blake2b` for feature hashing in `HashEmbedder`.

**Rejected.** Python's built-in `hash()`, which is the shorter code.

**Why.** `hash()` is salted per process for strings. A persisted index would become unreadable by
the next process, and "deterministic" would be false in the way that is hardest to notice: every
test would pass, because every test would build and query the index in one process.

### Tokenizer: code-like tokens survive, hyphenated words do not

**Chosen.** Lowercase, split on non-alphanumerics, keep a `.`/`-`-joined token whole **iff it
contains a digit**, drop stopwords except negation words, no stemming. `E11.9`, `I10`, `SGLT-2` and
`COVID-19` survive as single tokens; `end-stage` becomes `end` + `stage`.

**Rejected.** A default whitespace-and-punctuation tokenizer, and stemming.

**Why.** The hybrid-retrieval claim is that lexical retrieval carries the coding and guideline
categories because ICD-10 codes and drug-class names are rare tokens that dense retrievers embed
poorly. That claim is only testable if the tokenizer preserves those tokens; a tokenizer that splits
`E11.9` into `e`/`11`/`9` would make the hybrid result a measurement of a bug. Hyphenated *words*
are split in the other direction so that a query for "stage" matches "end-stage". Stemming was
rejected because it collapses distinctions across categories (`coding`/`code`) as readily as it
merges useful variants, and an unstemmed index keeps a surprising retrieval result explainable.

Negation words (`no`, `not`, `nor`, `against`, `without`) are kept even though standard English
stopword lists remove them. BM25 does not model negation either way, but discarding the tokens
guarantees "no chest pain" and "chest pain" are indistinguishable, and that is not a distinction to
throw away in this corpus.

### `RunConfig` is defined in Phase 1, not Phase 8

**Chosen.** The ablation toggles (`retrieval`, `router`, `memory`, `max_tool_calls`) live in
`consilium/config.py` from the start, so the router and the ReAct loop accept them natively when
they are written in Phases 4 and 5.

**Rejected.** Building the router and loop first and threading the toggles through in Phase 8.

**Why.** A reduced mode has to be a flag rather than a code edit, or the ablation is not
reproducible at a commit. Retrofitting the flags after the fact tends to produce a `if eval_mode:`
branch near the top of each component, which is the shape that makes a reviewer ask whether the
measured system is the shipped system.

### Session state is never a process-wide singleton

**Chosen.** (Implemented in Phase 6; recorded here because it constrains everything built before
it.) `WorkingMemory` obtained from a `MemoryStore` keyed by `session_id` and injected for the
duration of a turn.

**Rejected.** A module-level singleton holding the conversation, which is the shortest way to let
several agents in one turn see the same history.

**Why.** The requirement is that agents *within one turn* share history. A singleton achieves that
by making the history global, which means two concurrent API users share a conversation and the
module cannot be tested without monkeypatching. Passing one instance to the agents of one turn
satisfies the actual requirement with no global state.

### `rank-bm25` is pinned exactly

**Chosen.** `rank-bm25==0.2.2`.

**Rejected.** A floor constraint like `>=0.2.2,<0.3`.

**Why.** The package has had no release since 2022. A floor on an unmaintained package buys nothing
— there is no stream of compatible patches to receive — while an exact pin makes it obvious in the
diff if it is ever replaced, which is the likelier future for a dependency in that state. Every other
dependency is capped at the next major so that a floor-only specification cannot pull a breaking
release between the commit that produced the published numbers and a later checkout; `uv.lock` is
committed so the exact resolved set is recoverable.

### `numpy` is a core dependency

Not a substitution: the offline rule in the brief specifies "an in-memory numpy implementation" of
the `VectorStore` protocol, so `numpy` is required to build the component the specification names.
It would arrive transitively with `chromadb` and `sentence-transformers` in any case, but those are
optional and CI does not install them.

## Phase 2 — data and retrieval

### The red-flag matcher guards negation, narrowly

**Chosen.** An explicit negation cue — *no, not, never, without, denies, denied, deny, negative,
none* — within three tokens before a matched phrase suppresses that match, with a sentence boundary
ending the window. "No chest pain" and "denies chest pain" do not escalate. "Not sure if this is
chest pain" and "I have chest pain. No fever." still do. Hedges are deliberately not cues.

**Rejected — ignore negation entirely.** The argument for it is a genuine asymmetry: a false
negative on an emergency symptom is the worst error this system can make, and a false positive
appears to cost only one unnecessary banner. That reasoning is right in direction and wrong in
magnitude. A banner on "no chest pain" does not cost one banner; it costs the credibility of every
subsequent banner. Alarm fatigue is the standard failure mode of clinical alerting systems, and a
guard rail that fires on inputs a reader can see are negated trains people to skip it — including on
the occasions it is right. "I ignored negation for safety" describes a decision not to think about
the failure mode rather than a considered trade.

**Rejected — a real negation parser.** Dependency parsing or a negation-scope model (NegEx and its
descendants) would handle "chest pain is not something I have ever experienced". It also introduces
a model whose failures are opaque, a dependency the brief does not list, and a component that cannot
be reviewed by reading it. The narrow rule fits on a screen, and its boundaries are pinned by tests.

**Contractions are normalized, not enumerated.** The first version of the cue list held only
uncontracted forms, which meant it missed its own main case: "I don't have chest pain" is probably
the most common negated phrasing in real user input, and it escalated. The fix is in the tokenizer
rather than the cue list — any token matching `n't` folds to `not` before matching, so one rule
covers *don't, doesn't, didn't, haven't, hasn't, hadn't, isn't, aren't, wasn't, weren't, can't,
couldn't, wouldn't, shouldn't, won't, mustn't, needn't* and anything else formed the same way. Both
apostrophe characters are accepted, since text from a phone keyboard carries U+2019 rather than
U+0027. A cue list would have been wrong the first time someone wrote a form nobody had thought of;
the existing `not` cue and the existing three-token window then apply unchanged.

Apostrophe-free forms (*dont, doesnt, havent, isnt*, …) are included too, with two deliberate
exclusions: **`cant` and `wont` are real English words**, and suppressing an emergency match on a
legitimate word is a worse error than missing an informally typed negation.

**The cue list mixes two vocabularies, and should say so.** *denies, denied, deny* are clinician
register — they appear in notes written *about* a patient ("patient denies chest pain"), not in what
a patient types. They are kept because the system may be handed clinician-authored text and they
cost nothing, but they are not what makes the guard work on real user input. *no*, *not* and the
contracted forms are. A cue list that silently mixes registers invites the reader to assume it was
assembled from one source and validated against it, and it was not.

### Inflected forms are enumerated, not derived

Writing the contraction tests exposed a second recall gap: the matcher missed "chest pains", because
the word boundary after `pain` fails against the plural.

**Rejected — an optional trailing `s` in the compiled regex.** This was the first fix, and it was
wrong in a way worth recording. It covers `pain`/`pains` and silently fails on every other kind of
inflection: *vomiting / vomited / vomit*, *throwing up / threw up*, *face drooping / drooped /
droops*, *slurred / slurring*, *turning blue / turned blue*. Patching the one instance that a test
happened to surface leaves the general defect in place and creates the impression it was handled.

**Rejected — a stemmer.** Porter or Snowball stemming would generalize correctly across regular
morphology. It also puts the matching behaviour inside a library, so "which inputs escalate?" stops
being answerable by reading the data file, and the safety-critical behaviour of the system becomes
dependent on a component nobody on the project reviewed. It would also over-match: stemming
`stroke` and `stroking` to a common root is not wanted here.

**Chosen — an exhaustive manual audit.** The table is small enough to check completely: all **116
patterns across 14 rules** were audited against their plural, past-tense, third-person and gerund
forms, and the variants a person would plausibly type were added as explicit patterns. The matcher
applies no morphology of its own, so what matches is exactly what a reviewer reads in
`data/red_flags.yaml`.

| | before | after | added |
|---|---|---|---|
| patterns | 116 | 195 | +79 |
| rules changed | | | 13 of 14 |

Only `pregnancy_emergency` was unchanged; its four patterns have no plausible inflection. The
largest additions were `stroke_symptoms` (+12), `severe_breathing_difficulty` (+10) and
`anaphylaxis` (+9), all of which are phrased around verbs. A test asserts the negative case too —
`mottled skins` does not match, because it was not audited in — which is what makes "the matcher
applies no morphology" a checked claim rather than a comment.

This is defensible in a way the regex was not: "I audited all 116 patterns against inflected forms
and added 79" is verifiable by reading the file. "I added an optional `s`" is a patch that fails on
the next irregular verb.

**How the choice is settled.** Not by the argument above. Every match records whether a cue
suppressed it, and the `turn` trace event carries `red_flag_matched_raw`, `red_flag_matched` and
`red_flag_negation_suppressed`, so one evaluation run yields red-flag recall and false-positive rate
under *both* policies. `RedFlagTable(..., negation_guard=False)` is a constructor switch, not a code
edit, so the ablation is reproducible.

| policy | red-flag recall | false-negative count | false-positive rate |
|---|---|---|---|
| raw (no negation guard) | not measured | not measured | not measured |
| guarded (default) | not measured | not measured | not measured |

**The rule that decides it, fixed in advance so the result cannot be rationalised after the fact:
if the guard costs any recall at all on the labelled set, the default reverts to raw matching**, and
this section reports both numbers and says so. A guard that trades a single missed emergency for a
tidier false-positive rate is not a trade this project is willing to make; the point of measuring
both is that the decision rests on the data rather than on which way the prose leans.

### The red-flag matcher is built in Phase 2, not Phase 7

The build order puts the safety layer in Phase 7, and `consilium/safety/red_flags.py` arrives in
Phase 2 with the data file it loads. A rule table with no loader is an unverified rule table: the
`doc_id` cross-references, the urgency tiers and the pattern list are all assertions until something
reads them. Building the loader alongside the data means the corpus notes and the rules are checked
against each other in the same commit that introduces both. Phase 7 adds `policy.yaml`, the
validator and `OutputRepair` on top of this, unchanged.

Both the `assess_risk` skill and `OutputRepair` share this one implementation. Two matchers would
let a symptom escalate through one path and not the other, which surfaces as an unexplained gap
between the safety trigger rate and red-flag recall — a bug that is very hard to find from the
metrics alone.

### Hybrid retrieval, not dense-only

**Chosen.** BM25 and a dense index over the same chunks, fused by reciprocal rank.

**Rejected.** Dense-only retrieval, which is the default shape of a RAG system and half the code.

**Why.** The corpus is deliberately two kinds of text. A `lifestyle` note is prose, and "what should
I eat if my blood pressure is high" has almost no lexical overlap with a note that says
"hypertension" and "sodium reduction" throughout — that query needs an embedder. A `coding` note
turns on strings like `I10`, `E11.9` and `J44.1`, and those are exactly the tokens a dense model
handles worst: they are rare, they carry no morphology, and a 384-dimensional sentence embedding
puts `E11.9` and `E10.9` in nearly the same place while the difference between them is the whole
answer. Running one retriever means choosing which of the two question types to be bad at.

The claim is falsifiable and the harness is built to falsify it: `retrieval` trace events record the
fused ranking for every turn, so recall@5 can be reported per golden-set category. If lexical
retrieval does not in fact carry `condition_coding`, that shows up as a number here rather than as a
paragraph.

### RRF over weighted score fusion

**Chosen.** Reciprocal Rank Fusion, `sum(1 / (60 + rank))` over each retriever's ranking.

**Rejected.** Normalizing both retrievers' scores into `[0, 1]` and taking a weighted sum —
`alpha * dense + (1 - alpha) * lexical`, the other standard answer.

**Why.** BM25 scores are unbounded sums of IDF terms and cosine similarities live in `[-1, 1]`.
They are not comparable, and no normalization makes them so. Per-query min-max scaling, the usual
fix, has a specific failure: it maps each retriever's top hit to exactly 1.0 whether that hit was an
excellent match or the least bad of a weak field. That is precisely the case where the two
retrievers should be allowed to disagree — one of them found nothing good — and scaling erases the
distinction before fusion sees it.

Weighted fusion also introduces `alpha`, and `alpha` has to come from somewhere. Tuning it on the
golden set that is then used to report recall@5 is fitting the metric; picking it by eye is an
unmeasured number in the retrieval path. RRF has no such parameter. Its `k = 60` is the value from
the original paper and is deliberately **not** tuned here, for the same reason.

The cost is real and is worth stating: RRF throws away magnitude, so a retriever that is confident
and a retriever that is guessing contribute identically at the same rank. The design accepts that in
exchange for having no free parameter in the fusion step.

### Chroma, not pgvector or Milvus

**Chosen.** Chroma as the persistent vector store, behind a `VectorStore` protocol whose second
implementation, `NumpyStore`, is an in-memory brute-force cosine scan.

**Rejected — pgvector.** The better choice for a system that already runs Postgres, because it puts
the vectors next to the relational data and gets transactions, backup and access control for free.
This project runs no Postgres. Adding one would make the quickstart "install and configure a
database server" for a corpus of 78 notes, and the operational surface would exceed the system being
demonstrated.

**Rejected — Milvus.** Built for billion-scale approximate search with a distributed architecture to
match. At 312 chunks the entire index is a 312 × 384 float32 matrix — 479 KB — and an approximate
index over it would add a recall parameter to tune and a second source of retrieval error to
disentangle from the one being measured. Choosing infrastructure sized for a workload the project
does not have is the specific failure a reviewer is looking for.

**Why Chroma.** It is embedded, so it needs no server; it persists, so an ingest survives the
process; and it filters on metadata, which the category filter requires. That is the whole
requirement list.

**What makes this answer defensible rather than a preference** is that the protocol has a second
implementation that is actually used. `tests/test_vector_store_contract.py` runs one set of
assertions against both, so "we could swap the store" is a claim with code behind it — including the
detail that catches real swaps, which is that Chroma reports a *distance* and `NumpyStore` reports a
*similarity*, and the adapter is what makes a score mean one thing to a caller.

### The store is `configuration={"hnsw": {"space": "cosine"}}` and `embedding_function=None`

Chroma otherwise attaches a default ONNX embedding model and embeds text for you on `add`. This
project supplies its own vectors through the `Embedder` seam, so an implicit second embedder would
download a model and — worse — silently embed documents with one model while queries arrived from
another. Passing `None` makes that impossible rather than merely unlikely.

### Chunks are divided evenly, not filled greedily

**Chosen.** Take the fewest chunks the body needs, then place each break at the point that divides
the *remaining* text evenly among the *remaining* chunks, snapped to the best boundary available
inside the size band.

**Rejected.** Accumulate paragraphs until the next one would overflow 1,000 characters, then break —
which is what almost every chunking implementation does.

**Why.** The greedy version was written first and measured on this corpus. It left final chunks as
short as 156 characters. That is not cosmetic: BM25 length normalization actively favours short
documents, so a runt is *over*-retrieved relative to what it contains, and once retrieved it takes
one of the five slots the model sees while carrying a fragment of a sentence. Recomputing the ideal
break position after every break is what stops a break that had to move from accumulating into a
short tail.

Measured on the current corpus: 78 notes, 312 chunks, exactly four per note, mean 846 characters,
shortest 461, none above the 1,000 ceiling, and 74% inside the 800–1,000 band. The chunks below the
band are the last chunk of a note, and they are a consequence of the two bands the brief fixes
rather than of the algorithm: a 2,725-character body cannot be divided into four pieces that are all
above 800.

**Where "split on paragraph boundaries where possible" binds, and where it does not.** With
3,474-character bodies and 898 characters of new content per chunk, most breaks have no paragraph
boundary anywhere inside the feasible window, so they land on sentence boundaries instead. The
preference between boundary kinds is deliberately allowed to operate *only* over positions that are
already acceptable on size — the first version let it range over the whole feasible window, and
"prefer a paragraph boundary" duly pulled a break 288 characters off target and produced a
393-character opening chunk. A preference that can override the band is not a preference.

### One tokenizer, shared by BM25 and the offline embedder

The tokenizer decision itself is recorded under Phase 1. What Phase 2 adds is that BM25 uses *that*
function rather than a second one written next to the index. A lexical index and a hash embedder
that disagreed about whether `E11.9` is one token would produce two different vocabularies over one
corpus, and the resulting retrieval difference would be indistinguishable from a real dense-versus-
lexical effect — which is the exact comparison this project exists to measure.

One consequence is worth stating because it is a limitation rather than a feature: the rule that
keeps `.`/`-` joined tokens whole when they contain a digit also keeps `ICD-10-CM` whole, so a query
for `ICD-10` does not lexically match a note that only says `ICD-10-CM`. The corpus happens to use
both forms — 24 occurrences of the bare form and 33 of the modified one — so no note is unreachable
today. It remains a real edge, and it is the dense half of the hybrid that covers it.

### A lexical hit is token overlap, not a positive score

**Chosen.** BM25 returns a chunk only if the chunk shares at least one token with the query.

**Rejected.** Returning chunks whose BM25 score is greater than zero, which reads as the same rule.

**Why.** Okapi BM25 assigns a *negative* IDF to a term carried by more than half the corpus, and
`rank-bm25` floors it at a fraction of the average IDF, which is itself negative in that case. So
`score > 0` silently drops genuine matches whenever a query term is common — which is unlikely
across 312 chunks and entirely likely inside a narrow category filter or a small test corpus.
Testing for overlap says what is meant and does not depend on the sign of a smoothing term.

Chunks that match nothing are excluded rather than ranked last because RRF cannot tell a weak hit
from a non-hit once it has been given a rank: rank 20 of a ranking that found nothing scores the
same `1/80` as rank 20 of a ranking that found twenty good ones.

### The category filter narrows each retriever, not the fused result

**Chosen.** Both retrievers receive the category and each returns 20 in-category candidates.

**Rejected.** Fusing unfiltered results and filtering afterwards, which is one line.

**Why.** Filtering afterwards spends both retrievers' 20 candidates on documents that are then
discarded, so `lookup_disease_code` — filtered to `coding`, 76 of 312 chunks — would draw from a far
shallower effective pool than `search_knowledge` does. The two skills' recall@5 numbers would then
not be comparable, and the difference would look like a property of the categories.

BM25's *scores*, unlike its candidate set, are computed before filtering. IDF is a corpus-level
statistic: how rare `I10` is, is a fact about the corpus, not about the subset a skill happens to be
asking within. Rebuilding the index per category would make the same chunk score differently
depending on which skill asked.

### Retrieval depths are fixed at construction, not per call

`returned_k` is a property of the retriever and is deliberately not an argument to `search()`.
Recall@5 is a headline number, and a retrieval depth that varies per call makes its denominator vary
too — invisibly, because the results table shows one column either way.

The frozen values are 20 candidates per retriever, fused and deduplicated to a top-10 recorded in the
trace, and a top-5 returned to the model. The trace carries ranks 6–10 that the model never sees,
because MRR@10 is uncomputable without them and truncating the artifact to what the model saw is the
standard way retrieval metrics quietly stop being measurable.

---

## Phase 3 — skills and the registry

### Tool schemas are derived from the argument models, never hand-written

**Chosen.** `SkillRegistry.to_tool_schemas()` calls `model_json_schema()` on each skill's Pydantic
argument model, strips the class-name `title`, and wraps the result in the OpenAI function envelope.

**Rejected.** A JSON tool definition per skill, written by hand next to the implementation — which
is what most tutorials show, and what the reference implementation does.

**Why.** Two copies of a schema drift, and this pair drifts in the direction that is hardest to
diagnose. The model reads the hand-written copy and the validator enforces the generated one, so a
field that gained a default in the model but not in the JSON shows up as the model passing arguments
the system rejects — a failure that reads as a model problem and is not one. Deriving the schema
makes that class of bug unrepresentable, and `test_tool_schema_carries_no_hand_written_json` pins it
by comparing the emitted parameters to what Pydantic generates.

The one transformation applied is dropping the top-level `title`. It is the argument model's class
name (`AssessRiskArgs`), it means nothing to the model, and it is paid for in prompt tokens on every
turn that offers the tool. Field-level titles are left alone: they carry the field names.

### Skills are synchronous functions and the registry bridges to async

**Chosen.** Every skill is `def`, not `async def`. `SkillRegistry.run()` is synchronous;
`SkillRegistry.execute()` awaits it via `asyncio.to_thread`.

**Rejected.** (a) Declaring the skills `async def` for uniformity with the loop that calls them.
(b) Calling them directly from the coroutine, without a thread hop.

**Why.** Every skill is local CPU work: BM25 scoring over a few hundred chunks, one numpy matmul, a
regex pass over a rule table. `async def` over a body that never awaits is decoration — it makes the
function look concurrent while it still holds the event loop for its whole duration, and it does not
make `sentence-transformers` yield either.

Calling straight from the coroutine is the version that actually breaks something. It is invisible
on the single-agent path, where nothing else wants the loop, and it destroys the parallel one: three
workers dispatched by `asyncio.gather` would take turns instead of overlapping, so the
parallel-versus-single latency comparison in `docs/EVALUATION.md` would be measuring the harness
rather than the architecture. `test_execute_does_not_block_the_event_loop` asserts the overlap
directly rather than trusting the argument.

### A skill never raises into the loop

**Chosen.** Four failure modes — unknown skill name, arguments that fail validation, a
retrieval-backed skill invoked with retrieval switched off, and an exception inside the skill — all
return `SkillResult(ok=False, error=...)` and all emit a `tool_call` event with `ok=False`. The
handling is in the registry, once, not in seven implementations.

**Rejected.** Letting exceptions propagate to the agent and handling them there.

**Why.** A tool call that failed is data; a tool call that killed the turn is an outage. The metrics
in `docs/EVALUATION.md` count tool calls, their success rate and their latency, and an agent that
dies on a malformed tool call has a failure mode that none of those numbers can see. The propagating
version also fails in the worst place — mid-turn, after the tokens for the tool call have already
been paid for and before the user has anything.

`ValidationError` is flattened to one line naming the field and what was wrong with it, because the
observation goes back to the model, which then gets one more attempt inside its tool budget.
Pydantic's default multi-line rendering repeats the class name on every error and buries the field.

### `assess_risk` reads the rule table first and retrieval second

**Chosen.** The urgency tier and the action text come from `data/red_flags.yaml` through the same
`RedFlagTable` that `OutputRepair` uses. Retrieval only adds the corpus note that explains the
presentation, and it is filtered to `red_flag` only when a rule actually fired.

**Rejected.** (a) Retrieving `red_flag` notes and letting the model infer the tier from them.
(b) A second matcher inside the skill.

**Why.** A model that has to summarize retrieved prose into a tier can summarize it wrongly, and
would do so nondeterministically. A table lookup returns the same tier for the same input every
time, which is what makes red-flag recall a property of the system rather than of the sampling
temperature. Sharing the matcher with `OutputRepair` matters for the same reason in the other
direction: two implementations would show up as an unexplained gap between the safety trigger rate
and red-flag recall, and attributing that gap afterwards would be very hard.

The filter is conditional because filtering in the no-match case would search eleven emergency notes
for a description matching none of them and return the closest of the eleven — which reads, in the
answer, as evidence for an escalation that did not happen.

A non-match sets `action` to a fixed sentence saying the table covers a fixed list and that this is
not a clearance. Leaving `action` empty and letting the model phrase it would put the most
consequential sentence in the output under sampling.

### `analyze_symptoms` returns documents, and unmapped input says so

**Chosen.** The skill groups matched terms by body system, reports `single-system`, `multi-system`
or `unrecognized`, and returns `condition` notes as things to read. A term belonging to two systems
is reported under both.

**Rejected.** (a) A ranked differential diagnosis. (b) Filing unrecognized terms under
`constitutional`. (c) Forcing each term to a single owning system.

**Why.** A ranked differential is a diagnosis; `policy.yaml` forbids one and the project makes no
claim to be able to produce one. Filing unrecognized input under the whole-body bucket would invent
a finding out of a parse failure, so `unrecognized` with an empty grouping is the honest output and
is distinguishable from "no systems involved". And forcing "chest tightness" to be cardiovascular
*or* respiratory would encode a diagnostic judgement in a lookup table — reporting both keeps the
skill descriptive, which is the only register it is entitled to.

### `lookup_disease_code` extracts codes with a regex over retrieved text

**Chosen.** Retrieve from the `coding` category, then run an ICD-10 pattern over what came back and
report the codes separately from the prose, each with the `doc_id` it came from and its surrounding
context.

**Rejected.** A condition → code lookup table inside the skill.

**Why.** A lookup table would be a second copy of the corpus that could drift from it, and the
corpus is the thing under measurement — a code the table knew and the corpus did not would inflate
the apparent answer quality without any retrieval having succeeded. Extraction from retrieved text
has the property a reviewer can check: the codes reported are exactly the codes in the notes that
were retrieved, and if retrieval failed, the code list is empty rather than confidently wrong.

Deduplication is on `(code, doc_id)` rather than on `code`, because the same code in the chapter map
and in the per-condition note is two pieces of evidence with different weight, and collapsing them
hides which note to cite.

### `deep_research` is corpus-only, and that is a scoping decision

**Chosen.** No web search. The skill decomposes the question into sub-queries, retrieves for each
over the corpus, and reports the evidence with its sources.

**Rejected.** Adding a web-search tool to the research skill.

**Why.** Three separate costs, none of which this project can defend. It adds a network dependency
to a repository whose test rule is that `pytest -m "not network"` passes with no network and no key.
It forces a search-provider choice nobody asked for. And it creates a source-quality problem: a page
that ranks well is not a clinical authority, and there is no defensible filter this project could
apply. The seam for pointing the system at a larger *real* corpus is `scripts/ingest_medquad.py`,
which loads a public-domain dataset offline. Stated here as a scope boundary, so it is a decision
rather than a missing feature.

### The sub-queries come from the model, not from a second LLM call

**Chosen.** `deep_research` takes `sub_queries` as a tool argument. The agent writes them in the
call it was already making. When it supplies none, a fixed three-aspect decomposition is appended to
the question.

**Rejected.** Having the skill issue its own LLM call to decompose the question.

**Why.** The trace schema settles it. `llm_call.caller` is pattern-validated against `planner`,
`synthesizer`, `forced_answer` and `agent:<name>`; there is no slot for a skill. A skill-issued call
would either be untraced — making tokens-per-turn understate the architecture's cost, which is the
one thing the evaluation exists to measure — or force a change to a frozen schema, to buy a
capability the agent calling the skill already has for free.

### Sub-queries are retrieved sequentially, in the order given

**Chosen.** A `for` loop over the sub-queries.

**Rejected.** A thread pool, which is the obvious reading of the brief's "parallel retrieval".

**Why.** There is no latency to win — retrieval is in-process CPU work over a few hundred chunks —
and there is a metric to lose. `docs/EVALUATION.md` reports recall@5 three ways, one of which is
computed over the turn's **first** `retrieval` event, precisely so that the number is comparable
across ablation presets that differ in how many retrievals they perform. Racing the sub-queries
makes "first" a race, and that metric unreproducible. The fan-out shape the brief describes is real;
what is sequential is its execution.

### The "sources disagree" section is read from the corpus, not judged

**Chosen.** Guideline notes carry a `## Where guidance differs` section wherever authorities
genuinely diverge. `deep_research` extracts those sections verbatim from the notes it retrieved.
`find_guideline` flags which retrieved notes have one but does not duplicate the extraction.

**Rejected.** Asking a model to decide whether two retrieved passages disagree.

**Why.** That would be an unvalidated LLM judge running inside a tool and reported as a fact — the
same error as reporting faithfulness from a judge whose agreement with a human was never measured,
except worse, because it would be invisible in the results table. Reading the section means an empty
`disagreements` list says "the corpus does not record a divergence here", which is a checkable claim.
It also means the skill needs the whole note, not the chunk that matched, so `SkillContext.documents`
carries the loaded corpus; the section a disagreement lives in is usually not the chunk the query hit.

### The symptom table lives in the Skills layer and the red-flag table does not

**Chosen.** `RedFlagTable` in `consilium/safety/`; `SymptomSystemMap` in `consilium/skills/`.

**Rejected.** Putting both in substrate for symmetry.

**Why.** The rule that decides is how many layers consume the table. The red-flag table has two
consumers — the `assess_risk` skill and `OutputRepair` — so it has to sit below both, and a second
copy of its matching logic would let a symptom escalate through one path and not the other. The
symptom table has exactly one consumer, so it belongs to the layer that consumes it. Promoting it on
symmetry alone would put a table in the foundation that nothing in the foundation reads.

The symptom table also has no negation guard, deliberately: it records where a symptom is felt, and
"no chest pain" still tells you the person is describing a cardiovascular concern. Negation changes
the answer only where urgency is decided, which is the other table.

---

## Phase 4 — the ReAct loop, the agents, and the turn boundary

### `policy.yaml` is created in Phase 4, not Phase 7

**Chosen.** `data/policy.yaml` lands in Phase 4 carrying only the per-agent permitted-skill lists,
and is expanded in Phase 7 with the output policy — required elements, forbidden behaviours, and the
path reference to `data/red_flags.yaml`. `schema_version` is bumped when the expansion lands.

**Rejected.** (a) Hard-coding each agent's tool list in Phase 4 and switching it to the file in
Phase 7. (b) Moving the whole safety layer forward to Phase 4.

**Why.** The brief introduces the file with the safety layer in its §3.7 but also requires
`BaseAgent` to load from it at construction in §3.2, which is three phases earlier. That is an
ordering contradiction in the brief, not a design disagreement, so it is resolved rather than
escalated (owner's decision, pre-authorized). The hard-coded interim would be the worse fix: it
makes the policy file advisory for three phases, and "the agent's tools are derived from the policy"
is the property that makes the policy mean anything at all. Writing the lists once, in the file they
belong in, costs nothing and leaves no migration.

### The specialization is a prompt and a policy entry, and the class bodies prove it

**Chosen.** `ConsultationAgent`, `DiagnosticAgent` and `ResearchAgent` each declare exactly two
class attributes: `name` and `system_prompt`. Everything else — the tool list, the loop, the
context — comes from `BaseAgent` and from `policy.yaml`.

**Rejected.** Per-agent constructors wiring each specialist to its own tools.

**Why.** Per-agent wiring puts the specialization in two places that then disagree, and it makes the
safety policy decorative: a file saying `diagnostic` may not call `deep_research` means nothing if
the agent's tool list was never derived from it. `test_no_agent_class_hard_codes_a_tool_list`
asserts the class bodies stay at two attributes, so the claim is checked rather than asserted.

The permitted lists partition cleanly: each specialist owns its own three skills and all three share
`search_knowledge`. Sharing the unfiltered search is deliberate — an agent that cannot reach it has
no way to answer a question outside its own category filters, which would turn a routing mistake
into a failed turn instead of a slightly worse answer.

### Two budgets, and only one of them binds

**Chosen.** `max_tool_calls = 2` and `max_iterations = 3`, both overridable per call.

**Why they are not redundant.** `max_tool_calls` is the constraint that decides how much retrieval
grounds an answer and most of what a turn costs. `max_iterations` catches a failure it cannot: a
model that produces prose turn after turn without ever calling a tool never increments the tool
counter, so without the second guard the loop would not terminate. Defending both as if both bind
would be dishonest; they exist for different failures, and in practice the tool budget is the one
that fires.

The per-call override exists because `full_budget_6` differs from `full` in exactly this number.
Expressing an ablation preset as a second loop instance would mean the two presets ran through
different objects, which is one more difference than the ablation is supposed to isolate.

### The budget is enforced by the loop, and refused calls are not counted

**Chosen.** Once the tool budget is spent the loop stops passing tool schemas to the provider. If a
single response requests more calls than remain, the extras get a `tool` message saying the budget
is spent — and **no `tool_call` trace event**.

**Rejected.** (a) Asking for the limit in the system prompt. (b) Emitting a failed `tool_call` event
for each refused call.

**Why.** A prompt that says "use at most two tools" is a request; a loop that stops offering schemas
is a constraint, and the difference is visible in the tool-call distribution, which is reported.
Emitting events for refused calls would inflate that same distribution with calls that never ran —
and the distribution is exactly what `full_budget_6` exists to measure honestly, since a
distribution truncated at the cap cannot justify the cap. The model still gets a `tool` message for
every request, because a tool result with no matching request is rejected by every provider that
accepts tool calls at all.

### `forced_answer` is reserved for a call caused by exhaustion

**Chosen.** The trace caller is `agent:<name>` for an ordinary turn and `forced_answer` only when a
call is made with tools disabled *because* a budget ran out. A run configured with
`max_tool_calls=0` — the `baseline_llm` row — never offered tools in the first place, so its calls
stay attributed to the agent.

**Why.** Tokens per turn is reported split by caller. Labelling every `baseline_llm` call
`forced_answer` would put the entire control condition into a bucket named after a failure mode, and
the ablation table would then compare a row of forced answers against a row of ordinary ones.

### The turn boundary lives in `consilium/runtime.py`, not in the CLI

**Chosen.** `run_turn()` owns the invariant that one user turn is one `Tracer`, one trace file, and
one `turn` event written last. The CLI, the API and the eval harness all go through it.

**Rejected.** Assembling the tracer and the turn event in each entry point.

**Why.** Three entry points that each assemble a turn are three chances to configure it differently,
and the symptom appears late and indirectly: an evaluation number that cannot be reproduced through
the API. The same module is the composition root, because the two jobs are the same job — knowing
how the pieces fit. It imports from every layer, which is precisely why it is not one.

From Phase 5 the routing decision inside a turn moves to `consilium/router/`; the boundary around it
stays here, because the tracer and the `turn` event are properties of the turn rather than of how it
was routed.

### The escalation detector is built in Phase 4 and is deliberately strict

**Chosen.** `consilium/safety/escalation.py` requires an explicit instruction to seek care — a verb
plus a service or a place. It lands with the loop, three phases before `OutputRepair`.

**Rejected.** (a) Defining it inside `OutputRepair` in Phase 7. (b) Counting any occurrence of
"emergency" or "urgent".

**Why the timing.** Three fields of the `turn` event *are* this function applied to two different
strings, so nothing end-to-end is measurable without it. `OutputRepair` is also defined in terms of
it — the banner is prepended only when the detector returns False — so defining the detector inside
the repair would make the repair's own test unable to distinguish a detection bug from a repair bug.
The precedent is `red_flags.py`, built in Phase 2 for the same reason.

**Why strict.** `escalation_present_post_repair` *is* red-flag recall. A loose detector inflates the
one number in this project that must not be flattered: "asthma is a common cause of emergency
department visits" is a sentence about epidemiology, not an instruction. The opposite error — missing
a genuine escalation phrased unusually — understates recall and overstates the repair rate, both in
the conservative direction. The phrase list is therefore part of the measurement and is versioned
with it; adding a phrase changes what red-flag recall means.

In Phase 4 there is no repair, so `run_turn` writes the same value into the pre- and post-repair
fields and `repair_applied=False`. That is not a placeholder: the delivered answer really is the
model's own answer, which is exactly what those two fields claim.

### A second real provider, and stub clients for the glue that only it has

**Chosen.** `OpenAIProvider` and `AnthropicProvider` both implement `LLMProvider`. The parts that
differ between the two APIs are pure functions — system prompt as a parameter versus a message, tool
results as user-turn content blocks versus a `tool` role, Anthropic's tool schema as the OpenAI one
*unwrapped* rather than a second derivation — and are tested directly. The request assembly, the
retry policy and the streaming accumulation are tested with a small stub client injected through the
provider's own `client` argument.

**Rejected.** (a) One provider plus a config flag. (b) Leaving `chat()` and `stream_chat()`
untested until a live key exists.

**Why the second provider.** It is what makes `LLMProvider` a seam rather than a wrapper. Each of
the three differences above is the kind of thing a single-provider design encodes as universal
without noticing, and the cost of finding that out later is a rewrite of the agent layer.

**Why the stubs are not the thing the brief rejects.** The rejected practice is mocking
`sentence_transformers` or `chromadb` to satisfy the offline rule *instead of* using the `Embedder`
and `VectorStore` seams — faking a dependency where a real second implementation is the specified
design. For the LLM layer the specified second implementation is `MockProvider`, and it is what the
rest of the suite runs against. It cannot exercise the two real providers at all, and those contain
real behaviour with real bugs available to it: which keys reach the request, which exceptions are
retried, how stream pieces become one recorded `llm_call`. The stubs test code in this repository
through a constructor argument that exists anyway, and they claim nothing about whether a live
endpoint would answer.

`backoff_multiplier` is a constructor argument rather than a constant so that a 600-item evaluation
sweep against a rate-limited endpoint can use a different curve from an interactive CLI — and so
that a retry test does not spend a second per attempt proving that `tenacity` waits.

### `open_retriever` reuses a populated store instead of re-embedding

**Chosen.** `consilium ask` loads and chunks the corpus (BM25 needs the chunks in memory either
way), then re-embeds only if the vector store's chunk count does not match. `consilium ingest`
still resets unconditionally.

**Rejected.** Re-ingesting on every invocation.

**Why.** A persistent store outlives the process; re-embedding 312 chunks to answer one question
pays the whole ingestion cost per query. The freshness check is deliberately shallow — it catches
notes added, removed or rechunked, and does not try to detect an edit that leaves the count
unchanged. `consilium ingest` is the supported way to reload after editing a note, and it is the
command that resets.

---

## Phase 5 — planner, blackboard, parallel execution, synthesizer

### Planner-based routing, not a trained classifier and not keyword rules

**Chosen.** One LLM call with the three capability descriptions, an instruction to assign the fewest
specialists that can answer, three few-shot examples, and a JSON output schema validated by Pydantic.

**Rejected.** (a) A trained intent classifier. (b) Keyword rules over the question text.

**Why.** A trained classifier needs labelled routing data, and the only labelled routing data this
project has is the golden set — which is also what the routing metric is computed against. Training
on it and then reporting accuracy against it would be measuring the classifier's memory. Keyword
rules fail on the case the multi-dimensional block of the golden set exists to test: "my blood
pressure is 150/95, is that bad, and what do the guidelines say the target should be" contains
guideline keywords and symptom keywords, and the correct answer is *both agents*, which a rule table
can only reach by enumerating combinations.

The planner's cost is real and is measured rather than hidden: its call is traced as
`caller="planner"`, and tokens per turn is summed over **all** `llm_call` events. The overhead the
architecture adds is exactly what the ablation is supposed to expose.

The capability descriptions come from `policy.yaml`, not from the prompt file. One description means
the planner cannot be told an agent does something the policy does not permit it to do.

### Every unusable plan produces the same fallback, and the fallback is counted

**Chosen.** No content, no JSON in the content, JSON that does not parse, JSON that fails the
schema, a plan naming an unknown agent, an empty plan, or a provider error — all assign a single
`ConsultationAgent` subtask and set `fallback=True` on the `route` event.

**Rejected.** (a) Retrying the planner. (b) Dropping the unknown agent and keeping the rest of the
plan. (c) Raising.

**Why the flag matters.** Routing accuracy is reported unconditionally *and* excluding fallback
turns. Reporting only the second number would let a planner that fails half the time look perfect,
which is why the flag has to be set on every path that produces the default — a silent recovery
would be recorded as a routing success.

**Why an unknown agent invalidates the whole plan.** Dropping it would produce a smaller plan than
the planner intended and record it as a successful route to a narrower set of agents. That is a
wrong routing decision written down as a right one, which is worse than a counted fallback.

A repeated agent is collapsed to its first subtask instead: two subtasks for one specialist buy one
perspective at twice the cost, and `route.agents` is compared against a labelled agent list that a
repeated name could never match.

### `extract_json_object` counts braces instead of matching a regex

**Chosen.** A brace counter that knows about string literals and escapes, returning the first
balanced `{...}`.

**Rejected.** A regex, or `json.loads` on the whole reply.

**Why.** Models wrap JSON in prose and in code fences, so the whole reply often is not JSON. A
non-greedy regex stops at the first `}`, which is inside the nested subtask object; a greedy one
runs past the end of the plan into the trailing prose. Both failures land in the fallback bucket and
would be read as a planner that cannot produce JSON, when the planner produced perfectly good JSON
and the parser could not find it. Twenty lines of counter removes an entire class of misattributed
metric.

### A `route` event is emitted only when a routing decision was made

**Chosen.** `router="planner"` emits it. `router="single"` (the `single_agent_rag` control),
`router="none"` (the `baseline_llm` control) and a pinned `--agent` do not.

**Rejected.** Emitting a `route` event with `fallback=False` for every turn.

**Why.** Planner fallback rate is "share of turns where the planner's JSON failed to parse". For a
configuration with no planner, that is not 0% — it is n/a. Emitting the event would make the metric
compute a confident zero for a component that does not exist, and the ablation table would show a
control row outperforming the system on a metric the control cannot have. The absence of the event
makes n/a fall out of the data instead of out of a special case in the metric code.

### Workers cannot reach each other, because the API has no method for it

**Chosen.** A worker never sees the `Blackboard`. It gets a `SubtaskHandle` bound to one
`subtask_id`, whose whole surface is `subtask`, `agent`, `started()`, `completed()`, `failed()` and
`timed_out()`.

**Rejected.** Passing the board to each worker with a convention that they only touch their own row.

**Why.** Conventions are not enforced by anything, and the failure they permit is subtle: if workers
could read each other, outputs would depend on completion order, and two runs of the same question
could synthesize differently because one worker happened to finish first. The evaluation compares
parallel turns against single ones; a nondeterministic merge would be measured as architecture. A
test asserts the handle's public surface, so the guarantee is checked rather than described.

### A shared deadline, and partial failure is tolerated

**Chosen.** One `deadline_at` computed once for the turn; each worker runs inside
`asyncio.timeout_at(deadline_at)`. A worker that fails or times out is recorded on the board, and
the answer is synthesized from whoever finished, with the missing perspective named in the text.

**Rejected.** (a) A per-worker timeout. (b) `asyncio.timeout` around the whole `gather`. (c) Failing
the turn when any worker fails.

**Why.** Three workers each allowed 90 seconds is a 90-second budget only if they all start at once
and none of them queues — the thing the user waits on is the turn, so the deadline belongs to the
turn. Wrapping the `gather` would cancel the workers that had already succeeded and throw away their
answers at the moment they became most useful. And failing the turn on any worker failure would make
the parallel path strictly less reliable than the single one, which would be a strange thing to then
measure as an improvement.

`return_exceptions=True` is still passed to `gather` even though each worker catches its own
failures: a bug in the worker *wrapper* must not cancel siblings that already succeeded.

### Conflict precedence is deterministic, and four parts of it are code

**Chosen.** Urgency and red-flag claims defer to `DiagnosticAgent`, factual and background claims to
`ConsultationAgent`, evidence-strength claims to `ResearchAgent`.

**Rejected.** Asking the model which worker's answer is better.

**Why.** LLM arbitration fails exactly where it matters most: the diagnostic agent says *seek care
now*, and another agent's calmer, better-written paragraph reads more authoritative. A false
negative on a red flag is the worst error this system can make and over-escalation is the cheaper
failure, so that contest is not one the model gets to hold.

Four things are code rather than prompt text, and they are what makes "deterministic" a claim:

1. **Sections are presented in precedence order, never completion order.** Completion order is a
   race; a merge that depended on it would not reproduce.
2. **Each section is labelled with what its agent owns**, so the instruction in the system prompt and
   the evidence in the user message cannot drift apart.
3. **Missing perspectives are named by code**, appended after the merge. The model was never shown
   the failed output, so asking it to describe what is missing is asking it to describe something it
   cannot see.
4. **One completed worker means no synthesizer call at all.** There is nothing to merge, and
   paraphrase is the step at which a grounded answer loses its grounding.

Two further guarantees sit outside the synthesizer: the turn's `risk_level` comes from the red-flag
table applied to the *input*, in `run_turn`, so no amount of merging can move it; and a failed or
empty merge falls back to concatenating the workers' own answers in precedence order rather than
losing them.

### The router depends on a `Worker` protocol, not on `BaseAgent`

**Chosen.** `Router` takes an `AgentFactory` returning anything that satisfies `Worker`: a name, a
`for_turn`, and an `answer`.

**Rejected.** Importing `BaseAgent` and depending on the class.

**Why.** The protocol states the contract in one place — a worker is handed an objective and returns
a result — and there is no method on it through which one worker could reach another, which is the
same guarantee the blackboard handle makes, expressed in the other direction. `BaseAgent` satisfies
it structurally, so nothing changes on the production path. It also means a test can substitute a
worker that times out or raises without dragging a provider, a registry and a policy into a test
about dispatch.

The factory is *injected* rather than imported for a separate reason: `consilium/runtime.py`
constructs both the agents and the router, so importing it here would be a cycle.

---

## Phase 6 — memory

### Session state is keyed and injected, never a process-wide singleton

**Chosen.** `WorkingMemory` belongs to one `session_id` and is obtained from a `MemoryStore` keyed
by it, then injected for the duration of a turn. `Runtime` holds the store; nothing holds a session.

**Rejected.** A module-level conversation buffer, which is what the reference implementation does.

**Why.** Two concurrent API users would share a history. That is a correctness bug (the answers
would be wrong) *and* a privacy one (one user's symptoms would appear in another's context), and it
is exactly the failure a single-user CLI demo never reveals. It also makes the module untestable
without monkeypatching a module global, which is why the isolation test in
`tests/test_memory_store.py` — two sessions interleaved under `asyncio.gather`, each seeing only its
own turns — is possible at all.

Sharing *within* one turn is achieved by passing the same instance: every worker of a parallel turn
receives the same compacted history object. That is what the reference implementation actually
needed, and it does not require a global to get it.

### Context compaction, and never "entropy management"

**Chosen.** The window-plus-dedup-plus-recap step is called context compaction in every identifier,
comment, docstring and document.

**Rejected.** "Entropy management", the reference implementation's term.

**Why.** Nothing in the module computes an entropy. The term describes a dedup function and a
truncation, and it survives exactly one follow-up question in an interview. Inflated names are a
signal pointing the wrong way.

### The recap is deterministic extraction, not an LLM summary

**Chosen.** Exchanges older than the window are compacted by extracting the question, the opening
sentence of the answer, the recorded risk level when it was not routine, and the deduplicated list
of documents consulted.

**Rejected.** An LLM call that summarizes the dropped exchanges.

**Why.** Three reasons, and the first is decisive. `llm_call.caller` is pattern-validated to
`planner`, `synthesizer`, `forced_answer` and `agent:<name>` — there is no slot for a summarizer, so
the call would either be untraced (making tokens-per-turn understate the architecture's real cost)
or force a change to a frozen schema. Second, it would put a nondeterministic string into the input
of every subsequent turn, which makes the 30 multi-turn conversations in the golden set
irreproducible — the same conversation would compact differently on two runs and the multi-turn
resolution metric would be measuring the summarizer. Third, an extractive recap can be checked line
by line against the transcript, and a generated one can only be checked by another model.

The cost is that the recap is crude: one sentence per dropped exchange. For its actual job —
reminding the model what has already been discussed and which documents were already read — crude is
sufficient, and honest.

### Tool observations are not replayed into later turns

**Chosen.** What memory carries forward is the question, the answer, and the `doc_id` values behind
it. The retrieved passages themselves are dropped after the turn that retrieved them.

**Rejected.** Keeping the tool observations in the conversation buffer.

**Why.** Replaying an observation requires replaying the matching assistant tool-call message —
providers reject a tool result with no request — which means the entire prior ReAct transcript
enters every later turn, and the context grows without bound across a 7-turn conversation. Worse, it
voids the current turn's tool budget in a way no metric would show: the model could answer from
evidence it did not retrieve this turn and is not obliged to cite, so `tool_call.source_doc_ids`
would understate what the answer actually rests on. Re-retrieval costs one tool call; an unbounded,
uncited context costs the grounding claim.

Dedup by content hash is what makes the surviving citation list correct: the same passage retrieved
in three turns is listed once. `blake2b` and not the built-in `hash()`, which is salted per
interpreter run — the same session would otherwise compact differently on two machines, and the test
pins a golden digest computed in another process to prove it.

### The recap is a `user` message, not a `system` one

**Chosen.** The recap is delivered as a `user` message tagged `[earlier in this conversation]`.

**Rejected.** A `system` message, which is the conventional choice.

**Why.** System content is instruction. On providers that lift system messages into a top-level
parameter — Anthropic does, and `to_anthropic_messages` concatenates them — a system-role recap
would be glued onto the agent's own rules and read as something the model must do rather than
something that already happened. A user-role message tagged as history is what it actually is, and
it behaves the same on both providers.

### Redis is a backend, not a dependency

**Chosen.** `SerializedStore` works over any `KeyValueBackend`. `DictBackend` is a real in-process
implementation and is what the tests exercise; `RedisBackend` is fifteen lines that import `redis`
inside the constructor. `redis` appears nowhere in `pyproject.toml`.

**Rejected.** (a) Adding `redis` as a dependency or an extra. (b) Testing the Redis path by mocking
the `redis` module.

**Why.** The brief permits Redis as an optional backend and explicitly not as a requirement, and an
optional backend that made the package fail to import would not be optional. Splitting at
`KeyValueBackend` means everything above the adapter — the serialization, the key namespacing, the
round trip, the unreadable-state error — is covered offline by a real second implementation, and the
only untested code is the fifteen lines that call `redis` methods. The adapter itself is exercised
against a client-shaped object, which proves the adapter without claiming anything about a server.

### Episodic memory is SQLite with a brute-force scan, and the limit is written down

**Chosen.** One row per session in local SQLite, embedding stored as a float32 blob, retrieved by
cosine over every row, top 3, behind an `EpisodicStore` protocol. The same `Embedder` as retrieval.

**Rejected.** A hosted memory service, or a vector index.

**Why.** SQLite has no vector index, so a query reads every stored vector. At 384 dimensions that is
1,536 bytes per row: 10,000 sessions is about 15 MB read and a 10,000×384 matmul, single-digit
milliseconds, which is nothing next to one LLM round trip. `BRUTE_FORCE_ROW_CEILING = 10_000` is the
stated limit; past it the scan dominates and the answer is an index (`sqlite-vec`, pgvector, or a
real vector store behind the same protocol), not a faster loop. The store logs a warning when a
query scans more rows than that, so the limit is enforced by the code rather than only claimed in a
document. A portfolio project does not reach 10,000 sessions, so an index would be complexity bought
for a load that does not exist — but the number makes "we did not need one" a measured claim.

Using the same embedder as retrieval avoids two downloads, two dimensions to keep in step, and a
second thing to explain; it also keeps the module runnable offline through `HashEmbedder`.

The row is *upserted* per turn rather than written once at session end, because neither a CLI nor a
stateless HTTP API has a session-end signal. The table therefore holds exactly one row per session,
which is what "one summary per completed session" means in practice.

### Episodic recall is disabled in every measured run, and that is reported

**Chosen.** `EpisodicMemory.recall_enabled` defaults to `False`. Sessions are still remembered; only
recall is off. `docs/EVALUATION.md` reports the effect of episodic memory on answer quality as
`not measured`.

**Rejected.** Enabling recall during the evaluation sweep.

**Why.** The golden set's 150 items are independent questions. Running them through one system with
cross-session recall means item N can be answered from item N−1's stored summary — contaminating
faithfulness, recall@5 and the whole ablation at once, in a direction that flatters the system. The
alternative would be to give every item its own database, which measures episodic memory by
disabling it in a more expensive way.

So the component is built, tested, and honestly labelled as unmeasured. That is the same rule the
rest of the project follows: a number the harness did not produce does not get written down.

---

## Phase 7 — safety

### Detection and repair are two classes, because they are two counts

**Chosen.** `PolicyValidator` finds violations and emits `safety` events with `event="violation"`.
`OutputRepair` consumes those violations, fixes what it can, and emits `event="repair"`. Neither
does the other's job.

**Rejected.** One `SafetyGuard` that detects and fixes in a single pass, emitting one event.

**Why.** The violation rate says how often the model produced non-compliant output; the repair rate
says how often the guard had to act on it. They move independently, and the interesting case is when
they diverge — a model getting worse while a guard keeps working looks like a stable system if the
two numbers are merged. `docs/EVALUATION.md` reports them as two rates and never sums them, and
keeping the code in two classes is what makes that a structural property rather than a reporting
convention.

### Forbidden patterns are regular expressions; red-flag patterns are literal phrases

**Chosen.** `data/red_flags.yaml` keeps literal, auditable phrases. `data/policy.yaml`'s forbidden
behaviours are regexes, matched case-insensitively, one sentence at a time.

**Rejected.** Making both literal, or making both regex.

**Why.** A dose is a number followed by a unit — `500 mg`, `2.5 ml`, `10 units` — and no list of
literal phrases can express that. Going the other way, making the red-flag table regex would destroy
the property that matters most about it: that a non-programmer can read the file and know exactly
what escalates. So the two files use the tool their content requires, and the difference is stated
in both.

The regex list is kept short and each entry is commented, because the cost of a regex is that it is
harder to audit and its false positives delete real sentences. The patterns are tight for that
reason: `\byou (?:definitely|certainly|clearly|most likely) have\b`, not `\byou have\b`.

### A forbidden sentence is removed, not rewritten

**Chosen.** The offending sentence is replaced by a marker naming the rule —
`[removed: this system does not diagnose.]` — and the rest of the answer is delivered unchanged.

**Rejected.** (a) Rewriting the sentence into a compliant one. (b) Flagging the violation and
delivering the answer anyway. (c) Discarding the whole answer.

**Why.** Rewriting a clinical sentence into a different clinical sentence produces text nobody wrote
and nobody checked, and the rewrite would itself need checking — the guard would become a second
generator. Delivering it anyway makes the policy advisory, which is the failure mode where every
metric still reports a clean run. Discarding the whole answer throws away the grounded parts because
of one sentence. Removal is the only option that is deterministic, checkable, and leaves the reader
able to see that something was taken out rather than silently receiving a gap.

### The repair order is fixed: redact, prepend the banner, append the disclaimer

**Why each position.** The banner has to be the first thing read, so it is prepended after redaction
(redacting afterwards could reach into the banner) and before the disclaimer. The disclaimer is
boilerplate and goes last, where boilerplate belongs. Redaction runs first so that a forbidden
sentence cannot survive by being pushed past whatever the other repairs add.

### Escalation is decided on the input, and the banner is only prepended when it is missing

**Chosen.** `PolicyValidator` raises the `escalation_required` violation when the **user's input**
matched a red-flag pattern *and* the answer contains no seek-care instruction.

**Rejected.** (a) Deciding from the answer's content. (b) Always prepending the banner on a red-flag
input.

**Why.** Deciding from the answer lets an answer that never mentions the symptom pass by saying
nothing, which is the exact failure that matters. Always prepending would mean a correctly-handled
red flag also emits a repair — and then red-flag recall computed from repair events would score the
system's best behaviour as a false negative. This is why the `turn` event carries three separate
fields: `escalation_present_pre_repair` (the model handled it unaided),
`escalation_present_post_repair` (**this is red-flag recall**), and `repair_applied` (the guard, not
the model, is what saved it).

`tests/test_safety_policy_enforcement.py` pins the case that motivates the split: an answer that
already says "call emergency services now" produces no escalation repair at all.

### The escalation banner must be recognisable to the escalation detector

The banner is written so that `escalation_present()` returns True for it, and a test asserts exactly
that. Without it the repair would prepend a banner and the turn would still record
`escalation_present_post_repair=False` — the guard would fire and the metric would say it had not.
That is the kind of defect that survives review because both halves look correct in isolation.

### The loop refuses an unpermitted skill, and the validator counts it

**Chosen.** Both. `ReActLoop` refuses a skill outside the agent's permitted list whether or not a
validator was injected; `PolicyValidator.check_tool_call` is what emits the `safety` violation.

**Rejected.** Only one of the two.

**Why.** The loop's refusal is what guarantees an unpermitted skill cannot execute — a guarantee
that should not depend on an optional injected object. The validator's event is what makes the
refusal countable, and a blocked call with no measured trigger rate is decoration. Neither half is
redundant: one is the enforcement, the other is the measurement.

### `policy.yaml` names `red_flags.yaml` by path, and the runtime resolves it

**Chosen.** `policy.yaml` carries `red_flags: red_flags.yaml`, resolved relative to the policy file.
`build_runtime` loads the red-flag table from the path the policy names.

**Rejected.** (a) Restating the emergency phrases in `policy.yaml`. (b) Naming the file in the
policy but loading it from a separate setting.

**Why.** Two copies of the emergency list would eventually disagree, and the copy that lost would be
the one deciding whether a user is told to seek care. A test loads both files and asserts that no
red-flag phrase appears anywhere in `policy.yaml`. Resolving the reference in `build_runtime` — not
just documenting it — is what makes the reference load-bearing: if it were ignored in favour of a
second setting, the path in the policy would be a comment.

### Memory records the delivered answer, not the model's raw one

**Chosen.** `run_turn` records the post-repair text in `WorkingMemory` and in the episodic store.

**Why.** A later turn's context has to match what the user actually saw. Recording the raw answer
would mean the conversation the model believes it had is not the one that happened, and a redacted
sentence would come back through memory into a later turn's prompt — reintroducing exactly the
content the guard removed.

### `consilium runs purge`, and why it lands with the safety layer

**Chosen.** `consilium runs purge [--session ID] [--yes]`, refusing to touch anything outside the
configured runs directory and prompting unless told not to.

**Why here.** `runs/` holds verbatim user questions and full prompts, which makes it the most
sensitive artifact this project writes even though it forbids real patient data. `docs/SAFETY.md`
has to state a retention rule; a retention rule with no mechanism behind it is a sentence. The
path check is the third place the same `session_id` is validated — `Tracer` makes it a directory
name and `MemoryStore` makes it a cache key — and it gets its own check rather than trusting the
other two, because this is the one that deletes.

The confirmation prompt is not politeness: purging destroys the evidence behind every number
computed from those traces, and `eval/results/published/` is committed precisely so that published
numbers survive it.

## Phase 8 — the evaluation harness

The harness's metric definitions and their caveats live in `docs/EVALUATION.md` §3, which is where
a reader looking for "how is recall@5 defined" should go. What follows is the subset of Phase 8
decisions that had a real alternative and rejected it.

### Two label fields ship as machine-written candidates, and two ship empty

**Chosen.** `relevant_doc_ids` and `reference_answer` ship holding a candidate for the owner to
verify. `expected_route` and `red_flag` ship empty and are never proposed, in any field.

**Rejected — all four empty.** The original draft, and the safer-looking option.

**Rejected — all four proposed.** The efficient-looking option, and the one that would have made
the checkpoint decorative.

**Why.** The two halves fail differently. Naming the corpus notes that answer a question, and
writing two or three sentences from those notes, is mechanical: the labeller has to open the same
documents either way, so a candidate saves the transcription and costs nothing in independence —
what it anchors is a judgement the labeller was going to make against the same evidence, in the
same session, with the note in front of them. Routing and red-flag status are not like that. There
is no document to check them against; they are the labeller's reading of what the question is, and
they are the exact labels that routing accuracy and red-flag recall are computed against. A
candidate there would be an anchor on the two numbers the gate exists to protect, and the fact that
a plausible-sounding suggestion is usually right is what makes it dangerous rather than what makes
it acceptable.

The cost of the split is that it is asymmetric and has to be explained; a uniform rule would be
easier to state. That is the wrong thing to optimize for. `docs/EVALUATION.md` §1.1.1 states which
side each field is on and why, because the distinction is the whole answer to "how do you know the
eval set isn't circular".

### A candidate is marked in the record, not only in the documentation

**Chosen.** `proposed_fields` on every record names the fields holding a candidate, and
`GoldenItem.missing_labels()` reports a named field as **missing** even though it is populated, so
`load_golden` refuses the file until the marker is cleared.

**Rejected.** Writing the candidates in and stating in `docs/EVALUATION.md` that they are
candidates.

**Why.** Same reason the labelling gate is in the loader rather than in a document. Without the
marker, an owner who filled in `expected_route` and `red_flag` and set `labeled: true` would have a
file that loads — and 148 machine-written reference answers would have become ground truth by
silence, with nothing in the run able to tell that anyone had read them. With it, "the owner
verified this field" is a property of the file that the loader can check, and clearing the marker
is a deliberate act per item.

The marker is validated against `LABEL_FIELDS` rather than accepted as free text, because the
failure mode of a provenance marker is a typo that disables it, and a gate that fails open is worse
than no gate: it reports the same green run.

### The red-flag items are stratified by phrasing, and the strata are never pooled

**Chosen.** 22 hard-phrasing items that do not reuse the strings in `data/red_flags.yaml`, 5
easy-phrasing items that name the symptom the ordinary way, each item declaring its stratum, recall
reported per stratum.

**Rejected — hard phrasing only.** The original constraint, which produced a matcher hit rate of
0 of 22.

**Why.** A 0 with nothing beside it is not a measurement of the system; it is a measurement of
whether the drafting instruction was followed. The number that matters is the distance between what
paraphrase does and what canonical phrasing does, and that requires both. The five easy items make
the 0 attributable: 5 of 5 match, so the rule table is not broken, and the gap is the phrasing.

**Why not pooled.** A single red-flag recall figure over 27 items would move with the ratio between
the strata, and that ratio is a drafting choice — five was picked as enough to bound the hard number
from above without letting canonical phrasing become the bulk of the block. Pooling would launder
that choice into what reads as a property of the system. Reporting `0/22` and `5/5` says what was
actually observed, and the reader can weight them.

The stratum is declared per item rather than inferred, because the alternative — "easy is the ones
marked, hard is everything else" — silently absorbs any candidate nobody classified into the hard
stratum, which is the one whose number is the finding.
