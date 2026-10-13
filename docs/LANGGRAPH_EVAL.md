# LangGraph orchestrator: evaluation status and runbook

> **Not medical advice.** This is an educational software project. It does not diagnose, treat, or
> provide clinical guidance, and it must not be used for real medical decisions. No patient data of
> any kind may be used with it.

**Status (2026-10-14): the LangGraph run has not been executed.** The orchestrator is implemented
and tested offline (`tests/test_graph_orchestrator.py`, mock provider, in-process and stdio MCP),
but the environment the implementation was written in could not reach `api.openai.com`, so no
golden-set sweep, no judge pass and no LangSmith trace exist for it. Every number in the LangGraph
column below is therefore `not run`. The baseline columns are real: they are read from
`eval/results/published/summary.json` and its traces, and p95 is computed from those traces by
`eval/graph_compare.py`. Nothing in this file was estimated.

## The comparison as it stands

Produced by `uv run python -m eval.graph_compare eval/results/published-langgraph` (the LangGraph
column reads `not run` until that directory exists):

| metric | `baseline_llm` (legacy, published) | `single_agent_rag` (legacy, published) | `full` (legacy, published) | `full_no_memory` (legacy, published) | `full` (`ORCHESTRATOR=langgraph`) |
|---|---|---|---|---|---|
| items with a `turn` event | 150 | 150 | 150 | 150 | not run |
| retrieval recall@5 (union; n labelled) | 0.0% | 72.1% | 85.7% | 88.5% | not run |
| faithfulness, retrieved sources (judge mean) | n/a | 82.8% | 73.7% | 76.3% | not run |
| faithfulness, oracle sources (judge mean) | 63.0% | 76.5% | 71.3% | 71.5% | not run |
| red-flag recall, post-repair (n=28) | 89.3% | 92.9% | 50.0% | 53.6% | not run |
| red-flag false negatives | g-su-008, g-su-012, g-su-017 | g-su-013, g-md-024 | 14 items (see `summary.json`) | 13 items (see `summary.json`) | not run |
| output violations / repairs per 100 turns | 102.7 / 102.7 | 104.0 / 104.0 | 104.0 / 104.0 | 103.3 / 103.3 | not run |
| routing accuracy / planner fallback rate | n/a (no `route` event) | n/a (no `route` event) | 86.7% / 0.0% | 85.3% / 0.0% | not run |
| latency p50 | 1.27 s | 2.53 s | 3.98 s | 4.19 s | not run |
| latency p90 | 1.74 s | 3.34 s | 7.37 s | 7.14 s | not run |
| latency p95 (from `turn.wall_ms`, `eval/graph_compare.py`) | 1.84 s | 3.50 s | 7.74 s | 8.39 s | not run |
| cost per turn (traced `llm_call`s, `eval/pricing.yaml`) | $0.00013 | $0.00057 | $0.00120 | $0.00123 | not run |
| cost per run (cost per turn × 150 items) | $0.01935 | $0.08546 | $0.18052 | $0.18467 | not run |
| tokens / LLM calls / tool calls per turn | 514 / 1.00 / 0.00 | 3219 / 1.83 / 1.08 | 6714 / 3.71 / 1.71 | 6892 / 3.72 / 1.73 | not run |
| agent model (from `llm_call` events) | gpt-4o-mini-2024-07-18 | gpt-4o-mini-2024-07-18 | gpt-4o-mini-2024-07-18 | gpt-4o-mini-2024-07-18 | not run |
| judge model | gpt-4o-mini | gpt-4o-mini | gpt-4o-mini | gpt-4o-mini | not run |
| run (directory, commit, started) | `published`, `f19584a21cee`, 2026-08-30 | same | same | same | not run |

Reading notes for the baseline columns, carried over from `docs/EVALUATION.md`: `relevant_doc_ids`
(144/150) and `reference_answer` (148/150) are machine-written references no person verified, so
recall@5 and faithfulness are measured against that reference on every row, this one included.
Red-flag recall and routing accuracy are hand-labelled. p95 is not a harness output; it is the
harness's own `eval.metrics.percentile` applied to the 150 `turn.wall_ms` values of each
configuration, and the same function will be applied to the LangGraph traces.

The "best baseline" the LangGraph run is configured against is `full`: the headline architecture,
with `max_tool_calls=2`, `max_iterations=3`, `gpt-4o-mini` as agent model and as judge. `full` and
`full_no_memory` differ only in working memory, which has no effect on single-turn golden items
(each item is its own session), so both are shown.

## What the LangGraph row will and will not contain

- **Routing accuracy is n/a by construction.** The graph has one planner that selects tools, not
  specialists, and emits no `route` event. That is not a regression to classify; it is a metric
  that does not apply.
- **`max_iterations` does not apply.** The graph plans once; the legacy loop's "model keeps
  producing prose" guard has no counterpart. LLM calls per turn are 2 (planner + synthesizer) plus
  one per repair round, at most 4.
- **Violations/repairs per 100 turns mean the same thing on both rows:** what the turn boundary's
  validator found in the answer the orchestrator handed over. For the graph that is after up to two
  re-syntheses, so the disclaimer row (150/150 violations on every legacy configuration, because the
  legacy prompts never ask for it) is the place to expect the largest difference. The repair rounds
  themselves are in the run's log lines (`graph.repair_round`) and, when enabled, in LangSmith.
- **Tool-call events carry `transport: "mcp"`** and are forwarded from the MCP server's own trace
  files into the turn (see `consilium/graph/mcp_tools.py`); the server's per-result safety-envelope
  events are not forwarded, for the reason above.

## Runbook (your machine, repo root, ~45–60 min, under $1 at gpt-4o-mini list prices)

```bash
# 0. once: the real retrieval pipeline and the index
uv sync --extra embeddings
uv run consilium ingest                       # builds data/chroma from data/corpus (bge-small)

# 1. keys: .env is gitignored
printf 'CONSILIUM_PROVIDER=openai\nCONSILIUM_MODEL=gpt-4o-mini\nOPENAI_API_KEY=sk-...\n' >> .env
# optional: LANGSMITH_TRACING=true, LANGSMITH_API_KEY=..., LANGSMITH_PROJECT=consilium-health

# 2. acceptance: one real turn, trace printed
ORCHESTRATOR=langgraph uv run python -m consilium.graph "What are the red-flag symptoms for sepsis?"

# 3. smoke, then the sweep (same preset, model and judge as the published run; capped)
ORCHESTRATOR=langgraph uv run python -m eval.run --config full --limit 5 --max-cost 0.50
ORCHESTRATOR=langgraph uv run python -m eval.run --config full --max-cost 3.00
#    -> eval/results/<stamp>/{summary.json, report.md, runs/}

# 4. the comparison and the per-item deltas (prints markdown; paste the table into README.md)
uv run python -m eval.graph_compare eval/results/<stamp> > /tmp/langgraph-compare.md

# 5. publish the raw outputs in the repository's layout and commit them
uv run python -m eval.publish --from eval/results/<stamp> --out eval/results/published-langgraph
git add eval/results/published-langgraph && git commit -m "eval: ORCHESTRATOR=langgraph run <stamp>"
```

`eval/run.py` refuses the mock provider and refuses `--max-cost` for an unpriced model, so a
misconfigured run stops before it spends anything. The sweep is sequential by design (latency is a
reported number), so expect roughly 150 × (two to four gpt-4o-mini calls) plus the judge pass.

## Failure classification (to be done after the run)

`eval/graph_compare.py` prints, for every item where the LangGraph run did worse than `full` on
recall@5 or on red-flag escalation, the evidence and a *suggested* category under the project's
four: **routing miss** (the planner called no corpus-backed tool for an item that has relevant
documents), **retrieval miss** (a corpus tool ran and the relevant note was not in its top 5),
**tool-result misuse** (the red-flag table matched, the draft did not escalate and the repair did
not restore it), **unsupported answer** (cannot be read off a trace, since the judge's per-item
verdicts are not stored; the tool says "read the trace"). The suggestion is a starting point; the
classification that goes into the README must come from reading the named trace pair, and the
README entry must cite the trace path. No tuning is to follow the classification: the comparison
is one run, reported once.
