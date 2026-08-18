# The corpus: what it is, who wrote it, and what it may be used for

> **Not medical advice.** This is an educational software project. It does not diagnose, treat, or
> provide clinical guidance, and it must not be used for real medical decisions. No patient data of
> any kind may be used with it.

## What is in it

**78 notes**, inside the 60-80 band the brief specifies. Every one of the five `category` values has
real documents; an empty category leaves its skill with nothing to retrieve and makes the matching
block of the golden set unanswerable by construction, which is what the `coding` category being
empty would have done to `lookup_disease_code`.

| category | notes | shape |
|---|---|---|
| `condition` | 16 | one per condition: what it is, how it is recognized, why it is treated |
| `guideline` | 19 | thresholds, first-line management and follow-up intervals, with "Where guidance differs" where authorities diverge |
| `coding` | 19 | 10 chapter maps, 7 per-condition code-selection notes, 2 on the classification itself |
| `lifestyle` | 13 | diet 4, activity 5, sleep 2, adherence 2 |
| `red_flag` | 11 | emergency presentations, consistent with `data/red_flags.yaml` |

Body lengths run from 2,725 to 3,493 characters, inside the 2,700-3,500 band.

The `coding` notes are written from **ICD-10-CM**, the US clinical modification maintained by NCHS
and CMS, not from WHO ICD-10 — a distinction that has its own note, because a code taken from an
international reference may not exist in the US modification. No note reproduces a tabular code
list. Codes appear inside sentences, which is both what the length and chunking bands require and
what the tokenizer is built for: a dumped table would chunk badly and adds nothing a real lookup
does better.

`tests/test_corpus.py` enforces everything in this document that can be enforced mechanically,
including these counts, the length band, and the requirement that every `condition` note is named in
at least one `coding` note.

## Provenance

Every note in `data/corpus/` was written for this repository as an original educational summary.
Nothing in it is copied, scraped, or quoted from a clinical guideline, a textbook, or any other
copyrighted source. Where a note describes what a guideline recommends, it describes the *kind* of
recommendation a body of that type makes, in the author's own words.

### Why `source:` names a kind of authority and not a document

The `source:` field names **the kind of authority a statement reflects** — "major cardiology society
consensus guidance (educational summary)", "general clinical reference" — and never a specific
document, edition, table, or year.

This is a deliberate choice, and the reason is worth stating plainly because it looks at first like
a weaker option. A field reading `ACC/AHA 2017 Guideline, Table 6` would look more rigorous, and it
would be a fabricated citation. These notes are not quotations of that table. They are summaries
written from general knowledge of the kind of recommendation such a body makes, and dressing them as
sourced extracts would assert a provenance that does not exist. That is the same class of error as
putting an unmeasured number in the README — a claim of evidence where the evidence was never
produced — and this project's central rule is that it does not make claims of that kind.

The honest consequence, which the README states as a limitation: this system can describe what kind
of recommendation is typically made and roughly where the thresholds sit. **It cannot be cited, and
it is not a substitute for the actual guideline document.** A user who needs the real threshold
needs the real source.

## Spelling: US forms, with alternate-spelling anchors

**The corpus is written in US English.** This is a retrieval decision, not a style preference.

The clinical content these notes summarize is US-derived — ACC/AHA blood-pressure staging, diabetes
association HbA1c targets, preventive-services screening intervals, mg/dL units throughout. Writing
that content in British orthography would put the corpus's spelling and its subject matter in
different variants, with two measurable consequences:

1. **BM25 is lexical.** A user asking about "esophageal reflux" does not match a document that only
   contains "oesophageal". Rare-term lexical matching is precisely the capability that the
   hybrid-retrieval argument in `docs/DESIGN.md` rests on, so a spelling mismatch would sit directly
   on top of the project's central retrieval claim — and would show up as a retrieval miss whose
   real cause was orthography.
2. **The golden set inherits the mismatch.** Questions and reference answers get written in one
   variant. Any divergence from the corpus becomes a measured retrieval failure that has nothing to
   do with retrieval quality, and it would have to be explained away rather than fixed.

**Alternate-spelling anchors.** Where a clinical term has a common alternate form a user might
plausibly type, the alternate appears **once**, in parentheses, at first use in each note where that
term is central — for example "gastroesophageal reflux disease (gastro-oesophageal in British
spelling)", "iron-deficiency anemia (anaemia)", "obstructive sleep apnea (apnoea)". The tokenizer
splits `gastro-oesophageal` into `gastro` and `oesophageal`, so the alternate survives as its own
BM25 term and either spelling retrieves the document.

This is a deliberate retrieval accommodation, not an inconsistency. The current anchor set is
`oesophageal`/`oesophagus`/`oesophagitis`, `haemoglobin`, `anaemia`, `apnoea`, `generalised`, and
`GORD`; `paediatric`, `oedema`, `diarrhoea` and `haemorrhage` join it as notes needing them are
written. A corpus lint test asserts that no *other* British form appears, so the accommodation stays
an explicit list rather than drifting into mixed orthography.

## `doc_id` conventions — frozen for all five categories

**`doc_id` == the filename stem, and it is stable by contract.** The golden set labels `doc_id`s in
Phase 8. Renaming a corpus file after that silently invalidates every label pointing at it, so
renaming one is a re-labeling job, not a `sed`.

Slug rules, applying to every category: lowercase; hyphen-separated; **no underscores**; numerals as
digits (`type-2-diabetes`, not `type-two-diabetes`); no abbreviation except where the abbreviation is
the common name (`gerd`, `copd`, `uti`, `icd10`).

| category | `doc_id` pattern | examples |
|---|---|---|
| `condition` | `condition-<topic>` | `condition-hypertension`, `condition-type-2-diabetes` |
| `guideline` | `guideline-<topic>-<aspect>` | `guideline-hypertension-diagnosis-and-bp-targets`, `guideline-insomnia-cbt-first-line` |
| `lifestyle` | `lifestyle-<topic>-<domain>`, domain ∈ `diet` \| `activity` \| `sleep` \| `adherence` | `lifestyle-hypertension-diet`, `lifestyle-type-2-diabetes-activity` |
| `coding` | chapter notes: `coding-icd10-chapter-<nn>-<system>`; per-condition notes: `coding-<topic>-<code-root>` | `coding-icd10-chapter-09-circulatory`, `coding-hypertension-i10`, `coding-type-2-diabetes-e11` |
| `red_flag` | `red-flag-<presentation>` | `red-flag-chest-pain`, `red-flag-stroke-symptoms` |

Two details that will otherwise trip someone up:

- **The `red_flag` category value keeps its underscore; the `doc_id` prefix uses a hyphen.** The
  category is a Python `Literal` in `consilium/retrieval/types.py` and matches the enum used in
  metadata filters; the `doc_id` is a filename stem and follows the hyphen-only slug rule. They are
  deliberately not the same string.
- **Per-condition `coding` notes carry the code root without the decimal** (`e11`, not `e11.9`),
  because a dot in a filename stem reads as an extension. Specific codes with decimals appear in the
  body text, where the tokenizer is built to preserve them intact.
- **Where a condition spans a block of roots rather than one, the `doc_id` carries the lowest root
  in the block.** Osteoarthritis occupies M15–M19 and its note is `coding-osteoarthritis-m15`;
  influenza occupies J09–J11 and would be `coding-influenza-j09`. The choice of the lowest root is
  arbitrary but deterministic, which is the only property a filename needs; the body states the full
  block, and that is where a reader learns it.
- **A third `coding` form, `coding-icd10-<aspect>`, covers notes about the classification itself**
  rather than about a chapter or a condition — `coding-icd10-cm-code-structure-and-conventions`,
  `coding-icd10-cm-versus-who-icd10`. The chapter form requires a two-digit number, so a malformed
  `coding-icd10-chapter-9-circulatory` fails the lint instead of quietly passing as an
  aspect note.

## Chapter notes map; condition notes select

A `coding` chapter note and a `coding` condition note answer different questions, and the division
between them is frozen. It was frozen the hard way: the first chapter-note draft reproduced
`coding-hypertension-i10` almost claim for claim — the same I11/I12/I13 ladder, the same
heart-versus-kidney causal-presumption paragraph — and that is a measurement defect rather than a
matter of taste. Two documents answering one question make `relevant_doc_ids` ambiguous when the
golden set is labelled, inflate the recall@5 denominator whenever both are listed, and consume two
of the five top-k slots after per-`doc_id` dedup has already run.

| note kind | owns |
|---|---|
| chapter | the **map**: the chapter's letter and range, its blocks in order and what each is for, what is deliberately *not* in the chapter and which chapter holds it instead, and the cross-chapter codes questions in that area commonly need |
| condition | **code selection**: which code applies, which combination rules govern it, which second codes are required, which conventions decide the choice |

Where a chapter note has to gesture at a rule a condition note owns, **one clause naming the rule is
the budget** — not a section, and not a worked explanation. The general form: if the same rule is
explained in two notes, one of the two notes is wrong.

**Every condition note in the corpus is named, with its code root, in at least one coding note** —
per-condition where the code selection is nuanced enough to need its own document, chapter-level
where it is not. This is the property that makes the coding and condition notes mutually
retrievable, and it is precisely what an empty `coding` category destroys.

## Format contract

Every file in `data/corpus/` is an ingestable note — no READMEs, no index files, no
subdirectories — so "every file is a document, and its `doc_id` is its filename stem" holds without
exceptions. Front matter is exactly five keys, in this order, and the loader rejects extras:

```yaml
---
doc_id: guideline-hypertension-diagnosis-and-bp-targets   # == the filename stem
category: guideline                                        # lifestyle|coding|guideline|condition|red_flag
title: "Hypertension: diagnostic thresholds and blood-pressure targets"
source: "Major cardiology society consensus guidance (educational summary)"
last_reviewed: 2026-08-09
---
```

Immediately after the front matter, every note carries the same one-line disclaimer blockquote. The
loader **requires** it, which turns the labeling convention into a test, and **excludes it from
chunk text**: a string identical in every document carries no retrievable information, is zero-IDF
noise to BM25, and shifts every dense vector by the same non-trivial amount.

**Note length is 2,700–3,500 characters of body**, which yields 3–4 chunks per note at the 800–1,000
character chunk size. This is deliberate rather than incidental: a one-chunk corpus would never
exercise the per-`doc_id` deduplication step in RRF fusion, and that step is what stops a document
with two strong chunks from consuming two of the five slots the model sees.

## What the corpus deliberately contains

**Disagreement.** Several guideline notes carry a "Where guidance differs" section, because
authorities genuinely diverge: the hypertension diagnostic threshold (130/80 in US guidance, 140/90
in European), the diabetes screening population (all adults from 35 versus overweight adults 35–70),
the preferred reliever in mild asthma, whether to state numeric LDL targets, and the treatment band
for subclinical hypothyroidism. The `deep_research` skill is required to produce an explicit
"sources disagree" section; it can only do that if the corpus actually disagrees with itself, so
these divergences are content the evaluation depends on rather than editorial hedging.

**Cross-category overlap by design.** A condition note and its guideline notes cover the same
disease from different angles — what it is, versus what the thresholds and intervals are. The
category filter keeps them from competing for the same top-5 slots, and questions needing both are
exactly the multi-dimensional questions the golden set labels `mode: parallel`.

## What the corpus deliberately excludes

- **Doses, titration schedules, and prescription instructions.** `policy.yaml` forbids the system
  from emitting them; a corpus containing them would be a retrieval path around the policy.
- **Prevalence and epidemiological statistics.** They would be unmeasured numbers of a different
  kind, and nothing in the evaluation needs them.
- **Definitive diagnostic statements.** Notes describe how a condition is recognized, not how to
  conclude that a particular person has it.
- **Directive phrasing.** "First-line options are X"; never "you should take X".

## The optional external corpus

`scripts/ingest_medquad.py` loads the public-domain NIH MedQuAD dataset, so the system can be
pointed at a real external corpus rather than only at notes written by the same author who wrote the
system. It downloads nothing during tests. `docs/EVALUATION.md` lists "corpus written by the author
of the system under evaluation" as a threat to validity, and this loader is the honest mitigation
available at portfolio scale — not a claim that the threat has been removed.
