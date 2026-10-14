# LangGraph orchestrator: evaluation

> **Not medical advice.** This is an educational software project. It does not diagnose, treat, or
> provide clinical guidance, and it must not be used for real medical decisions. No patient data of
> any kind may be used with it.

Five sweeps of the 150-item golden set on 2026-10-14, all with `claude-haiku-5-5` as agent model and
as judge, compare the legacy planner–worker–synthesizer router with the LangGraph orchestrator in
its two versions. Every number here is read from the runs' `summary.json` files and traces by
`eval/graph_compare.py`; the raw outputs are committed under `eval/results/published-haiku-*/`
with sha256 manifests. Nothing was recomputed by a second implementation and nothing was typed in.

## Protocol

| | published baseline (2026-08-30) | these runs (2026-10-14) |
|---|---|---|
| agent model / judge model | `gpt-4o-mini` / `gpt-4o-mini` | `claude-haiku-5-5` / `claude-haiku-5-5` |
| configuration | `full` (planner routing, `max_tool_calls=2`, `max_iterations=3`) | same preset, `--config full` |
| retriever | bge-small-en-v1.5 + BM25, RRF, Chroma | (a) the offline `HashEmbedder`/`NumpyStore` pipeline; (b) the same bge/Chroma index as the baseline, built on the author's machine and copied in |
| items | 150 golden, 29 multi-turn conversations | same files, same labels |
| orchestrator | `legacy` | `legacy`, `langgraph` v1, `langgraph` v2, each as a separate process |
| judge prompt | `faithfulness_v2`, `multiturn_v1` | same |
| Anthropic `max_tokens` | n/a | `CONSILIUM_ANTHROPIC_MAX_TOKENS=8192` (see Migration findings) |
| spend cap | `--max-cost 6.00` | `--max-cost 3.00` per run, never reached |
| where | the author's Mac | a Linux sandbox; sequential items, two runs at a time |

The retriever row exists because the first paired run had to use the offline pipeline (the
sandbox could not reach Hugging Face), and the bge pair was run once the weights were copied in.
The hash runs are kept because they are a second, independent paired measurement of the same
orchestration difference; their retrieval numbers are not comparable to the bge rows.

Reading notes, as in `docs/EVALUATION.md`: `relevant_doc_ids` (144/150) and `reference_answer`
(148/150) are machine-written references, so recall@5 and faithfulness are measured against that
reference on every row; red-flag recall and routing accuracy are hand-labelled. p95 is
`eval.metrics.percentile` over the 150 `turn.wall_ms` values, computed by `eval/graph_compare.py`;
p50 and p90 are the harness's. Cost is the sum of traced `llm_call` tokens at `eval/pricing.yaml`
rates — `claude-haiku-5-5` $0.10 / $0.50 per million tokens, platform.claude.com on 2026-10-13 —
and excludes the judge's untraced calls. Routing accuracy is not applicable to the graph (no
specialists, no `route` event).

## Results, bge/Chroma retriever

| metric | `full`, published (gpt-4o-mini) | legacy | LangGraph v1 | LangGraph v2 |
|---|---|---|---|---|
| items with a `turn` event | 150 | 150 | 150 | 150 |
| retrieval recall@5 (n=149) | 0.857 | **0.966** | 0.884 | 0.941 |
| hit@5 / MRR@10 | 0.899 / 0.756 | 0.987 / 0.885 | 0.946 / 0.879 | 0.960 / 0.834 |
| faithfulness, retrieved sources (judge mean, n judged) | 0.737 (148) | **0.810** (147) | 0.752 (148) | not measured (judge cut off at 49; see below) |
| faithfulness, oracle sources | 0.713 | 0.657 | 0.626 | not measured |
| red-flag recall, post-repair (n=28) | 0.500 | 0.964 | **1.000** | **1.000** |
| red-flag false negatives | 14 items | `g-su-017` | none | none |
| model escalated unaided (pre-repair) | 0.500 | 0.929 | 1.000 | 1.000 |
| output violations / repairs per 100 turns | 104.0 / 104.0 | 105.3 / 105.3 | **0.0 / 0.0** | 0.7 / 0.7 |
| violations by rule | disclaimer 150, dosing 4, escalation 2 | disclaimer 150, dosing 5, escalation 2, diagnosis 1 | none | escalation 1 |
| routing accuracy / planner fallback | 0.867 / 0.000 | 0.893 / 0.000 | n/a | n/a |
| multi-turn resolved (judge, n graded turns) | 0.627 (83) | 0.561 (82) | **0.988** (83) | not measured (run cut off) |
| latency p50 / p90 / p95 | 3.98 / 7.37 / 7.74 s | 8.50 / 18.88 / 21.92 s | **7.27 / 10.34 / 11.86 s** | 7.84 / 11.47 / 13.06 s |
| cost per turn / per 150-item run | $0.00120 / $0.181 | $0.00252 / $0.378 | **$0.00162 / $0.243** | $0.00204 / $0.305 |
| tokens / LLM calls / tool calls per turn | 6714 / 3.71 / 1.71 | 16393 / 3.87 / 2.25 | 10705 / 2.85 / 1.06 | 14241 / 2.67 / 1.70 |
| tool calls by skill (150 items) | search 78, guideline 52, research 25, risk 51, symptoms 24, code 20, lifestyle 7 | search 92, guideline 73, research 42, risk 51, code 44, symptoms 24, lifestyle 11 | risk 57, guideline 38, code 32, search 25, lifestyle 7 | search 162 (150 forced + 12), risk 53, guideline 26, code 8, lifestyle 6 |
| traced spend, whole run | $0.531 (all five configurations) | $0.378 | $0.243 | $0.305 |
| raw data | `eval/results/published/` | `published-haiku-bge-legacy/` | `published-haiku-bge-langgraph-v1/` | not published (partial); rerun pending |

**The v2 run is incomplete.** The Anthropic account's credit balance ran out at 03:31 UTC, after
all 150 golden turns had finished and 49 of them had been judged; the remaining judge calls and all
132 multi-turn turns failed with `credit balance is too low`. Its golden-item rows above (recall,
hit, MRR, red-flag, violations, latency, cost, tokens, tool calls) are complete and valid; its
faithfulness and multi-turn cells are not reported, and the run is not published. The rerun is one
command (below) and about $0.45.

## Results, offline hash retriever (same orchestration difference, second measurement)

| metric | legacy | LangGraph v1 |
|---|---|---|
| retrieval recall@5 (n=149) | 0.946 | 0.791 |
| faithfulness, retrieved / oracle | 0.780 / 0.670 | 0.713 / 0.588 |
| red-flag recall (n=28) | 0.929 (`g-su-001`, `g-md-021`) | 0.929 (`g-su-001`, `g-md-024`) |
| output violations / repairs per 100 turns | 103.3 / 103.3 | 0.0 / 0.0 |
| routing accuracy / fallback | 0.900 / 0.000 | n/a |
| multi-turn resolved | 0.463 (82) | 0.975 (81) |
| latency p50 / p90 / p95 | 8.37 / 17.86 / 19.51 s | 7.18 / 10.18 / 11.40 s |
| cost per turn / per run | $0.00248 / $0.372 | $0.00160 / $0.240 |
| tokens / LLM calls / tool calls per turn | 16272 / 3.91 / 2.29 | 10573 / 2.85 / 1.09 |
| raw data | `published-haiku-hash-legacy/` | `published-haiku-hash-langgraph-v1/` |

The legacy orchestrator's 0.946 on the *hash* embedder, above the published bge 0.857, is a model
effect, not an index effect: `claude-haiku-5-5` makes 2.29 tool calls per turn against
`gpt-4o-mini`'s 1.71 (102 `search_knowledge` calls against 78), so more documents reach the union
that recall@5 is scored on.

## What the comparison says

1. **The repair loop works as designed.** Both graph versions hand the turn boundary an answer it
   has nothing to fix: 0.0 and 0.7 violations per 100 turns against 105.3. The whole of the legacy
   figure is the disclaimer (150 of 150 turns, because the legacy prompts never ask for it) plus a
   handful of dosing and escalation rules; the graph's second synthesis carries the disclaimer and
   removes the dosing sentences. The price is about one extra model call per turn, visible in the
   LLM-calls column (2.85 against the legacy 3.87 — still fewer, because there is no planner-of-
   specialists call and no merge).
2. **Red-flag recall is a model result, not an orchestration result.** 0.964–1.000 on every
   `claude-haiku-5-5` row against 0.500 on the published `gpt-4o-mini` `full` row. The legacy
   orchestrator on haiku still shows the published mechanism once (`g-su-017`: `assess_risk` finds
   no pattern, the diagnostic agent does not escalate); both graph versions escalated on all 28.
3. **v1 regressed retrieval, and the cause is structural.** Retrieval was optional: the planner
   made 1.06 tool calls per turn and chose `assess_risk` (57 calls) over `search_knowledge` (25).
   recall@5 0.884 against 0.966; on the hash retriever 0.791 against 0.946. Classification of the
   24 bge items where v1 differs from legacy: 2 improvements (`g-gh-020`, `g-su-017`); **11
   tool-selection misses** where the only call was `assess_risk`, which cannot reach a condition,
   guideline or lifestyle note (`g-gh-019`, `g-su-022`, `g-su-028`, `g-su-030`, `g-md-002`,
   `g-md-006`, `g-md-008`, `g-md-010`, `g-md-012`, `g-md-023`, `g-md-028`); **8 single-call
   partial coverage** on two-document questions answered through one category-filtered tool
   (`g-gh-001`, `g-gh-022`, `g-cc-001`, `g-cc-004`, `g-cc-013`, `g-cc-028`, `g-ge-014`,
   `g-md-013`); **3 retrieval-ranking misses** where the right tool ran and the note was not in
   its top 5 (`g-ge-020`, `g-su-005`, `g-su-006`; the two red-flag items still escalated). No
   tool-result misuse. Trace pairs: `published-haiku-bge-legacy/traces/full-<id>.json` and
   `published-haiku-bge-langgraph-v1/traces/full-<id>.json`.
4. **v2 closes most of it without touching retrieval quality or the prompts' substance.**
   `search_knowledge` runs on every turn with the raw question before the planner; the planner
   decides only on additional calls (12 further searches, 53 `assess_risk`, 26 `find_guideline`).
   recall@5 0.941, 13 items differ from legacy: 4 improvements (`g-gh-020`, `g-cc-022`,
   `g-cc-025`, and `g-su-017`, which v2 escalates and legacy does not); **4 tool-selection
   misses** where legacy's diagnostic agent also called `analyze_symptoms`, whose `condition`
   filter is what reached the note (`g-su-022`, `g-su-028`, `g-su-030`, `g-md-021`); **4 partial
   coverage** where legacy's fan-out made 2–6 calls and the graph 1–2 (`g-cc-011`, `g-cc-028`,
   `g-md-016`, `g-md-023`); **1 ranking miss** (`g-su-006`, `red-flag-anaphylaxis` outside the
   raw-question top 5; escalated regardless). The residual 2.5 points are fan-out width — three
   specialists each with a budget of two — not the index.
5. **Multi-turn resolution is where the graph's memory differs most.** The thread checkpoint keeps
   every prior (question, answer) pair verbatim; the legacy working memory replays five exchanges
   and compacts the rest. 0.988 against 0.561 on bge (0.975 against 0.463 on hash). This is also
   the graph's open cost problem on long conversations: nothing is compacted.
6. **Faithfulness is lower on the graph** (0.752 against 0.810 retrieved, 0.626 against 0.657
   oracle, v1), by a judge whose agreement with people is kappa 0.592. The direction is consistent
   across both retrievers. One plausible mechanism is in the traces: the graph's synthesizer sees
   every tool observation inside the MCP server's safety envelope and writes one longer answer
   (10705 tokens per turn against the legacy specialists' shorter grounded sections), so more
   claims per answer meet the same evidence. Not established; it needs the v2 judge pass and a
   claim-level read.

## Migration findings (things that broke, and what was changed)

- **`claude-haiku-5-5` rejects a transcript that ends on an assistant message** (`400: This model
  does not support assistant message prefill`). The v1 graph did this when the planner called no
  tool: its prose reply was the last message the synthesizer saw. The synthesizer now drops a
  trailing tool-less assistant message and answers the question itself; the legacy loop never
  ends a transcript that way. Commit `689d9a3`.
- **The judge was truncated.** The Anthropic provider's `max_tokens` default is 1500, sized for
  an answer; the faithfulness judge writes one JSON object per claim and hit `stop_reason=length`
  on 7 of the first 10 calls, which the parser scores as "could not be parsed" (`judge.no_json`).
  The OpenAI provider sends no cap. `CONSILIUM_ANTHROPIC_MAX_TOKENS` (default unchanged) was added
  and set to 8192 for every run here. Commit `689d9a3`.
- **Optional retrieval regressed recall** (finding 3); fixed structurally in v2 (`f5bae8a`), v1
  kept selectable with `CONSILIUM_GRAPH_VERSION=1`.
- **The legacy research agent returned an empty forced answer** 3 times per run on
  `claude-haiku-5-5` (`agent.empty_forced_answer`; delivered as the fixed fallback sentence). Not
  seen in the published gpt-4o-mini run. Not investigated.
- **`summary.json`'s `commit` is HEAD at write time, not at process start.** The hash runs record
  `5831975` and the bge runs `f5bae8a`; the code they imported was `e80a993` and `5831975`. The
  commits in between touch `eval/graph_compare.py`, `.gitignore` and the v2 graph, which these
  processes never loaded — every LangGraph trace in the v1 directories is planner-first.

## Runbook

```bash
uv sync --extra embeddings
# .env: CONSILIUM_PROVIDER=anthropic, CONSILIUM_MODEL=claude-haiku-5-5, ANTHROPIC_API_KEY=...,
#       CONSILIUM_ANTHROPIC_MAX_TOKENS=8192
uv run consilium ingest                                        # once, if data/chroma is absent

# the pending v2 rerun (complete judge + multi-turn), ~$0.45
ORCHESTRATOR=langgraph CONSILIUM_GRAPH_VERSION=2 uv run python -m eval.run --config full --max-cost 3.00
uv run python -m eval.graph_compare eval/results/<stamp> --baseline eval/results/published-haiku-bge-legacy
uv run python -m eval.publish --from eval/results/<stamp> --out eval/results/published-haiku-bge-langgraph-v2

# the other rows, for reference
ORCHESTRATOR=legacy    uv run python -m eval.run --config full --max-cost 3.00
ORCHESTRATOR=langgraph CONSILIUM_GRAPH_VERSION=1 uv run python -m eval.run --config full --max-cost 3.00
# offline retriever: add --embedder hash --store numpy
```

Where the Hugging Face hub is unreachable, point `HF_HUB_CACHE` at a directory holding
`models--BAAI--bge-small-en-v1.5` and set `HF_HUB_OFFLINE=1`.
