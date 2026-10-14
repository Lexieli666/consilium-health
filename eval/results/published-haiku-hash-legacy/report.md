# Evaluation results

> **Not medical advice.** This is an educational software project. It does not diagnose, treat, or provide clinical guidance, and it must not be used for real medical decisions. No patient data of any kind may be used with it.

- commit: `5831975176fa`
- started: 2026-10-14T00:00:53.143374+00:00
- finished: 2026-10-14T01:39:10.328417+00:00
- provider / model: `anthropic` / `claude-haiku-5-5`
- judge model: `claude-haiku-5-5`
- golden set: `/home/claude/consilium-health/eval/data/golden.jsonl` (150 items)
- multi-turn set: `/home/claude/consilium-health/eval/data/multiturn.jsonl`
- python / platform: 3.13.16 / Linux 6.18.44-fc-v114
- token rates: pricing.yaml (4 model(s) priced)

`not measured` means the number was not produced by this run. `n/a` means the cell is
structurally undefined for that configuration. Neither is ever filled in by hand.

**Spend cap.** Ran under `--max-cost 3.00` and finished at $0.3722 over 150 item(s) (the sweep's traced llm_call events only; the judge's own calls are not traced and are not counted).

## Ablation

| configuration | routing acc | recall@5 (vs. unverified ref) | faithfulness retrieved (vs. unverified ref) | faithfulness oracle (vs. unverified ref) | red-flag recall | p90 latency | tokens/turn | cost/turn |
|---|---|---|---|---|---|---|---|---|
| `full` | 0.900 | 0.946 | 0.780 | 0.670 | 0.929 | 17864 ms | 16272 | 0.0025 |

**Measured against an unverified reference.** `relevant_doc_ids` (144 of 150 items), `reference_answer` (148 of 150 items) were written by a model and no person verified them, so recall@5, hit@5, MRR@10, faithfulness (oracle) are measured against a machine-constructed reference. Routing accuracy and red-flag recall are not: `expected_route` and `red_flag` were hand-labelled on all 150 items.

## Per configuration

### `full`

150 items.

**Routing.** accuracy 0.900 over n=150; excluding fallbacks 0.900 over n=150; planner fallback rate 0.000.

| expected mode | actual mode | count |
|---|---|---|
| parallel | parallel | 26 |
| parallel | single | 4 |
| single | parallel | 4 |
| single | single | 116 |

| expected agent set | correct | total |
|---|---|---|
| `consultation` | 53 | 58 |
| `consultation+diagnostic` | 3 | 4 |
| `consultation+diagnostic+research` | 1 | 2 |
| `consultation+research` | 7 | 8 |
| `diagnostic` | 28 | 30 |
| `diagnostic+research` | 14 | 16 |
| `research` | 29 | 32 |

**Retrieval.** recall@5 (union over the turn) 0.946; recall@5 (first retrieval event) 0.798; hit@5 0.966; MRR@10 0.758; documents retrieved per turn 20.2 over 3.3 retrievals; n=149. Computed against `relevant_doc_ids`, which was written by a model and never verified by a person.

**Safety.** red-flag recall 0.929 over n=28, with **2 false negative(s)**; the model escalated unaided on 0.893; violations 103.3 per 100 turns; repairs 103.3 per 100 turns (0 after a stream had started); the negation guard changed the outcome on 1 turn(s).

By phrasing stratum (never pooled -- a single figure would move with the ratio between the strata rather than with the system): easy n=5 recall 1.000 (0 false negative(s)); hard n=22 recall 0.909 (2 false negative(s))

Red-flag false negatives (item ids, listed rather than counted so each one can be read): `g-su-001`, `g-md-021`

**Latency.** p50 8375 ms, p90 17864 ms, n=150. By route mode: parallel n=30 p50 17864 ms p90 21440 ms; single n=120 p50 7943 ms p90 11222 ms.

**Usage.** 16272 tokens per turn over 3.9 LLM calls; 2.29 tool calls per turn; cost 0.0025 per turn.

Tool-call distribution (the cap can only be justified from a distribution that was not truncated at it): 1 call(s): 17, 2 call(s): 103, 3 call(s): 1, 4 call(s): 28, 6 call(s): 1

Tokens by caller (the planner and synthesizer are the overhead the architecture adds, so they are counted): `agent:consultation` 153742, `agent:diagnostic` 307999, `agent:research` 184044, `forced_answer` 1529499, `planner` 158905, `synthesizer` 106671

**Judge.** faithfulness against what was retrieved 0.780; against the golden set's relevant documents 0.670; n=148; judge model `claude-haiku-5-5` (prompt `faithfulness_v2`). Computed against `reference_answer` and `relevant_doc_ids`, which were written by a model and never verified by a person.

**Judge validation.** Agreement with a human has **not been measured**. The faithfulness numbers above therefore come from an unvalidated instrument, and must be read that way.

**Multi-turn.** resolved 0.463; unresolved 0.500; misresolved 0.037; n=82.

## Notes

- full_budget_6 is a diagnostic on a stratified subset, not an ablation row; its n is stated in its own section.
- Episodic memory was disabled for this run: cross-session recall over independent golden items would let item N answer from item N-1.
- The summarization path is exercised by 5 of 29 multi-turn conversations (m-021, m-023, m-026, m-027, m-030): only these carry a dependency reaching past the working-memory window, so the memory ablation's effect on that path rests on n=5.
- **Measured against an unverified reference.** `relevant_doc_ids` (144 of 150 items), `reference_answer` (148 of 150 items) were written by a model and no person verified them, so recall@5, hit@5, MRR@10, faithfulness (oracle) are measured against a machine-constructed reference. Routing accuracy and red-flag recall are not: `expected_route` and `red_flag` were hand-labelled on all 150 items.
- **Spend cap.** Ran under `--max-cost 3.00` and finished at $0.3722 over 150 item(s) (the sweep's traced llm_call events only; the judge's own calls are not traced and are not counted).
