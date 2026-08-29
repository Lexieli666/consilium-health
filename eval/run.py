"""The evaluation runner.

**This file requires a live API key and is never invoked by ``pytest``.**  It is the one part of the
repository that costs money, and it is deliberately not importable from the test suite's default
path: the offline rule is ``pytest -m "not network"`` passes with no key, and a runner that ran
itself during collection would break it.

Everything it computes is computed by ``eval/metrics.py`` from trace events.  What lives here is
orchestration -- which items, which configurations, which judge -- and the two commands that
validate the judge.

Four commands, matching the brief:

``--config NAME``       run one preset instead of the ablation set.
``--limit N``           run the first N items.  For a smoke test before spending on the full sweep.
``--human-sample N``    write ``judge_sample.csv`` for a person to label.  Requires ``--config``.
``--score-judge PATH``  read a completed sample and report agreement and Cohen's kappa.

and two that exist only so a **second** validation round can be drawn:

``--sample-seed INT``      shuffle the draw with this seed instead of :data:`SAMPLE_SEED`.
``--exclude-sample CSV``   drop the item ids a prior ``judge_sample.csv`` already used.  Repeatable.

A judge prompt revised because a validation round failed must be re-scored on a **fresh** sample.
Scoring it against the items it was revised on measures how well the revision was fitted to those
items and nothing else, so the two flags are what make a re-validation round drawable at all: the
seed moves the shuffle, and the exclusion guarantees no round-1 item can be drawn again even if it
does.

``--human-sample`` requires ``--config`` because the ablation set's first preset is
``baseline_llm``, which retrieves nothing: a sample drawn from it would validate the judge on the
one configuration whose answers are ungrounded by construction.  The draw is stratified over the
five item-id blocks and seeded, and the seed and the method are written beside the CSV, because
"sampling method" is one of the things ``docs/EVALUATION.md`` has to state and a method nobody can
re-run is not a stated method.

**The golden set must be labelled.**  ``load_golden`` refuses a draft, so this runner cannot be
pointed at the file the system drafted for itself.  That is the point of the checkpoint: an eval set
the system wrote and scored itself against is worth nothing.

**And where a label was not verified, the run says so in its own output.**  The golden set's
``relevant_doc_ids`` and ``reference_answer`` are machine-written and were knowingly accepted
unverified; ``expected_route`` and ``red_flag`` were hand-labelled.  The counts go into
``summary.json`` as ``unverified_labels`` and into ``report.md``'s results table, so a reviewer
reading the committed evidence can tell which numbers rest on a machine-constructed reference
without opening the golden set.  A run against a fully hand-verified set produces an empty
``unverified_labels`` and the disclosure disappears on its own.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import random
import subprocess
import sys
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

from consilium.config import ABLATION_PRESETS, PRESETS, Settings, get_preset
from consilium.log import configure_logging, get_logger
from consilium.retrieval.corpus import Document
from consilium.runtime import Runtime, build_runtime
from eval.harness import ConversationRun, ItemRun, run_conversation, run_golden_item
from eval.items import (
    EvalDataError,
    GoldenItem,
    MultiturnConversation,
    load_golden,
    load_multiturn,
    unverified_item_count,
    unverified_label_counts,
)
from eval.judge import (
    FAITHFULNESS_PROMPT,
    Judge,
    JudgeError,
    SampleRow,
    numbered_sources,
    score_sample,
    write_sample,
)
from eval.metrics import (
    latency_report,
    retrieval_outcome,
    retrieval_report,
    routing_outcome,
    routing_report,
    safety_report,
    usage_report,
)
from eval.report import (
    ConfigResult,
    JudgeReport,
    RunSummary,
    UnverifiedLabels,
    environment_notes,
    write_results,
)
from eval.validate import past_window_conversations, route_document_mismatches

log = get_logger(__name__)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GOLDEN = ROOT / "eval" / "data" / "golden.jsonl"
DEFAULT_MULTITURN = ROOT / "eval" / "data" / "multiturn.jsonl"
RESULTS_DIR = ROOT / "eval" / "results"
PRICING_PATH = ROOT / "eval" / "pricing.yaml"

#: The seed the ``--human-sample`` draw shuffles with, and the file the method is recorded in.
#: Fixed rather than drawn from the clock: a sampling method a reviewer cannot re-run is not a
#: stated sampling method, and ``docs/EVALUATION.md`` has to state one.  It is printed to stderr
#: and written into the sample directory so the sentence can be copied into the document verbatim.
SAMPLE_SEED = 20260829
SAMPLE_METHOD_FILENAME = "judge_sample_method.txt"

#: The column ``--exclude-sample`` reads item ids out of.  It is the first column of
#: ``SAMPLE_COLUMNS``, and it is the one thing a prior sample is guaranteed to carry whatever a
#: labeller did to the rest of the file in a spreadsheet.
SAMPLE_ID_COLUMN = "item_id"

#: How many of the labelled items the ``full_budget_6`` diagnostic runs on.  A stratified subset,
#: reported separately with its n stated, because it exists to show the untruncated tool-call
#: distribution rather than to be an ablation row.
BUDGET_DIAGNOSTIC_ITEMS = 50


def load_pricing(path: Path = PRICING_PATH) -> dict[str, dict[str, float]]:
    """Token rates from ``eval/pricing.yaml``.  Empty by default, and that is deliberate."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError:
        return {}
    rates = raw.get("rates") or {}
    return {str(key): {str(k): float(v) for k, v in value.items()} for key, value in rates.items()}


def git_commit() -> str:
    """The commit the run happened at.  Published numbers name it."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
            cwd=ROOT,
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return result.stdout.strip()[:12] or "unknown"


def stratified(items: Sequence[GoldenItem], count: int) -> list[GoldenItem]:
    """A subset with the same category proportions as the whole set, in file order.

    Stratified rather than the first N: the golden set is written in category blocks, so the first
    50 items would be two categories and the diagnostic would report a tool-call distribution for a
    third of the question types.
    """
    by_category: dict[str, list[GoldenItem]] = {}
    for item in items:
        by_category.setdefault(item.category, []).append(item)

    per_category = max(1, count // max(1, len(by_category)))
    chosen: list[GoldenItem] = []
    for bucket in by_category.values():
        chosen.extend(bucket[:per_category])
    order = {item.id: index for index, item in enumerate(items)}
    return sorted(chosen[:count], key=lambda item: order[item.id])


async def sweep(
    runtime: Runtime,
    items: Sequence[GoldenItem],
    *,
    prefix: str,
    runs_dir: Path,
) -> list[ItemRun]:
    """Run every item, sequentially.

    Sequential on purpose.  Concurrency against a rate-limited endpoint turns a measured latency
    into a measurement of the queue, and p90 latency is a reported number.
    """
    runs: list[ItemRun] = []
    for index, item in enumerate(items, start=1):
        run = await run_golden_item(runtime, item, runs_dir=runs_dir, prefix=prefix)
        if not run.ok:
            log.warning("eval.item_failed", item=item.id, error=run.error)
        runs.append(run)
        if index % 10 == 0:
            print(f"  {prefix}: {index}/{len(items)}", file=sys.stderr)
    return runs


async def sweep_conversations(
    runtime: Runtime,
    conversations: Sequence[MultiturnConversation],
    *,
    prefix: str,
    runs_dir: Path,
) -> list[ConversationRun]:
    return [
        await run_conversation(runtime, conversation, runs_dir=runs_dir, prefix=prefix)
        for conversation in conversations
    ]


def score(
    config_name: str, runs: Sequence[ItemRun], *, pricing: dict[str, dict[str, float]]
) -> ConfigResult:
    """Compute every trace-derived metric for one configuration."""
    scored = [(run.item, run.events) for run in runs]
    routed = [routing_outcome(run.item, run.events) for run in runs]
    retrieved = [retrieval_outcome(run.item, run.events) for run in runs]
    routing = routing_report([outcome for outcome in routed if outcome is not None])
    retrieval = retrieval_report([outcome for outcome in retrieved if outcome is not None])
    return ConfigResult(
        config=config_name,
        n_items=len(runs),
        routing=routing,
        retrieval=retrieval,
        safety=safety_report(scored),
        latency=latency_report(scored),
        usage=usage_report(scored, pricing=pricing),
    )


async def judge_config(
    judge: Judge,
    runs: Sequence[ItemRun],
    documents: dict[str, Document],
) -> JudgeReport:
    """Faithfulness, both ways, for one configuration.

    The **oracle** column is judged against the golden set's ``relevant_doc_ids`` rather than
    against what the run retrieved, and it is computed for every configuration including
    ``baseline_llm`` -- which retrieves nothing and would otherwise be structurally n/a on the only
    faithfulness column, leaving the control condition unjudged on the metric the comparison is
    about.
    """
    retrieved_scores: list[float] = []
    oracle_scores: list[float] = []

    for run in runs:
        if not run.ok or run.outcome is None:
            continue
        answer = run.outcome.answer
        retrieved = [
            (doc_id, documents[doc_id].body)
            for doc_id in run.outcome.sources
            if doc_id in documents
        ]
        oracle = [
            (doc_id, documents[doc_id].body)
            for doc_id in run.item.relevant_doc_ids
            if doc_id in documents
        ]
        if retrieved:
            verdict = await judge.faithfulness(
                question=run.item.question, answer=answer, sources=retrieved
            )
            if verdict is not None and verdict.score is not None:
                retrieved_scores.append(verdict.score)
        if oracle:
            verdict = await judge.faithfulness(
                question=run.item.question, answer=answer, sources=oracle
            )
            if verdict is not None and verdict.score is not None:
                oracle_scores.append(verdict.score)

    return JudgeReport(
        judge_model=judge.model,
        prompt_version=FAITHFULNESS_PROMPT,
        faithfulness_retrieved=_mean(retrieved_scores),
        faithfulness_oracle=_mean(oracle_scores),
        n_judged=len(oracle_scores) or len(retrieved_scores),
    )


async def judge_conversations(
    judge: Judge, runs: Sequence[ConversationRun], base: JudgeReport
) -> JudgeReport:
    """Multi-turn resolution, over the turns a labeller annotated with a referent.

    A turn may be annotated with **several** referent turns, and such a turn is graded as one item:
    the judge is given every index and resolves the turn only if the answer accounts for all of
    them (``eval/judges/multiturn_v1.md``).  Grading each referent separately would have turned one
    labelled turn into two, four or five items and let a system that resolved three of four
    referents score 0.75 on a question it answered wrongly.
    """
    counts = {"resolved": 0, "unresolved": 0, "misresolved": 0}
    graded = 0
    for run in runs:
        if run.error:
            continue
        for index, turn in enumerate(run.conversation.turns):
            if turn.depends_on_turn is None or not turn.expected_referent:
                continue
            if index >= len(run.answers):
                continue
            verdict = await judge.multiturn(
                conversation=[t.question for t in run.conversation.turns[:index]],
                question=turn.question,
                referent=turn.expected_referent,
                referent_turns=turn.depends_on_turn,
                answer=run.answers[index],
            )
            if verdict is None:
                continue
            counts[verdict.verdict] += 1
            graded += 1

    if not graded:
        return base
    return JudgeReport(
        judge_model=base.judge_model,
        prompt_version=base.prompt_version,
        faithfulness_retrieved=base.faithfulness_retrieved,
        faithfulness_oracle=base.faithfulness_oracle,
        n_judged=base.n_judged,
        multiturn_resolved=counts["resolved"] / graded,
        multiturn_unresolved=counts["unresolved"] / graded,
        multiturn_misresolved=counts["misresolved"] / graded,
        n_conversations=graded,
    )


def sample_block(item_id: str) -> str:
    """The id-prefix block an item belongs to: ``g-gh-001`` -> ``g-gh``.

    The **id prefix** rather than ``item.category``: the two agree in the shipped file and
    ``tests/test_eval_drafts.py`` keeps them agreeing, but the CSV a person labels carries the id
    and nothing else, so the id is the only thing a reviewer checking the draw for balance can
    actually check.
    """
    head, _, _ = item_id.rpartition("-")
    return head or item_id


class SampleDrawError(RuntimeError):
    """Raised when the requested judge sample cannot be drawn as specified.

    Separate from :class:`eval.judge.JudgeError`, which is about a prompt that will not load or a
    completed sample that will not score.  This one is about the draw: a prior sample that cannot
    be read, or an exclusion that leaves a stratum unable to fill itself.  It is raised rather than
    worked around, because every way of working around it -- widening the draw, taking fewer from
    the short block, falling back to the full pool -- silently changes the sampling method, and the
    method is one of the things ``docs/EVALUATION.md`` has to state.
    """


def excluded_item_ids(paths: Sequence[Path]) -> tuple[str, ...]:
    """The item ids a prior ``judge_sample.csv`` already used, in order and deduplicated.

    A judge prompt revised because a validation round failed is re-scored on a **fresh** sample.
    Re-scoring it on the items it was revised against measures the fit of the revision to those
    items, which is the one number a validation round must not produce.  Changing the seed is not
    enough on its own -- a different shuffle of the same pool draws some of the same rows -- so the
    ids come out of the prior CSV and are removed from the pool before the shuffle.

    Read with ``utf-8-sig`` because by then the file has been round-tripped through a spreadsheet,
    and Excel writes a BOM -- which would otherwise prefix the first column's name and make an
    exclusion of forty items silently exclude none.
    """
    ids: list[str] = []
    for path in paths:
        try:
            with Path(path).open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
        except OSError as exc:
            raise SampleDrawError(f"cannot read the prior judge sample at {path}: {exc}") from exc
        if not rows or SAMPLE_ID_COLUMN not in rows[0]:
            raise SampleDrawError(
                f"{path}: no rows with an {SAMPLE_ID_COLUMN!r} column, so it would exclude "
                "nothing. --exclude-sample takes a judge_sample.csv from a previous round."
            )
        ids.extend(value for row in rows if (value := str(row.get(SAMPLE_ID_COLUMN) or "").strip()))
    return tuple(dict.fromkeys(ids))


def per_block_count(blocks: int, count: int) -> int:
    """How many rows the draw takes from each id-prefix block."""
    return max(1, count // max(1, blocks))


def refuse_short_blocks(supply: Mapping[str, int], *, per_block: int, excluded: int) -> None:
    """Refuse a draw whose exclusion has left some stratum unable to fill itself.

    ``supply`` counts the candidates left in **every** block, including one exclusion emptied
    completely -- which is why the blocks are enumerated before the exclusion is applied rather
    than after.  A block that vanished from the pool would otherwise leave four strata behind, and
    the draw would quietly restratify over them and report a method sentence saying so in a file
    nobody re-reads.
    """
    short = [(block, supply[block]) for block in sorted(supply) if supply[block] < per_block]
    if not short:
        return
    detail = ", ".join(f"{block} has {available}" for block, available in short)
    raise SampleDrawError(
        f"excluding {excluded} item id(s) leaves a block short: {detail}, against the "
        f"{per_block} the draw needs from each of the {len(supply)} blocks. A re-validation "
        "sample is drawn only from items the previous round did not use, and taking fewer from a "
        "short block would change the stratification the earlier number was computed under. "
        "Lower --human-sample, or exclude fewer prior samples."
    )


def block_supply(item_ids: Iterable[str], *, exclude: Collection[str] = ()) -> dict[str, int]:
    """Candidates per id-prefix block after exclusion, keyed by every block the ids contain."""
    supply = {sample_block(item_id): 0 for item_id in item_ids}
    for item_id in item_ids:
        if item_id not in exclude:
            supply[sample_block(item_id)] += 1
    return supply


@dataclass(frozen=True)
class SampleDraw:
    """A drawn judge-validation sample, and the description of how it was drawn."""

    config: str
    rows: tuple[SampleRow, ...]
    seed: int
    per_block: int
    blocks: tuple[str, ...]
    excluded: tuple[str, ...]
    #: Item ids removed from the pool before the shuffle, and the files they were read from.
    #: Both travel into :meth:`method`, because "a fresh sample" is a claim about the draw that a
    #: reviewer can only check if the draw says which items it was forbidden to take.
    prior_excluded_ids: tuple[str, ...] = ()
    prior_sample_paths: tuple[str, ...] = ()

    def method(self) -> str:
        """One sentence for ``docs/EVALUATION.md``'s "sampling method" line."""
        replaced = (
            "no row needed replacing"
            if not self.excluded
            else (
                f"{len(self.excluded)} row(s) the judge could not score "
                f"({', '.join(self.excluded)}) were excluded and replaced from within their own "
                "block"
            )
        )
        prior = (
            "no prior sample excluded"
            if not self.prior_sample_paths
            else (
                f"excluding the {len(self.prior_excluded_ids)} item id(s) drawn by "
                f"{', '.join(self.prior_sample_paths)}"
            )
        )
        return (
            f"{len(self.rows)} rows from config `{self.config}`, stratified over "
            f"{len(self.blocks)} item-id blocks ({', '.join(self.blocks)}) at {self.per_block} "
            f"per block, shuffled with random.Random({self.seed}), {prior}; {replaced}"
        )


async def draw_human_sample(
    judge: Judge,
    runs: Sequence[ItemRun],
    documents: dict[str, Document],
    *,
    count: int,
    config: str,
    seed: int = SAMPLE_SEED,
    exclude: Collection[str] = (),
    exclude_sources: Sequence[str] = (),
) -> SampleDraw:
    """Draw the judge-validation sample and fill in the judge's half of every row.

    Four properties, each of which the first version of this path did not have:

    **The judge is actually run.**  The sample exists to compare the judge's label against a
    person's, so a CSV shipped with an empty ``judge_label`` cannot be scored at all -- and
    ``--score-judge`` would have compared hand-written labels against empty strings and reported a
    kappa for the comparison.  Every sampled row is judged here, against the **same evidence**
    :func:`judge_config` uses: the full bodies of the notes the turn cited, numbered by
    ``numbered_sources`` so the CSV's ``sources_text`` is the string the verdict came from.

    **The draw is stratified and seeded.**  ``runs[:N]`` over a set written in category blocks is
    one and a third categories.  So the sample takes ``count // len(blocks)`` from each id-prefix
    block, shuffled with a fixed seed, and the seed and the method travel with the CSV.

    **Every shipped row is scorable.**  A judge that returns nothing, or that finds no factual
    claim to grade, produces no label, and a row with no label is a row a person labels for
    nothing.  Such a row is dropped and replaced from within its **own** block, so the strata stay
    equal rather than being levelled by whichever block happened to survive.

    **A second round cannot redraw the first round's items.**  ``exclude`` is the id set a prior
    sample used, and it is removed from each block's candidates **before** the shuffle rather than
    filtered out of the result -- a post-hoc filter would return a short sample instead of a fresh
    one.  If that leaves any block unable to fill its stratum the draw is refused
    (:func:`refuse_short_blocks`); the blocks themselves are enumerated before the exclusion, so a
    block emptied outright is reported as short rather than disappearing and letting the draw
    quietly restratify over the four that survived.
    """
    by_block: dict[str, list[ItemRun]] = {}
    for run in runs:
        if run.ok and run.outcome is not None:
            by_block.setdefault(sample_block(run.item.id), []).append(run)

    blocks = tuple(sorted(by_block))
    available = {
        block: [run for run in by_block[block] if run.item.id not in exclude] for block in blocks
    }
    per_block = per_block_count(len(blocks), count)
    refuse_short_blocks(
        {block: len(pool) for block, pool in available.items()},
        per_block=per_block,
        excluded=len(exclude),
    )
    # Not cryptographic and not meant to be: the point of the seed is that the draw is reproducible.
    rng = random.Random(seed)  # noqa: S311

    rows: list[SampleRow] = []
    excluded: list[str] = []
    for block in blocks:
        candidates = list(available[block])
        rng.shuffle(candidates)
        taken = 0
        for run in candidates:
            if taken == per_block:
                break
            row = await _judged_row(judge, run, documents)
            if row is None:
                excluded.append(run.item.id)
                continue
            rows.append(row)
            taken += 1

    return SampleDraw(
        config=config,
        rows=tuple(rows),
        seed=seed,
        per_block=per_block,
        blocks=blocks,
        excluded=tuple(excluded),
        prior_excluded_ids=tuple(exclude),
        prior_sample_paths=tuple(exclude_sources),
    )


async def _judged_row(
    judge: Judge, run: ItemRun, documents: dict[str, Document]
) -> SampleRow | None:
    """One sample row, or ``None`` when the judge produced nothing to agree or disagree with.

    The answer-level label is ``supported`` only when every claim the judge found was supported,
    because that is the question the labelling instructions put to the person: a three-valued judge
    column compared against a two-valued human one would lose kappa to a disagreement about the
    label set rather than about the answers.  A verdict with ``total == 0`` -- an answer that is
    only an escalation banner and a disclaimer -- is not a third value either.  There is nothing to
    ground, :attr:`FaithfulnessVerdict.score` is ``None`` for the same reason, and the row is
    dropped rather than labelled.
    """
    if run.outcome is None:  # pragma: no cover - the caller filters these out
        return None
    sources = [
        (doc_id, documents[doc_id].body) for doc_id in run.outcome.sources if doc_id in documents
    ]
    verdict = await judge.faithfulness(
        question=run.item.question, answer=run.outcome.answer, sources=sources
    )
    if verdict is None or verdict.total == 0:
        return None
    return SampleRow(
        item_id=run.item.id,
        question=run.item.question,
        answer=run.outcome.answer,
        retrieved_doc_ids=" ".join(run.outcome.sources),
        sources_text=numbered_sources(sources),
        judge_label="supported" if verdict.supported == verdict.total else "unsupported",
        judge_rationale=verdict.rationale,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eval/run.py",
        description=(
            "Run the evaluation sweep. Requires a live API key and costs money; size it with "
            "--limit before running the full set."
        ),
    )
    parser.add_argument(
        "--config", help=f"One preset instead of the ablation set: {', '.join(PRESETS)}"
    )
    parser.add_argument("--limit", type=int, help="Run only the first N items.")
    parser.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN)
    parser.add_argument("--multiturn", type=Path, default=DEFAULT_MULTITURN)
    parser.add_argument("--out", type=Path, default=None, help="Results directory.")
    parser.add_argument("--embedder", default="bge", choices=["bge", "hash"])
    parser.add_argument("--store", default="chroma", choices=["chroma", "numpy"])
    parser.add_argument("--no-judge", action="store_true", help="Skip the LLM judge entirely.")
    parser.add_argument(
        "--human-sample",
        type=int,
        metavar="N",
        help=(
            "Write judge_sample.csv with N judged rows for a person to label, then stop. "
            "Requires --config; the draw is stratified over the five item-id blocks."
        ),
    )
    parser.add_argument(
        "--sample-seed",
        type=int,
        default=SAMPLE_SEED,
        metavar="INT",
        help=(
            f"Seed the --human-sample shuffle with this instead of {SAMPLE_SEED}. A re-validation "
            "round changes it so the draw is not the previous round's draw."
        ),
    )
    parser.add_argument(
        "--exclude-sample",
        type=Path,
        action="append",
        metavar="CSV",
        default=None,
        help=(
            "A prior judge_sample.csv whose item_ids are excluded from the --human-sample draw. "
            "Repeatable, so a third round can exclude the first two. A revised judge prompt "
            "scored against the sample it was revised on measures overfitting, not agreement."
        ),
    )
    parser.add_argument(
        "--score-judge",
        type=Path,
        metavar="CSV",
        help="Score a completed judge sample and report agreement and Cohen's kappa, then stop.",
    )
    return parser


async def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings.from_env()
    configure_logging(settings.log_level, settings.log_format)

    if args.score_judge:
        return _score_judge(args.score_judge)

    if args.human_sample and not args.config:
        print(
            "--human-sample needs --config. Without it the sample is drawn from the first preset "
            "of the ablation set, which is baseline_llm -- it retrieves nothing, so every row "
            "would carry empty evidence and the judge would be validated on the one configuration "
            "whose answers are ungrounded by construction. Try: --config full --human-sample 40",
            file=sys.stderr,
        )
        return 2

    if args.human_sample and args.no_judge:
        print(
            "--human-sample cannot be combined with --no-judge. The sample exists to compare the "
            "judge's label against a person's, and a CSV whose judge_label column is empty cannot "
            "be scored at all.",
            file=sys.stderr,
        )
        return 2

    exclude_paths = tuple(args.exclude_sample or ())
    if exclude_paths and not args.human_sample:
        print(
            "--exclude-sample only applies to --human-sample: there is no draw to exclude anything "
            "from. Passing it to a sweep would look like a fresh sample was being drawn while "
            "nothing was excluded from anything.",
            file=sys.stderr,
        )
        return 2

    if settings.provider == "mock":
        print(
            "CONSILIUM_PROVIDER is 'mock'. The evaluation harness needs a live provider: numbers "
            "from a scripted mock are not measurements and must never be published.",
            file=sys.stderr,
        )
        return 2

    try:
        items = load_golden(args.golden)
        conversations = load_multiturn(args.multiturn)
    except EvalDataError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.limit:
        items = items[: args.limit]

    # Read and checked *before* the sweep, which is the expensive half: an unreadable prior sample
    # or an exclusion that cannot fill a stratum is a mistake in the command line, and finding it
    # after 150 paid items have run costs money to learn nothing.  The same check runs again inside
    # the draw, against the items that actually produced an outcome.
    prior_ids: tuple[str, ...] = ()
    if exclude_paths:
        try:
            prior_ids = excluded_item_ids(exclude_paths)
            supply = block_supply([item.id for item in items], exclude=prior_ids)
            refuse_short_blocks(
                supply,
                per_block=per_block_count(len(supply), args.human_sample),
                excluded=len(prior_ids),
            )
        except SampleDrawError as exc:
            print(str(exc), file=sys.stderr)
            return 2

    presets = [args.config] if args.config else list(ABLATION_PRESETS)
    started = datetime.now(UTC)
    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    out = args.out or (RESULTS_DIR / stamp)
    runs_dir = out / "runs"
    pricing = load_pricing()

    results: list[ConfigResult] = []
    judge: Judge | None = None
    documents: dict[str, Document] = {}

    for name in presets:
        config = get_preset(name)
        runtime = build_runtime(settings, config=config, embedder=args.embedder, store=args.store)
        documents = dict(runtime.documents)
        _warn_route_document_mismatches(runtime, items)
        if judge is None and not args.no_judge:
            judge = Judge(runtime.provider)

        print(f"running {name} over {len(items)} items", file=sys.stderr)
        runs = await sweep(runtime, items, prefix=name, runs_dir=runs_dir)

        if args.human_sample:
            if judge is None:  # pragma: no cover - refused at argument validation above
                print("--human-sample needs the judge; drop --no-judge.", file=sys.stderr)
                return 2
            print(f"drawing a judge sample with seed {args.sample_seed}", file=sys.stderr)
            try:
                draw = await draw_human_sample(
                    judge,
                    runs,
                    documents,
                    count=args.human_sample,
                    config=name,
                    seed=args.sample_seed,
                    exclude=prior_ids,
                    exclude_sources=tuple(str(path) for path in exclude_paths),
                )
            except SampleDrawError as exc:
                print(str(exc), file=sys.stderr)
                return 2
            path = out / "judge_sample.csv"
            write_sample(path, draw.rows)
            method_path = out / SAMPLE_METHOD_FILENAME
            method_path.write_text(draw.method() + "\n", encoding="utf-8")
            print(
                f"wrote {path}\n"
                f"  sampling method: {draw.method()}\n"
                f"  recorded in {method_path}, for docs/EVALUATION.md\n"
                "Hide judge_label and judge_rationale before you read a row, fill in human_label, "
                "then --score-judge it."
            )
            return 0

        result = score(name, runs, pricing=pricing)
        if judge is not None:
            report = await judge_config(judge, runs, documents)
            if name == "full":
                conversation_runs = await sweep_conversations(
                    runtime, conversations, prefix=f"{name}-mt", runs_dir=runs_dir
                )
                report = await judge_conversations(judge, conversation_runs, report)
            result = ConfigResult(
                config=result.config,
                n_items=result.n_items,
                routing=result.routing,
                retrieval=result.retrieval,
                safety=result.safety,
                latency=result.latency,
                usage=result.usage,
                judge=report,
            )
        results.append(result)

    if "full_budget_6" in PRESETS and not args.config:
        diagnostic = stratified(items, BUDGET_DIAGNOSTIC_ITEMS)
        runtime = build_runtime(
            settings,
            config=get_preset("full_budget_6"),
            embedder=args.embedder,
            store=args.store,
        )
        print(f"running full_budget_6 diagnostic over {len(diagnostic)} items", file=sys.stderr)
        runs = await sweep(runtime, diagnostic, prefix="full_budget_6", runs_dir=runs_dir)
        results.append(score("full_budget_6", runs, pricing=pricing))

    environment = environment_notes()
    unverified = UnverifiedLabels(
        n_total=len(items),
        n_items=unverified_item_count(items),
        fields=unverified_label_counts(items),
    )
    summary = RunSummary(
        commit=git_commit(),
        started_at=started.isoformat(),
        finished_at=datetime.now(UTC).isoformat(),
        provider=settings.provider,
        model=settings.model or "provider default",
        judge_model=judge.model if judge else None,
        golden_path=str(args.golden),
        multiturn_path=str(args.multiturn),
        n_items=len(items),
        limit=args.limit,
        python=environment["python"],
        platform=environment["platform"],
        pricing_source=(
            f"{PRICING_PATH.name} ({len(pricing)} model(s) priced)"
            if pricing
            else f"{PRICING_PATH.name} is empty, so cost is reported as not measured"
        ),
        unverified_labels=unverified,
        results=results,
        notes=[
            "full_budget_6 is a diagnostic on a stratified subset, not an ablation row; its n is "
            "stated in its own section.",
            "Episodic memory was disabled for this run: cross-session recall over independent "
            "golden items would let item N answer from item N-1.",
        ]
        + [_compaction_note(conversations)]
        + ([unverified.sentence()] if unverified.present else []),
    )
    summary_path, report_path = write_results(out, summary)
    print(f"wrote {summary_path}\nwrote {report_path}")
    return 0


def _warn_route_document_mismatches(runtime: Runtime, items: Sequence[GoldenItem]) -> None:
    """Log, once per sweep, any item whose route carries no dedicated skill for its documents.

    A **warning**, not a refusal.  ``search_knowledge`` is unfiltered and every agent holds it, so
    no labelled route is impossible and nothing here invalidates a run.  What it flags is an item
    whose documents the turn can only reach through the unfiltered search -- competing with the
    whole corpus for a top-5 slot rather than with one category of it -- which is worth seeing
    beside a recall@5 number rather than discovered afterwards.

    ``tests/test_eval_drafts.py`` pins the same set against a reviewed baseline, so a new one is
    caught before a sweep is ever paid for; this is the copy a person running the sweep sees.
    """
    mismatches = route_document_mismatches(
        items,
        policy=runtime.policy,
        doc_categories={
            doc_id: document.category for doc_id, document in runtime.documents.items()
        },
    )
    for mismatch in mismatches:
        log.warning(
            "eval.route_document_mismatch",
            item=mismatch.item_id,
            agents=mismatch.agents,
            expected_skills=mismatch.expected_skills,
            owning_agents=mismatch.owning_agents,
            detail=str(mismatch),
        )


def _compaction_note(conversations: Sequence[MultiturnConversation]) -> str:
    """The small-n caveat on the memory ablation, computed from the file and carried into the run.

    Only the conversations whose dependencies reach past the working-memory window can distinguish
    ``full`` from ``full_no_memory`` on the compaction path at all: inside the window both configs
    replay the same verbatim transcript.  There are five of them, out of twenty-nine, so any effect
    size read off that comparison rests on five items.  It is computed here rather than written as
    a sentence in a document so that it cannot go stale against the file, and it lands in every
    run's ``summary.json`` rather than only in ``docs/EVALUATION.md`` -- the number it qualifies is
    in the results, and so is the qualification.
    """
    past_window = past_window_conversations(conversations)
    return (
        f"The summarization path is exercised by {len(past_window)} of {len(conversations)} "
        f"multi-turn conversations ({', '.join(past_window) or 'none'}): only these carry a "
        "dependency reaching past the working-memory window, so the memory ablation's effect on "
        f"that path rests on n={len(past_window)}."
    )


def _score_judge(path: Path) -> int:
    try:
        result = score_sample(path)
    except JudgeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    raw = "not measured" if result.raw_agreement is None else f"{result.raw_agreement:.3f}"
    kappa = "not measured" if result.cohens_kappa is None else f"{result.cohens_kappa:.3f}"
    print(f"judge validation over n={result.n}")
    print(f"raw agreement: {raw}")
    print(f"Cohen's kappa: {kappa}")
    print("Copy both numbers into docs/EVALUATION.md, with n, beside the faithfulness results.")
    return 0


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def cli(argv: Sequence[str] | None = None) -> int:
    """Entry point.  ``consilium eval`` forwards here; so does ``python -m eval.run``."""
    return asyncio.run(main(argv))


if __name__ == "__main__":  # pragma: no cover - the money-spending path
    raise SystemExit(cli())
