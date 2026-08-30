# The published evaluation run

> **Not medical advice.** This is an educational software project. It does not diagnose, treat, or
> provide clinical guidance, and it must not be used for real medical decisions. No patient data of
> any kind may be used with it.

This directory is the evidence behind every number in `README.md`, `docs/EVALUATION.md` and
`docs/SAFETY.md`. It is the only path under `eval/results/` that git tracks; the timestamped run
directories the harness writes are ignored. A published number whose evidence is gitignored is a
number no reviewer can check, which is the whole reason this directory exists.

## Layout

| path | what it is |
|---|---|
| `summary.json` | every metric, plus the commit, dates, provider, model, judge model, platform and spend cap. **Copied byte for byte from the run. Never regenerated.** |
| `report.md` | the rendered report for the same run. **Copied byte for byte. Never regenerated.** |
| `traces/<session_id>.json` | one file per session: `{"session_id", "turns": [{"turn_index", "events": [...]}]}`, every trace event verbatim, turns in numeric order. 679 files. |
| `MANIFEST.json` | sha256 of every file above. This README is excluded — it describes the layout rather than being evidence. |

The harness writes traces as `runs/<session_id>/<turn_index>.jsonl`, which is right for appending
during a turn and wrong for reading afterwards. Publication folds each session into one file so
that a reviewer chasing one golden item does not have to know that a session is a directory.
Nothing is dropped, summarized or reordered: `turn_index` survives as a field rather than as a
filename, and the events are the same objects the runner wrote.

Session ids are `<config>-<item_id>` for the golden set (`full-g-su-001`) and
`<config>-mt-<conversation_id>` for the multi-turn set (`full-mt-m-021`). 150 items × 4 ablation
configurations, plus the 50-item `full_budget_6` diagnostic, plus 29 multi-turn conversations.

## Reproducing and checking it

```bash
python -m eval.publish --from eval/results/<timestamp>   # build this directory from a run
python -m eval.publish --verify                          # recompute every digest in MANIFEST.json
python -m eval.publish --judge-volume                    # the judge's untraced input, reconstructed
python -m eval.publish --sizing-replay                   # replay the A3 sizing slice
```

`--verify` is also asserted by `tests/test_eval_publish.py`, so an edited trace or a regenerated
`summary.json` fails the suite rather than quietly changing what a published number refers to.

The last two commands recompute the figures in `docs/EVALUATION.md` §5.3 that `eval/run.py` did not
produce: the judge talks to the provider directly and nothing traces it, so its volume has to be
reconstructed from the prompts it would have sent. Those are reconstructions and the document says
so; they are shipped as code so that the numbers in the document can be recomputed by anyone
holding this repository and nothing else.

## What this run is, and what it is not

- 650 golden turns and 132 multi-turn turns at commit `c1436bd`, `openai` / `gpt-4o-mini`, judged by
  `gpt-4o-mini` with `faithfulness_v2`, under `--max-cost 6.00`, finishing at $0.5313 of traced
  spend. The cap did not fire.
- `recall@5`, `hit@5`, `MRR@10` and both faithfulness columns are measured **against a
  machine-constructed reference**: `relevant_doc_ids` and `reference_answer` were written by a model
  and no person verified them (144 and 148 of the 150 items). Routing accuracy and red-flag recall
  are not — `expected_route` and `red_flag` were hand-labelled on all 150 items, blind.
- **`summary.json` and `report.md` name the operator's local golden-set path**, which includes a
  home directory. They are published byte for byte, so nothing was edited out; the traces contain no
  paths and no credentials. Whether that is acceptable in a public repository is the owner's call at
  the pre-push review, and `CLAUDE.md` §16 lists the options.
- `report.md`'s judge paragraph says agreement with a human has **not been measured**. That is true
  of the sweep, which does not read the validation file; it is not true of the judge. Two blind
  validation rounds were run and the second measured Cohen's kappa **0.592** (n=40), 0.008 below the
  0.6 usability line. `docs/EVALUATION.md` §4.1–4.2 is the record, and every faithfulness number
  published anywhere carries that kappa beside it.
