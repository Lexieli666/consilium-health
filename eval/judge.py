"""The LLM judge, its prompts, and the two-command loop that validates it.

**A judge whose agreement with a human was never measured is an unvalidated instrument**, and
reporting faithfulness from it without saying so is the same error as writing an unmeasured number
into the README.  This module therefore produces two things: the judge's scores, and the machinery
for measuring how far those scores can be trusted.

The loop is two commands, both in ``eval/run.py``:

1. ``--human-sample N`` writes ``judge_sample.csv``: the judge's label, the judge's rationale,
   **the evidence the judge was shown**, and an empty ``human_label`` column.
2. ``--score-judge <csv>`` reads the completed file and reports raw agreement and Cohen's kappa.

The evidence column is not decoration.  A labeller given only a list of ``doc_id`` values is
answering a harder question than the judge answered -- theirs includes "find and open the right
note" -- and the disagreement that produces is disagreement about evidence access, which kappa
would report as unreliability of the judge.  ``numbered_sources`` is therefore the one place the
source block is formatted, and both the judge's prompt and the CSV read it.

Until step 2 has been run, ``docs/EVALUATION.md`` says the judge is unvalidated **in those words**.
It has now been run twice (``docs/EVALUATION.md`` §4.1 and §4.2), so that is no longer what it says:
round 2 measured the prompt in force at **Cohen's kappa 0.592 on a blind sample of 40**, which is
0.008 below the 0.6 usability line, and the owner's decision of 2026-08-30 was to publish
faithfulness with that number attached rather than revise a third time.  A measured instrument that
fell short of the line is a different claim from an unvalidated one, and every faithfulness number
carries the kappa instead of the word.

Prompts are files in ``eval/judges/``, versioned by filename, and the version is recorded in
``summary.json`` beside every number it produced.  A prompt change is a change to the measurement,
and it belongs in a diff.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from consilium.llm.base import LLMProvider, Message
from consilium.log import get_logger
from consilium.router.planner import extract_json_object

log = get_logger(__name__)

JUDGE_DIR = Path(__file__).parent / "judges"

#: The faithfulness prompt in force.  ``v1`` produced the round-1 validation numbers
#: (``docs/EVALUATION.md`` §4.1: raw agreement 0.675, kappa 0.350, below the 0.4 line) and stays on
#: disk unedited, because the version that produced a number has to remain readable beside it.
#: ``v2`` is the revision written against round 1's two failure modes, and it was re-validated on a
#: **fresh, disjoint** sample -- drawn with ``--sample-seed`` and ``--exclude-sample`` so it could
#: not reuse the items the prompt was revised against.  Round 2 (§4.2): raw agreement 0.800, kappa
#: **0.592**, 0.008 short of the 0.6 usability line.  The owner invoked the procedure's low-kappa
#: reporting clause on 2026-08-30, so ``v2`` stays exactly as it is and the kappa is published
#: beside every faithfulness number instead.  Editing this file would make §4.2 cite a prompt that
#: no longer says what the judge was told.
FAITHFULNESS_PROMPT = "faithfulness_v2"
MULTITURN_PROMPT = "multiturn_v1"

#: Columns of ``judge_sample.csv``, in the order they are written.  ``human_notes`` and
#: ``human_label`` are the two the person fills in, and ``human_label`` is **last** because it is
#: the only one the scorer reads: a scratch column placed after it would invite a labeller to stop
#: at the notes and leave the label blank, and a blank label is skipped rather than counted.
#:
#: ``sources_text`` sits next to ``retrieved_doc_ids`` and carries the same excerpts the judge was
#: given, numbered the same way.  The ids stay because they are what the golden set labels and what
#: a reviewer cross-references; the text is what makes the two raters answer the same question.
SAMPLE_COLUMNS = (
    "item_id",
    "question",
    "answer",
    "retrieved_doc_ids",
    "sources_text",
    "judge_label",
    "judge_rationale",
    "human_notes",
    "human_label",
)


class JudgeError(RuntimeError):
    """Raised when a judge prompt is missing or a completed sample cannot be scored."""


@dataclass(frozen=True)
class FaithfulnessVerdict:
    """One answer's claim-level grading."""

    supported: int
    total: int
    rationale: str

    @property
    def score(self) -> float | None:
        """``None``, not zero, when the answer made no factual claims.

        An answer that is entirely an escalation banner and a disclaimer has nothing to ground.
        Scoring it zero would punish the safest possible output on the metric that measures
        grounding, and the item is excluded from the mean instead.
        """
        return self.supported / self.total if self.total else None


@dataclass(frozen=True)
class MultiturnVerdict:
    """One later turn's reference resolution."""

    verdict: str
    why: str


def load_prompt(name: str) -> str:
    """Read a versioned judge prompt from ``eval/judges/``."""
    path = JUDGE_DIR / f"{name}.md"
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise JudgeError(f"cannot read the judge prompt at {path}: {exc}") from exc


def numbered_sources(sources: Sequence[tuple[str, str]]) -> str:
    """The evidence block, formatted the one way the judge is ever shown it.

    ``sources`` is ``(doc_id, text)`` in rank order.  This is a function rather than an expression
    inside :meth:`Judge.faithfulness` because ``judge_sample.csv`` has to carry the **same** string
    the row's verdict was produced from.  A second copy of the formatting would be free to drift,
    and a human would then be grading evidence the judge never saw while the resulting kappa was
    reported as agreement about identical items.
    """
    return "\n\n".join(
        f"[{index}] ({doc_id})\n{text}" for index, (doc_id, text) in enumerate(sources, start=1)
    )


def _system_block(prompt: str) -> str:
    """The prompt body below its explanatory header.

    The files are written for a human first -- they explain why the judge exists and how it is
    validated -- and the model only needs what follows the ``---`` separator.
    """
    _, _, body = prompt.partition("\n---\n")
    return (body or prompt).strip()


class Judge:
    """Grades answers with an LLM, recording which model and which prompt version did it."""

    def __init__(self, provider: LLMProvider, *, model_label: str | None = None) -> None:
        self.provider = provider
        self.model = model_label or provider.model

    async def faithfulness(
        self, *, question: str, answer: str, sources: Sequence[tuple[str, str]]
    ) -> FaithfulnessVerdict | None:
        """Grade one answer against numbered source excerpts.

        ``sources`` is ``(doc_id, text)`` in rank order.  Returns ``None`` when the judge could not
        be parsed -- a judge failure is not a faithfulness failure, and scoring it zero would let a
        flaky judge look like an ungrounded system.
        """
        numbered = numbered_sources(sources)
        payload = await self._ask(
            FAITHFULNESS_PROMPT,
            f"QUESTION:\n{question}\n\nSOURCES:\n{numbered or '(none)'}\n\nANSWER:\n{answer}",
        )
        if payload is None:
            return None
        try:
            supported = int(str(payload["supported"]))
            total = int(str(payload["total"]))
        except (KeyError, TypeError, ValueError):
            log.warning("judge.faithfulness_unparsed")
            return None
        claims = payload.get("claims", [])
        return FaithfulnessVerdict(
            supported=supported,
            total=total,
            rationale=json.dumps(claims)[:2000] if isinstance(claims, list) else "",
        )

    async def multiturn(
        self,
        *,
        conversation: Sequence[str],
        question: str,
        referent: str,
        referent_turns: Sequence[int],
        answer: str,
    ) -> MultiturnVerdict | None:
        """Grade whether a later turn resolved its annotated referent, or all of them.

        ``referent_turns`` is the label's turn indices and ``referent`` is the labeller's one
        description of them.  Both are passed, and the description is passed **whole**: eleven of
        the labelled turns name several earlier turns, and the labeller's prose does not decompose
        onto them one-to-one -- "sleeping badly and waking around 4 a.m." is one phrase for two
        turns, "father's acute confusion, urinary retention, possible fever, and living alone" is
        four clauses for four.  Splitting it on punctuation to line the parts up with the indices
        would be a machine inventing the parts of a hand-written label.  So the judge gets the
        indices, gets the numbered transcript to look them up in, and is told in the prompt that an
        answer resolves the turn only if it accounts for all of them.
        """
        history = "\n".join(f"[{index}] {turn}" for index, turn in enumerate(conversation))
        numbers = ", ".join(str(index) for index in referent_turns) or "(none)"
        payload = await self._ask(
            MULTITURN_PROMPT,
            f"CONVERSATION:\n{history}\n\nQUESTION:\n{question}\n\n"
            f"REFERENT TURNS:\n{numbers}\n\nREFERENT:\n{referent}\n\nANSWER:\n{answer}",
        )
        if payload is None:
            return None
        verdict = str(payload.get("verdict", ""))
        if verdict not in ("resolved", "unresolved", "misresolved"):
            log.warning("judge.multiturn_unknown_verdict", verdict=verdict)
            return None
        return MultiturnVerdict(verdict=verdict, why=str(payload.get("why", "")))

    async def _ask(self, prompt_name: str, user: str) -> dict[str, object] | None:
        """One judge call.  Never raises: a judge outage must not end a paid sweep."""
        try:
            response = await self.provider.chat(
                [
                    Message(role="system", content=_system_block(load_prompt(prompt_name))),
                    Message(role="user", content=user),
                ],
                tools=None,
            )
        except Exception:
            log.exception("judge.provider_failed", prompt=prompt_name)
            return None

        raw = extract_json_object(response.content or "")
        if raw is None:
            log.warning("judge.no_json", prompt=prompt_name)
            return None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("judge.invalid_json", prompt=prompt_name)
            return None
        return parsed if isinstance(parsed, dict) else None


# --------------------------------------------------------------------------------------------
# Judge validation
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SampleRow:
    """One row of ``judge_sample.csv``.

    The field order is the column order, and ``write_sample`` reads the columns off
    :data:`SAMPLE_COLUMNS` by name, so the two cannot drift into disagreement silently: a column
    with no matching field fails at write time rather than shipping an empty column a labeller
    would dutifully leave empty.
    """

    item_id: str
    question: str
    answer: str
    retrieved_doc_ids: str
    sources_text: str
    judge_label: str
    judge_rationale: str
    human_notes: str = ""
    human_label: str = ""


def write_sample(path: Path, rows: Sequence[SampleRow]) -> None:
    """Write the CSV a person fills in.  ``human_label`` is the last column, and empty."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(SAMPLE_COLUMNS))
        writer.writeheader()
        for row in rows:
            writer.writerow({column: getattr(row, column) for column in SAMPLE_COLUMNS})


@dataclass(frozen=True)
class Agreement:
    """Raw agreement and Cohen's kappa between the judge and a human."""

    n: int
    raw_agreement: float | None
    cohens_kappa: float | None
    labels: tuple[str, ...] = ()


def score_sample(path: Path) -> Agreement:
    """Read a completed sample and compute agreement.

    Rows with an empty ``human_label`` are skipped, not counted as disagreements: a partially
    labelled file is a partially labelled file, and treating the blanks as data would make the
    agreement number depend on how far the labeller got.
    """
    try:
        with Path(path).open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except OSError as exc:
        raise JudgeError(f"cannot read the judge sample at {path}: {exc}") from exc

    pairs = [
        (str(row.get("judge_label", "")).strip(), str(row.get("human_label", "")).strip())
        for row in rows
        if str(row.get("human_label", "")).strip()
    ]
    if not pairs:
        raise JudgeError(
            f"{path}: no rows have a human_label. Fill in the last column before scoring."
        )
    return agreement(pairs)


def agreement(pairs: Sequence[tuple[str, str]]) -> Agreement:
    """Cohen's kappa for two raters over the labels that actually appear.

    Kappa and not raw agreement alone: with a skewed label distribution -- most answers faithful --
    two raters who both mostly say "faithful" agree 90% of the time by chance, and a raw number
    would read as a validated judge.
    """
    if not pairs:
        return Agreement(n=0, raw_agreement=None, cohens_kappa=None)

    total = len(pairs)
    observed = sum(1 for judge, human in pairs if judge == human) / total
    labels = tuple(sorted({label for pair in pairs for label in pair}))

    expected = 0.0
    for label in labels:
        judge_share = sum(1 for judge, _ in pairs if judge == label) / total
        human_share = sum(1 for _, human in pairs if human == label) / total
        expected += judge_share * human_share

    kappa = None if expected >= 1.0 else (observed - expected) / (1.0 - expected)
    return Agreement(n=total, raw_agreement=observed, cohens_kappa=kappa, labels=labels)
