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
``--human-sample N``    write ``judge_sample.csv`` for a person to label.
``--score-judge PATH``  read a completed sample and report agreement and Cohen's kappa.

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
import subprocess
import sys
from collections.abc import Sequence
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

log = get_logger(__name__)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GOLDEN = ROOT / "eval" / "data" / "golden.jsonl"
DEFAULT_MULTITURN = ROOT / "eval" / "data" / "multiturn.jsonl"
RESULTS_DIR = ROOT / "eval" / "results"
PRICING_PATH = ROOT / "eval" / "pricing.yaml"

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
    """Multi-turn resolution, over the turns a labeller annotated with a referent."""
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


def sample_rows(runs: Sequence[ItemRun], limit: int) -> list[SampleRow]:
    """Rows for the human-labelling CSV, with the judge label left for the runner to fill."""
    rows: list[SampleRow] = []
    for run in runs[:limit]:
        if not run.ok or run.outcome is None:
            continue
        rows.append(
            SampleRow(
                item_id=run.item.id,
                question=run.item.question,
                answer=run.outcome.answer,
                retrieved_doc_ids=" ".join(run.outcome.sources),
                judge_label="",
                judge_rationale="",
            )
        )
    return rows


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
        help="Write judge_sample.csv with N rows for a person to label, then stop.",
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
        if judge is None and not args.no_judge:
            judge = Judge(runtime.provider)

        print(f"running {name} over {len(items)} items", file=sys.stderr)
        runs = await sweep(runtime, items, prefix=name, runs_dir=runs_dir)

        if args.human_sample:
            path = out / "judge_sample.csv"
            write_sample(path, sample_rows(runs, args.human_sample))
            print(f"wrote {path}; fill in the human_label column, then --score-judge it")
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
        + ([unverified.sentence()] if unverified.present else []),
    )
    summary_path, report_path = write_results(out, summary)
    print(f"wrote {summary_path}\nwrote {report_path}")
    return 0


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
