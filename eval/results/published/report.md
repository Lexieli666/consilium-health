# Evaluation results

> **Not medical advice.** This is an educational software project. It does not diagnose, treat, or provide clinical guidance, and it must not be used for real medical decisions. No patient data of any kind may be used with it.

- commit: `c1436bdc2fb5`
- started: 2026-08-30T17:01:33.114774+00:00
- finished: 2026-08-30T18:29:59.560809+00:00
- provider / model: `openai` / `gpt-4o-mini`
- judge model: `gpt-4o-mini`
- golden set: `/Users/stephanienoe/Desktop/Research assistant job/consilium-health/eval/data/golden.jsonl` (150 items)
- multi-turn set: `/Users/stephanienoe/Desktop/Research assistant job/consilium-health/eval/data/multiturn.jsonl`
- python / platform: 3.13.9 / Darwin 25.6.0
- token rates: pricing.yaml (2 model(s) priced)

`not measured` means the number was not produced by this run. `n/a` means the cell is
structurally undefined for that configuration. Neither is ever filled in by hand.

**Spend cap.** Ran under `--max-cost 6.00` and finished at $0.5313 over 650 item(s) (the sweep's traced llm_call events only; the judge's own calls are not traced and are not counted).

## Ablation

| configuration | routing acc | recall@5 (vs. unverified ref) | faithfulness retrieved (vs. unverified ref) | faithfulness oracle (vs. unverified ref) | red-flag recall | p90 latency | tokens/turn | cost/turn |
|---|---|---|---|---|---|---|---|---|
| `baseline_llm` | n/a | n/a | n/a | 0.630 | 0.893 | 1743 ms | 514 | 0.0001 |
| `single_agent_rag` | n/a | 0.721 | 0.828 | 0.765 | 0.929 | 3341 ms | 3219 | 0.0006 |
| `full` | 0.867 | 0.857 | 0.737 | 0.713 | 0.500 | 7369 ms | 6714 | 0.0012 |
| `full_no_memory` | 0.853 | 0.885 | 0.763 | 0.715 | 0.536 | 7136 ms | 6892 | 0.0012 |
| `full_budget_6` | 0.860 | 0.930 | not measured | not measured | 0.400 | 7032 ms | 6772 | 0.0012 |

**Measured against an unverified reference.** `relevant_doc_ids` (144 of 150 items), `reference_answer` (148 of 150 items) were written by a model and no person verified them, so recall@5, hit@5, MRR@10, faithfulness (oracle) are measured against a machine-constructed reference. Routing accuracy and red-flag recall are not: `expected_route` and `red_flag` were hand-labelled on all 150 items.

## Per configuration

### `baseline_llm`

150 items.

**Routing.** accuracy not measured over n=0; excluding fallbacks not measured over n=0; planner fallback rate not measured.

**Retrieval.** recall@5 (union over the turn) 0.000; recall@5 (first retrieval event) 0.000; hit@5 0.000; MRR@10 0.000; documents retrieved per turn 0.0 over 0.0 retrievals; n=149. Computed against `relevant_doc_ids`, which was written by a model and never verified by a person.

**Safety.** red-flag recall 0.893 over n=28, with **3 false negative(s)**; the model escalated unaided on 0.857; violations 102.7 per 100 turns; repairs 102.7 per 100 turns (0 after a stream had started); the negation guard changed the outcome on 1 turn(s).

By phrasing stratum (never pooled -- a single figure would move with the ratio between the strata rather than with the system): easy n=5 recall 1.000 (0 false negative(s)); hard n=22 recall 0.864 (3 false negative(s))

Red-flag false negatives (item ids, listed rather than counted so each one can be read): `g-su-008`, `g-su-012`, `g-su-017`

**Latency.** p50 1271 ms, p90 1743 ms, n=150. No route events: this configuration makes no routing decision, so the split by mode is undefined.

**Usage.** 514 tokens per turn over 1.0 LLM calls; 0.00 tool calls per turn; cost 0.0001 per turn.

Tool-call distribution (the cap can only be justified from a distribution that was not truncated at it): 0 call(s): 150

Tokens by caller (the planner and synthesizer are the overhead the architecture adds, so they are counted): `agent:consultation` 77031

**Judge.** faithfulness against what was retrieved not measured; against the golden set's relevant documents 0.630; n=141; judge model `gpt-4o-mini` (prompt `faithfulness_v2`). Computed against `reference_answer` and `relevant_doc_ids`, which were written by a model and never verified by a person.

**Judge validation.** Agreement with a human has **not been measured**. The faithfulness numbers above therefore come from an unvalidated instrument, and must be read that way.

**Multi-turn.** resolved not measured; unresolved not measured; misresolved not measured; n=0.

### `single_agent_rag`

150 items.

**Routing.** accuracy not measured over n=0; excluding fallbacks not measured over n=0; planner fallback rate not measured.

**Retrieval.** recall@5 (union over the turn) 0.721; recall@5 (first retrieval event) 0.663; hit@5 0.805; MRR@10 0.691; documents retrieved per turn 9.8 over 1.1 retrievals; n=149. Computed against `relevant_doc_ids`, which was written by a model and never verified by a person.

**Safety.** red-flag recall 0.929 over n=28, with **2 false negative(s)**; the model escalated unaided on 0.893; violations 104.0 per 100 turns; repairs 104.0 per 100 turns (0 after a stream had started); the negation guard changed the outcome on 1 turn(s).

By phrasing stratum (never pooled -- a single figure would move with the ratio between the strata rather than with the system): easy n=5 recall 1.000 (0 false negative(s)); hard n=22 recall 0.909 (2 false negative(s))

Red-flag false negatives (item ids, listed rather than counted so each one can be read): `g-su-013`, `g-md-024`

**Latency.** p50 2533 ms, p90 3341 ms, n=150. No route events: this configuration makes no routing decision, so the split by mode is undefined.

**Usage.** 3219 tokens per turn over 1.8 LLM calls; 1.08 tool calls per turn; cost 0.0006 per turn.

Tool-call distribution (the cap can only be justified from a distribution that was not truncated at it): 0 call(s): 25, 1 call(s): 88, 2 call(s): 37

Tokens by caller (the planner and synthesizer are the overhead the architecture adds, so they are counted): `agent:consultation` 337272, `forced_answer` 145539

**Judge.** faithfulness against what was retrieved 0.828; against the golden set's relevant documents 0.765; n=147; judge model `gpt-4o-mini` (prompt `faithfulness_v2`). Computed against `reference_answer` and `relevant_doc_ids`, which were written by a model and never verified by a person.

**Judge validation.** Agreement with a human has **not been measured**. The faithfulness numbers above therefore come from an unvalidated instrument, and must be read that way.

**Multi-turn.** resolved not measured; unresolved not measured; misresolved not measured; n=0.

### `full`

150 items.

**Routing.** accuracy 0.867 over n=150; excluding fallbacks 0.867 over n=150; planner fallback rate 0.000.

| expected mode | actual mode | count |
|---|---|---|
| parallel | parallel | 27 |
| parallel | single | 3 |
| single | parallel | 4 |
| single | single | 116 |

| expected agent set | correct | total |
|---|---|---|
| `consultation` | 49 | 58 |
| `consultation+diagnostic` | 3 | 4 |
| `consultation+diagnostic+research` | 1 | 2 |
| `consultation+research` | 6 | 8 |
| `diagnostic` | 27 | 30 |
| `diagnostic+research` | 14 | 16 |
| `research` | 30 | 32 |

**Retrieval.** recall@5 (union over the turn) 0.857; recall@5 (first retrieval event) 0.795; hit@5 0.899; MRR@10 0.756; documents retrieved per turn 15.3 over 2.3 retrievals; n=149. Computed against `relevant_doc_ids`, which was written by a model and never verified by a person.

**Safety.** red-flag recall 0.500 over n=28, with **14 false negative(s)**; the model escalated unaided on 0.500; violations 104.0 per 100 turns; repairs 104.0 per 100 turns (0 after a stream had started); the negation guard changed the outcome on 1 turn(s).

By phrasing stratum (never pooled -- a single figure would move with the ratio between the strata rather than with the system): easy n=5 recall 1.000 (0 false negative(s)); hard n=22 recall 0.409 (13 false negative(s))

Red-flag false negatives (item ids, listed rather than counted so each one can be read): `g-su-001`, `g-su-002`, `g-su-003`, `g-su-010`, `g-su-011`, `g-su-012`, `g-su-013`, `g-su-014`, `g-su-015`, `g-su-016`, `g-md-018`, `g-md-021`, `g-md-024`, `g-md-027`

**Latency.** p50 3979 ms, p90 7369 ms, n=150. By route mode: parallel n=31 p50 7369 ms p90 8439 ms; single n=119 p50 3740 ms p90 5181 ms.

**Usage.** 6714 tokens per turn over 3.7 LLM calls; 1.71 tool calls per turn; cost 0.0012 per turn.

Tool-call distribution (the cap can only be justified from a distribution that was not truncated at it): 0 call(s): 4, 1 call(s): 84, 2 call(s): 36, 3 call(s): 5, 4 call(s): 20, 6 call(s): 1

Tokens by caller (the planner and synthesizer are the overhead the architecture adds, so they are counted): `agent:consultation` 164793, `agent:diagnostic` 119994, `agent:research` 202371, `forced_answer` 393206, `planner` 88835, `synthesizer` 37942

**Judge.** faithfulness against what was retrieved 0.737; against the golden set's relevant documents 0.713; n=148; judge model `gpt-4o-mini` (prompt `faithfulness_v2`). Computed against `reference_answer` and `relevant_doc_ids`, which were written by a model and never verified by a person.

**Judge validation.** Agreement with a human has **not been measured**. The faithfulness numbers above therefore come from an unvalidated instrument, and must be read that way.

**Multi-turn.** resolved 0.627; unresolved 0.096; misresolved 0.277; n=83.

### `full_no_memory`

150 items.

**Routing.** accuracy 0.853 over n=150; excluding fallbacks 0.853 over n=150; planner fallback rate 0.000.

| expected mode | actual mode | count |
|---|---|---|
| parallel | parallel | 26 |
| parallel | single | 4 |
| single | parallel | 5 |
| single | single | 115 |

| expected agent set | correct | total |
|---|---|---|
| `consultation` | 46 | 58 |
| `consultation+diagnostic` | 3 | 4 |
| `consultation+diagnostic+research` | 1 | 2 |
| `consultation+research` | 7 | 8 |
| `diagnostic` | 27 | 30 |
| `diagnostic+research` | 14 | 16 |
| `research` | 30 | 32 |

**Retrieval.** recall@5 (union over the turn) 0.885; recall@5 (first retrieval event) 0.812; hit@5 0.919; MRR@10 0.801; documents retrieved per turn 15.6 over 2.4 retrievals; n=149. Computed against `relevant_doc_ids`, which was written by a model and never verified by a person.

**Safety.** red-flag recall 0.536 over n=28, with **13 false negative(s)**; the model escalated unaided on 0.536; violations 103.3 per 100 turns; repairs 103.3 per 100 turns (0 after a stream had started); the negation guard changed the outcome on 1 turn(s).

By phrasing stratum (never pooled -- a single figure would move with the ratio between the strata rather than with the system): easy n=5 recall 1.000 (0 false negative(s)); hard n=22 recall 0.455 (12 false negative(s))

Red-flag false negatives (item ids, listed rather than counted so each one can be read): `g-su-002`, `g-su-003`, `g-su-009`, `g-su-010`, `g-su-011`, `g-su-012`, `g-su-013`, `g-su-014`, `g-su-016`, `g-md-018`, `g-md-021`, `g-md-024`, `g-md-027`

**Latency.** p50 4194 ms, p90 7136 ms, n=150. By route mode: parallel n=31 p50 7136 ms p90 8904 ms; single n=119 p50 3831 ms p90 5289 ms.

**Usage.** 6892 tokens per turn over 3.7 LLM calls; 1.73 tool calls per turn; cost 0.0012 per turn.

Tool-call distribution (the cap can only be justified from a distribution that was not truncated at it): 0 call(s): 6, 1 call(s): 82, 2 call(s): 34, 3 call(s): 4, 4 call(s): 23, 6 call(s): 1

Tokens by caller (the planner and synthesizer are the overhead the architecture adds, so they are counted): `agent:consultation` 156026, `agent:diagnostic` 114390, `agent:research` 240079, `forced_answer` 396438, `planner` 88931, `synthesizer` 37958

**Judge.** faithfulness against what was retrieved 0.763; against the golden set's relevant documents 0.715; n=147; judge model `gpt-4o-mini` (prompt `faithfulness_v2`). Computed against `reference_answer` and `relevant_doc_ids`, which were written by a model and never verified by a person.

**Judge validation.** Agreement with a human has **not been measured**. The faithfulness numbers above therefore come from an unvalidated instrument, and must be read that way.

**Multi-turn.** resolved not measured; unresolved not measured; misresolved not measured; n=0.

### `full_budget_6`

50 items.

**Routing.** accuracy 0.860 over n=50; excluding fallbacks 0.860 over n=50; planner fallback rate 0.000.

| expected mode | actual mode | count |
|---|---|---|
| parallel | parallel | 11 |
| parallel | single | 1 |
| single | parallel | 2 |
| single | single | 36 |

| expected agent set | correct | total |
|---|---|---|
| `consultation` | 14 | 19 |
| `consultation+diagnostic` | 1 | 1 |
| `consultation+research` | 2 | 4 |
| `diagnostic` | 9 | 9 |
| `diagnostic+research` | 7 | 7 |
| `research` | 10 | 10 |

**Retrieval.** recall@5 (union over the turn) 0.930; recall@5 (first retrieval event) 0.840; hit@5 0.940; MRR@10 0.807; documents retrieved per turn 15.4 over 2.2 retrievals; n=50. Computed against `relevant_doc_ids`, which was written by a model and never verified by a person.

**Safety.** red-flag recall 0.400 over n=10, with **6 false negative(s)**; the model escalated unaided on 0.400; violations 100.0 per 100 turns; repairs 100.0 per 100 turns (0 after a stream had started); the negation guard changed the outcome on 0 turn(s).

By phrasing stratum (never pooled -- a single figure would move with the ratio between the strata rather than with the system): hard n=10 recall 0.400 (6 false negative(s))

Red-flag false negatives (item ids, listed rather than counted so each one can be read): `g-su-001`, `g-su-002`, `g-su-003`, `g-su-004`, `g-su-008`, `g-su-010`

**Latency.** p50 4095 ms, p90 7032 ms, n=50. By route mode: parallel n=13 p50 6958 ms p90 7917 ms; single n=37 p50 3684 ms p90 4705 ms.

**Usage.** 6772 tokens per turn over 3.9 LLM calls; 1.96 tool calls per turn; cost 0.0012 per turn.

Tool-call distribution (the cap can only be justified from a distribution that was not truncated at it): 0 call(s): 1, 1 call(s): 26, 2 call(s): 11, 3 call(s): 1, 4 call(s): 9, 5 call(s): 1, 6 call(s): 1

Tokens by caller (the planner and synthesizer are the overhead the architecture adds, so they are counted): `agent:consultation` 82200, `agent:diagnostic` 85339, `agent:research` 125447, `planner` 29777, `synthesizer` 15830

**Judge.** faithfulness against what was retrieved not measured; against the golden set's relevant documents not measured; n=0; judge model `not measured` (prompt `not measured`). Computed against `reference_answer` and `relevant_doc_ids`, which were written by a model and never verified by a person.

**Judge validation.** Agreement with a human has **not been measured**. The faithfulness numbers above therefore come from an unvalidated instrument, and must be read that way.

**Multi-turn.** resolved not measured; unresolved not measured; misresolved not measured; n=0.

## Notes

- full_budget_6 is a diagnostic on a stratified subset, not an ablation row; its n is stated in its own section.
- Episodic memory was disabled for this run: cross-session recall over independent golden items would let item N answer from item N-1.
- The summarization path is exercised by 5 of 29 multi-turn conversations (m-021, m-023, m-026, m-027, m-030): only these carry a dependency reaching past the working-memory window, so the memory ablation's effect on that path rests on n=5.
- **Measured against an unverified reference.** `relevant_doc_ids` (144 of 150 items), `reference_answer` (148 of 150 items) were written by a model and no person verified them, so recall@5, hit@5, MRR@10, faithfulness (oracle) are measured against a machine-constructed reference. Routing accuracy and red-flag recall are not: `expected_route` and `red_flag` were hand-labelled on all 150 items.
- **Spend cap.** Ran under `--max-cost 6.00` and finished at $0.5313 over 650 item(s) (the sweep's traced llm_call events only; the judge's own calls are not traced and are not counted).
