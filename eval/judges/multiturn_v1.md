# Multi-turn resolution judge, v1

Versioned as a file, for the same reason as the faithfulness judge: a change to the prompt is a
change to the measurement. Its agreement with a human is measured the same way, and is reported the
same way, and is `not measured` until it has been.

**Still v1 after the multi-referent extension of 2026-08-22, deliberately.** The rule that a prompt
change is a version change exists so that two numbers produced by different prompts are never
compared as if they were the same measurement. This prompt has produced no numbers: no sweep has
been run, `summary.json` records nothing from it, and `docs/EVALUATION.md` §6 reads `not measured`
throughout. A v2 whose v1 has no results would be a version history in which the first entry means
nothing. The first number this file produces will be the first number it has ever produced.

---

## System

You are grading whether an answer resolved a reference correctly. You are not grading the quality of
the answer.

## Task

You will be given:

- CONVERSATION: the turns before the one being graded, numbered from 0, in order.
- QUESTION: the turn being graded. It contains a pronoun or an ellipsis that only resolves against
  one or more earlier turns.
- REFERENT TURNS: the numbers of the earlier turns the reference resolves against, annotated by a
  human. There may be more than one.
- REFERENT: what the pronoun or ellipsis refers to, in the human's words. It is one description
  even when REFERENT TURNS names several turns — do not try to split it into parts and match them
  one to one against the turn numbers.
- ANSWER: the answer that was delivered.

Decide one of:

- `resolved` — the answer is about REFERENT.
- `unresolved` — the answer asks what the user means, or answers generically without committing to
  a referent.
- `misresolved` — the answer is about something else. This is the failure that matters, because it
  is the one a reader cannot detect from the answer alone.

Asking a clarifying question is `unresolved`, not `misresolved`, and it is not a wrong answer — it is
a different behaviour, and the two are counted separately.

## When REFERENT TURNS names more than one turn

Some questions resolve against several earlier turns at once: "does his age change what is
recommended?" needs both the turn that introduced the son and the turn that gave his age; "are those
two things significant?" needs both symptoms.

**The answer resolves the turn only if it accounts for every turn listed in REFERENT TURNS.** Read
each listed turn in CONVERSATION and check that the answer is about all of them together.

**Partial resolution is `misresolved`, not `resolved` and not `unresolved`.** An answer that picks
up some of the listed referents and silently drops the others has committed to a reading of the
question, and that reading is wrong — it answers a narrower question than the one that was asked.
It is `misresolved` for the same reason a wholly wrong referent is: the reader cannot tell from the
answer alone that something was dropped. It is not `unresolved`, because the answer did commit;
`unresolved` is for an answer that declines to commit at all.

Two things this does **not** mean:

- It does not require the answer to enumerate the referents or to name the turn numbers. A single
  sentence that is genuinely about all of them together resolves the turn.
- It does not require the answer to be *right*. You are grading what the answer is about, not
  whether its content is correct.

## Output

Reply with JSON only:

```json
{"verdict": "resolved|unresolved|misresolved", "why": "<one sentence>"}
```

Where the verdict is `misresolved` because of a dropped referent, say in `why` which one was
dropped.
