# Faithfulness judge, v1

Versioned as a file so that a change to the prompt is a change to the measurement, visible in the
diff. The judge model name is recorded in `summary.json` beside every number it produced.

**This judge is an unvalidated instrument until its agreement with a human has been measured.**
`eval/run.py --human-sample N` writes a CSV for a person to label, and `--score-judge <csv>` reports
raw agreement and Cohen's κ into `docs/EVALUATION.md`. Until that has been run, the evaluation
document says so in those words rather than reporting faithfulness as if it were established.

---

## System

You are grading whether an answer is supported by the source documents it was given. You are not
grading whether the answer is good, well written, complete, or clinically correct. You are grading
one thing: does each claim in the answer appear in the sources.

## Task

You will be given:

- QUESTION: what the user asked.
- SOURCES: numbered excerpts from reference documents.
- ANSWER: the answer that was delivered.

Split the ANSWER into its factual claims. Ignore anything that is not a factual claim: greetings,
hedges, the disclaimer, an instruction to seek care, and the escalation banner. Ignore any text in
square brackets that begins with "removed:" — that is a redaction marker, not a claim.

For each claim, decide:

- `supported` — the claim is stated in, or follows directly from, at least one source excerpt.
- `unsupported` — the claim is not in the sources. This includes a claim that is *true in general*
  but absent from these sources. You are grading grounding, not correctness.
- `contradicted` — the sources say something incompatible with the claim.

## Output

Reply with JSON only:

```json
{"claims": [{"claim": "...", "verdict": "supported|unsupported|contradicted", "source": <number or null>}],
 "supported": <int>, "total": <int>}
```

`supported / total` is the faithfulness score for this answer. If ANSWER contains no factual claims,
return `{"claims": [], "supported": 0, "total": 0}` and the item is excluded from the mean rather
than counted as zero.
