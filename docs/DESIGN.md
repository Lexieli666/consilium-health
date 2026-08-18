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
