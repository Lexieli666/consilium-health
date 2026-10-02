# Retrieval on Snowflake Cortex Search

> **Not medical advice.** This is an educational software project. It does not diagnose, treat, or
> provide clinical guidance, and it must not be used for real medical decisions. No patient data of
> any kind may be used with it.

One question, one number: what is Consilium's retrieval recall@5 when the retriever is Snowflake
Cortex Search instead of the self-hosted BM25 + bge-small + RRF hybrid, on the same 150 golden
questions, the same 78-note corpus and the same chunker? Measured 2026-10-01 (results are dated
2026-10-02 UTC); every number below is read from the JSON files in `results/2026-10-02/`.

| retriever | embedding | recall@5 | hit@5 | MRR@10 | p90 latency | note |
|---|---|---|---|---|---|---|
| Consilium hybrid, self-hosted (published run, `single_agent_rag`) | bge-small-en-v1.5 + BM25, RRF k=60 | 0.721 | 0.805 | 0.691 | 3341 ms (whole agent turn) | from `eval/results/published/summary.json`; measured inside agent turns, see below |
| Consilium hybrid, self-hosted (direct replay) | bge-small-en-v1.5 + BM25, RRF k=60 | **0.918** | 0.946 | 0.833 | 7.5 ms (retrieval only, in-process) | `self_hosted_bge.json` |
| Cortex Search | arctic-embed-m-v1.5 | **0.950** | 0.973 | 0.932 | 241 ms (retrieval only, network call) | `cortex_m15.json` |
| Cortex Search | arctic-embed-l-v2.0 | **0.957** | 0.987 | 0.927 | 240 ms (retrieval only, network call) | `cortex_l20.json` |

n = 149 on every row: the 149 golden items that carry a `relevant_doc_ids` label, which is the same
denominator as the published run (`g-md-027` has no relevant document and is excluded there too).

## What was held constant, and what was not

Held constant: the 78 notes in `data/corpus/`; the chunker (`consilium.retrieval.chunking`, 312
chunks, 800-1,000 characters, longest 991); the golden set (`eval/data/golden.jsonl`); the metric
code (`eval.metrics.recall_at_k`, `hit_at_k`, `reciprocal_rank`, imported rather than reimplemented);
and the retrieval protocol of the replays: the raw question as the query, no category filter, 20
chunks requested, deduplicated to documents in rank order, the first 5 scored for recall@5 and hit@5
and the first 10 for MRR@10. `load_corpus.py` writes the chunks to Snowflake with Snowpark
`write_pandas`, so Cortex indexes byte-identical chunk text; 991 characters is well under the
512-token guidance for a Cortex search column, so no re-chunking was needed.

Not constant, and the reason the table has four rows rather than three: **the published 0.721 is
not a retriever number**. It was measured inside `single_agent_rag` agent turns, where the model
rewrote each question into a search query, 86 of the 149 turns went through a category-filtered
skill (the mechanism of Failure Case 2: `g-gh-023`'s relevant note is `lifestyle-asthma-adherence`
and the agent filtered to `condition`), and a few turns never retrieved at all (`g-md-017`,
`g-md-021`). The Cortex replay sends the raw question to an unfiltered index. Those are different
experiments, so `replay_self_hosted.py` runs Consilium's own retriever under the Cortex protocol.
That row, 0.918, is the like-for-like baseline; the gap from 0.721 to 0.918 is the cost of the
agent's query rewriting and skill routing, not of the index. (For reference, the same replay with
the project's offline `HashEmbedder` instead of bge-small scores 0.756, `self_hosted_hash.json`.)

Latency is not comparable across rows either and the table says so: 3341 ms is a whole agent turn
including model calls; 7.5 ms is an in-process NumPy and BM25 lookup; 240 ms is a Cortex REST round
trip from a laptop to AWS us-east-2.

## The reference caveat, carried over

`relevant_doc_ids` is a machine-written reference that no person verified (144 of the 150 items,
see the main README). recall@5 on every row of this table is therefore recall against the same kind
of reference on both sides. The comparison between rows is sound; the absolute values inherit the
caveat.

## Cost

Setup: an Enterprise trial on AWS us-east-2, `ACCOUNTADMIN` throughout, which is acceptable for a
throwaway trial and not for a deployment. A real deployment would use a dedicated role holding
`CREATE CORTEX SEARCH SERVICE` on the schema, `SELECT` on the source table and `USAGE` on the
warehouse, and the network policy attached to the user for the programmatic access token would
name an egress IP rather than `0.0.0.0/0`. Both services were created with `TARGET_LAG = '1 day'`
because the corpus is static: a short lag would spend warehouse credits refreshing an index that
never changes. The warehouse is XSMALL with `AUTO_SUSPEND = 60`.

Credits: two services over 312 chunks, two 149-query replays and the setup queries. Read from
`SNOWFLAKE.ACCOUNT_USAGE.METERING_HISTORY` about four hours after the runs: **0.2696 credits of
warehouse time** (the index builds plus the Snowsight session) and **0.0047 credits of AI
functions**, with `CORTEX_SEARCH` serving not yet metered (the serving history had four rows and no
credits; it is billed per GB indexed per month, and 312 chunks are a few hundred kilobytes). At the
Enterprise list price of about $3 per credit that is under $1 of a $400 trial balance. The
`ACCOUNT_USAGE` views lag by hours, so the figure at the end of the day will be slightly higher;
the two cost queries at the end of `services.sql` are the ones to rerun.

## What the result means

Cortex Search beats the like-for-like self-hosted baseline by 3.2 points of recall@5 with the medium
model and 3.9 with the large one, and by about 10 points of MRR@10 (0.93 against 0.83). The
per-item results point at two mechanisms, one on each side.

**Where Cortex wins: buried and obliquely phrased symptoms.** The self-hosted retriever's eight
zero-hit items are concentrated in the `su` stratum (subtle red flags) and the `md` stratum;
Cortex recovers six of them with both models. `g-su-006`, `g-su-022`, `g-su-028` and `g-ge-007` go
from recall 0.0 to 1.0, and `g-md-021` (an 80-year-old, confused since morning, not passing urine;
relevant: `red-flag-sepsis`, `condition-urinary-tract-infection`) goes from 0.0 to 0.5 (m) and 1.0
(l). These are questions where the words of the question and the words of the note barely overlap,
which is where a stronger embedding model and a semantic reranking stage would be expected to help;
the MRR gap supports the reranker in particular, since Cortex m-v1.5 puts the first relevant
document at rank 1 on 134 of 149 items against 108 for the self-hosted fusion.

**Where Cortex loses: ICD-10 coding questions, and multi-document asks.** The items on which the
self-hosted retriever beats Cortex are mostly in the `cc` stratum: `g-cc-019` ("which conditions
have a three-character code that takes no further character", relevant `coding-hypertension-i10`)
is 1.0 self-hosted and 0.0 on m-v1.5; `g-cc-023` and `g-cc-024` (code roots and decimal characters)
drop from 1.0 to 0.5 on m-v1.5, and l-v2.0 drops `g-cc-013`, `g-cc-017`, `g-cc-018` and `g-cc-021`
to 0.5 instead. Per stratum, self-hosted scores 0.972 on `cc` against 0.922 (m) and 0.906 (l) for
Cortex, and Cortex scores 0.883-0.917 on `su` against 0.783. Consilium's BM25 tokenizer was written
to keep code-like tokens such as `I10` and `K21` intact and its RRF weighting was tuned on this
corpus; Cortex's lexical component and fusion were not, and the large model loses slightly more on
`cc` than the medium one while gaining more on `su`, which is the trade one would expect from a
model that is better at meaning and no better at exact tokens. The remaining losses are
multi-document items where Cortex finds the condition note but not the companion guideline or code
note within the top 5 (`g-md-005`, `g-md-010`, `g-md-012`), and one genuine miss: m-v1.5 ranks
`red-flag-meningitis-signs` above `red-flag-sepsis` for `g-su-013` (rigors and a temperature) and
leaves sepsis out of its top 5, where both the self-hosted retriever and l-v2.0 keep it.

**Where both fail.** `g-su-005` (a stroke symptom buried in a question about blood-pressure tablets,
with "speach" misspelled) and `g-su-017` (suicidal ideation phrased without any of the words a note
would use) are zero-hit on every row. No retriever in this table finds them, which is consistent
with the main README's finding that red-flag handling cannot be left to retrieval.

The two Cortex models disagree on 11 items and split them; the large model's extra 0.7 points of
recall@5 come from `su` and `md` and cost it 0.5 points of MRR@10. For a system whose failures that
matter are the buried red flags, the large model is the better choice; for one that is mostly asked
coding questions, the self-hosted lexical path is still worth keeping in the fusion.

## Reproducing it

```bash
uv sync --extra cortex                                  # snowflake, snowflake-snowpark-python, pandas, pyarrow
# ~/.snowflake/connections.toml: a [consilium] connection; nothing in the repo holds a credential
uv run python -m eval.cortex.load_corpus                # 312 chunks -> CONSILIUM.EVAL.CHUNKS via Snowpark
# run eval/cortex/services.sql in Snowsight; wait for both services to show indexing_state ACTIVE
uv run python -m eval.cortex.run_eval                   # both services -> results/<date>/cortex_*.json
uv sync --extra embeddings --extra cortex               # bge-small for the like-for-like row
uv run python -m eval.cortex.replay_self_hosted         # -> results/<date>/self_hosted_bge.json
```

The optional "routed-filter" variant described in the exercise brief (replicating the agents'
category filters on Cortex via `@eq` filters on `CATEGORY`) was not run. Every agent holds
`search_knowledge`, which has no category filter, so the per-route ceiling derived from
`data/policy.yaml` is the whole corpus for every route; reproducing Failure Case 2 needs the
per-skill-call filter from the traces, which is what the published-run row already measures.
