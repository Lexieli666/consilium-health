"""``summary.json`` and ``report.md``: what makes a published number checkable.

``eval/results/<timestamp>/`` holds every run; the one that is published is copied to
``eval/results/published/``, **which is committed while the timestamped directories are not**.  A
published number whose evidence is gitignored is a number no reviewer can check, which defeats the
point of the whole harness.

Two rules run through this module:

**``not measured`` is printed, never a zero.**  Every metric is optional, and a missing one renders
as the words ``not measured``.  A metric that quietly became 0.0 because the run had no red-flag
items would read as a measurement of a system that failed, which is the opposite of what happened.

**``n/a`` and ``not measured`` are different.**  ``n/a`` means the cell is structurally undefined --
routing accuracy for a configuration that has no router.  ``not measured`` means it could have been
measured and was not.  The ablation table distinguishes them, and neither is ever filled in.

**A number computed against an unverified reference says so in the table, not in a footnote.**  The
golden set's ``relevant_doc_ids`` and ``reference_answer`` are machine-written and the owner decided
not to verify them item by item (``eval/items.py``).  recall@5 and both faithfulness columns are
computed against exactly those two fields; routing accuracy and red-flag recall are not.  A reader
who takes recall@5 for a number measured against a hand-built reference has been misled, and a
footnote is something a reader can finish the table without reaching -- so the disclosure is in the
column headers, in a line directly under the table, and in the two per-configuration paragraphs
that report those metrics.
"""

from __future__ import annotations

import json
import platform
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any

from eval.metrics import (
    LatencyReport,
    RetrievalReport,
    RoutingReport,
    SafetyReport,
    UsageReport,
)

#: What a metric renders as when it has no value.
NOT_MEASURED = "not measured"

#: What a cell renders as when the metric is structurally undefined for that configuration.
NOT_APPLICABLE = "n/a"

#: Cells the ablation table leaves as ``n/a`` because the configuration has no such component.
#: These are undefined, not missing, and are never filled in.
STRUCTURALLY_UNDEFINED: dict[str, tuple[str, ...]] = {
    "baseline_llm": ("routing_accuracy", "recall_at_5", "faithfulness_retrieved"),
    "single_agent_rag": ("routing_accuracy",),
}


#: Which metric each unverified label field is the reference for.  Used to name the affected
#: numbers in the disclosure rather than saying "some metrics".
METRICS_BY_LABEL_FIELD: dict[str, tuple[str, ...]] = {
    "relevant_doc_ids": ("recall@5", "hit@5", "MRR@10", "faithfulness (oracle)"),
    "reference_answer": ("faithfulness (oracle)",),
    "expected_route": ("routing accuracy",),
    "red_flag": ("red-flag recall",),
}


@dataclass(frozen=True)
class UnverifiedLabels:
    """Which label fields hold a machine-written value no person checked, and on how many items.

    Carried in ``summary.json`` so a reviewer reading the committed evidence can see the provenance
    of the reference the numbers were computed against, without having to open the golden set.
    """

    #: Items in the golden set the run was scored over.
    n_total: int = 0
    #: Items holding at least one unverified label field.
    n_items: int = 0
    #: ``label field -> how many items hold an unverified value in it``.
    fields: dict[str, int] = field(default_factory=dict)

    @property
    def present(self) -> bool:
        """Whether anything was left unverified.  False means every label was written by hand."""
        return bool(self.fields)

    @property
    def affected_metrics(self) -> tuple[str, ...]:
        """The metrics computed against an unverified field, deduplicated, in a stable order."""
        seen: dict[str, None] = {}
        for name in self.fields:
            for metric in METRICS_BY_LABEL_FIELD.get(name, ()):
                seen.setdefault(metric, None)
        return tuple(seen)

    def sentence(self) -> str:
        """The disclosure, in one sentence, with the counts in it.

        Deliberately not softened.  "Partly verified" or "lightly reviewed" would describe a
        process that did not happen; what happened is that nobody read them.
        """
        if not self.present:
            return ""
        fields = ", ".join(
            f"`{name}` ({count} of {self.n_total} items)" for name, count in self.fields.items()
        )
        return (
            f"**Measured against an unverified reference.** {fields} were written by a model and "
            "no person verified them, so "
            + ", ".join(self.affected_metrics)
            + " are measured against a machine-constructed reference. Routing accuracy and "
            "red-flag recall are not: `expected_route` and `red_flag` were hand-labelled on all "
            f"{self.n_total} items."
        )


@dataclass(frozen=True)
class JudgeReport:
    """Faithfulness and multi-turn resolution, and the honest state of the judge behind them."""

    judge_model: str | None = None
    prompt_version: str | None = None
    #: Faithfulness against what the run actually retrieved.
    faithfulness_retrieved: float | None = None
    #: Faithfulness against the golden set's ``relevant_doc_ids``.  Computed for **every** config
    #: including ``baseline_llm``, which retrieves nothing and would otherwise be structurally n/a
    #: on the only faithfulness column -- leaving the control condition unjudged.
    faithfulness_oracle: float | None = None
    n_judged: int = 0
    multiturn_resolved: float | None = None
    multiturn_unresolved: float | None = None
    multiturn_misresolved: float | None = None
    n_conversations: int = 0
    #: Raw agreement and Cohen's kappa against a human sample.  ``None`` until
    #: ``--score-judge`` has been run; until then docs/EVALUATION.md says the judge is unvalidated.
    human_agreement: float | None = None
    cohens_kappa: float | None = None
    n_human_labeled: int = 0


@dataclass(frozen=True)
class ConfigResult:
    """Everything measured for one configuration."""

    config: str
    n_items: int
    routing: RoutingReport
    retrieval: RetrievalReport
    safety: SafetyReport
    latency: LatencyReport
    usage: UsageReport
    judge: JudgeReport = field(default_factory=JudgeReport)


@dataclass(frozen=True)
class RunSummary:
    """One sweep: what was run, against what, with what, and what came out."""

    commit: str
    started_at: str
    finished_at: str
    provider: str
    model: str
    judge_model: str | None
    golden_path: str
    multiturn_path: str
    n_items: int
    limit: int | None
    python: str
    platform: str
    pricing_source: str
    #: Provenance of the reference the retrieval and faithfulness numbers were computed against.
    unverified_labels: UnverifiedLabels = field(default_factory=UnverifiedLabels)
    results: list[ConfigResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def to_json(summary: RunSummary) -> str:
    """Serialize the whole summary, with ``None`` preserved rather than dropped.

    ``None`` is data here: it is the difference between a metric that was measured as zero and one
    that was not measured at all, and a serializer that omitted it would erase that distinction from
    the committed artifact.
    """
    return json.dumps(_plain(summary), indent=2, sort_keys=False) + "\n"


def write_results(directory: Path, summary: RunSummary) -> tuple[Path, Path]:
    """Write ``summary.json`` and ``report.md``, returning both paths."""
    directory.mkdir(parents=True, exist_ok=True)
    summary_path = directory / "summary.json"
    report_path = directory / "report.md"
    summary_path.write_text(to_json(summary), encoding="utf-8")
    report_path.write_text(render_markdown(summary), encoding="utf-8")
    return summary_path, report_path


def environment_notes() -> dict[str, str]:
    """What the run happened on.  Recorded so a number can be reproduced or explained."""
    return {
        "python": sys.version.split()[0],
        "platform": f"{platform.system()} {platform.release()}",
    }


def fmt(value: float | None, *, digits: int = 3, suffix: str = "") -> str:
    """Render a metric, or the words ``not measured``."""
    if value is None:
        return NOT_MEASURED
    return f"{value:.{digits}f}{suffix}"


def fmt_cell(value: float | None, config: str, metric: str, **kwargs: Any) -> str:
    """Render an ablation cell, distinguishing ``n/a`` from ``not measured``."""
    if metric in STRUCTURALLY_UNDEFINED.get(config, ()):
        return NOT_APPLICABLE
    return fmt(value, **kwargs)


def render_markdown(summary: RunSummary) -> str:
    """The human-readable report.

    The ablation table comes first because it is the claim: "the architecture helps" needs a
    control, and the table is where the control sits next to the system.
    """
    unverified = summary.unverified_labels
    #: Appended to the header of every column whose reference is machine-written.  In the header
    #: rather than behind a marker, because a marker is a footnote and a footnote is optional
    #: reading -- and the whole point is that the reader cannot take these three numbers for
    #: something they are not.
    caveat = " (vs. unverified ref)" if unverified.present else ""
    lines: list[str] = [
        "# Evaluation results",
        "",
        "> **Not medical advice.** This is an educational software project. It does not diagnose,"
        " treat, or provide clinical guidance, and it must not be used for real medical decisions."
        " No patient data of any kind may be used with it.",
        "",
        f"- commit: `{summary.commit}`",
        f"- started: {summary.started_at}",
        f"- finished: {summary.finished_at}",
        f"- provider / model: `{summary.provider}` / `{summary.model}`",
        f"- judge model: `{summary.judge_model or NOT_MEASURED}`",
        f"- golden set: `{summary.golden_path}` ({summary.n_items} items"
        + (f", limited to {summary.limit}" if summary.limit else "")
        + ")",
        f"- multi-turn set: `{summary.multiturn_path}`",
        f"- python / platform: {summary.python} / {summary.platform}",
        f"- token rates: {summary.pricing_source}",
        "",
        "`not measured` means the number was not produced by this run. `n/a` means the cell is",
        "structurally undefined for that configuration. Neither is ever filled in by hand.",
        "",
        "## Ablation",
        "",
        f"| configuration | routing acc | recall@5{caveat} | faithfulness retrieved{caveat} |"
        f" faithfulness oracle{caveat} | red-flag recall | p90 latency | tokens/turn | cost/turn |",
        "|---|---|---|---|---|---|---|---|---|",
    ]

    for result in summary.results:
        config = result.config
        lines.append(
            "| `{config}` | {routing} | {recall} | {faith_r} | {faith_o} | {red} | {p90} |"
            " {tokens} | {cost} |".format(
                config=config,
                routing=fmt_cell(result.routing.accuracy, config, "routing_accuracy"),
                recall=fmt_cell(result.retrieval.recall_at_5_union, config, "recall_at_5"),
                faith_r=fmt_cell(
                    result.judge.faithfulness_retrieved, config, "faithfulness_retrieved"
                ),
                faith_o=fmt_cell(result.judge.faithfulness_oracle, config, "faithfulness_oracle"),
                red=fmt_cell(result.safety.red_flag_recall, config, "red_flag_recall"),
                p90=fmt(result.latency.p90_ms, digits=0, suffix=" ms"),
                tokens=fmt(result.usage.tokens_per_turn, digits=0),
                cost=fmt(result.usage.cost_per_turn_usd, digits=4),
            )
        )

    if unverified.present:
        lines += ["", unverified.sentence()]

    lines += ["", "## Per configuration", ""]
    for result in summary.results:
        lines.extend(_config_section(result, unverified))

    if summary.notes:
        lines += ["## Notes", ""]
        lines += [f"- {note}" for note in summary.notes]
        lines.append("")

    return "\n".join(lines)


def _config_section(result: ConfigResult, unverified: UnverifiedLabels) -> list[str]:
    routing, retrieval, safety, latency, usage = (
        result.routing,
        result.retrieval,
        result.safety,
        result.latency,
        result.usage,
    )
    lines = [
        f"### `{result.config}`",
        "",
        f"{result.n_items} items.",
        "",
        "**Routing.** "
        f"accuracy {fmt(routing.accuracy)} over n={routing.n}; "
        f"excluding fallbacks {fmt(routing.accuracy_excluding_fallback)} "
        f"over n={routing.n_excluding_fallback}; "
        f"planner fallback rate {fmt(routing.fallback_rate)}.",
        "",
    ]
    if routing.confusion:
        lines += ["| expected mode | actual mode | count |", "|---|---|---|"]
        lines += [
            f"| {expected} | {actual} | {count} |"
            for (expected, actual), count in sorted(routing.confusion.items())
        ]
        lines.append("")
    if routing.per_agent_set:
        lines += ["| expected agent set | correct | total |", "|---|---|---|"]
        lines += [
            f"| `{key}` | {correct} | {total} |"
            for key, (correct, total) in sorted(routing.per_agent_set.items())
        ]
        lines.append("")

    lines += [
        "**Retrieval.** "
        f"recall@5 (union over the turn) {fmt(retrieval.recall_at_5_union)}; "
        f"recall@5 (first retrieval event) {fmt(retrieval.recall_at_5_first_event)}; "
        f"hit@5 {fmt(retrieval.hit_at_5)}; MRR@10 {fmt(retrieval.mrr_at_10)}; "
        f"documents retrieved per turn {fmt(retrieval.docs_retrieved_per_turn, digits=1)} "
        f"over {fmt(retrieval.retrievals_per_turn, digits=1)} retrievals; n={retrieval.n}."
        + _reference_caveat(unverified, "relevant_doc_ids"),
        "",
        "**Safety.** "
        f"red-flag recall {fmt(safety.red_flag_recall)} over n={safety.n_red_flag_items}, "
        f"with **{safety.red_flag_false_negatives} false negative(s)**; "
        f"the model escalated unaided on {fmt(safety.model_escalated_unaided)}; "
        f"violations {fmt(safety.violations_per_100_turns, digits=1)} per 100 turns; "
        f"repairs {fmt(safety.repairs_per_100_turns, digits=1)} per 100 turns "
        f"({safety.post_stream_repairs} after a stream had started); "
        f"the negation guard changed the outcome on {safety.negation_suppressed_turns} turn(s).",
        "",
    ]
    if safety.by_stratum:
        lines += [
            "By phrasing stratum (never pooled -- a single figure would move with the ratio"
            " between the strata rather than with the system): "
            + "; ".join(
                f"{name} n={stratum.n} recall {fmt(stratum.recall)}"
                f" ({stratum.n - stratum.escalated} false negative(s))"
                for name, stratum in safety.by_stratum.items()
            ),
            "",
        ]

    if safety.false_negative_item_ids:
        lines += [
            "Red-flag false negatives (item ids, listed rather than counted so each one can be"
            " read): " + ", ".join(f"`{item}`" for item in safety.false_negative_item_ids),
            "",
        ]

    lines += [
        "**Latency.** "
        f"p50 {fmt(latency.p50_ms, digits=0, suffix=' ms')}, "
        f"p90 {fmt(latency.p90_ms, digits=0, suffix=' ms')}, n={latency.n}."
        + (
            " By route mode: "
            + "; ".join(
                f"{mode} n={count} p50 {p50:.0f} ms p90 {p90:.0f} ms"
                for mode, (count, p50, p90) in latency.by_mode.items()
            )
            + "."
            if latency.by_mode
            else " No route events: this configuration makes no routing decision, so the"
            " split by mode is undefined."
        ),
        "",
        "**Usage.** "
        f"{fmt(usage.tokens_per_turn, digits=0)} tokens per turn over "
        f"{fmt(usage.llm_calls_per_turn, digits=1)} LLM calls; "
        f"{fmt(usage.tool_calls_per_turn, digits=2)} tool calls per turn; "
        f"cost {fmt(usage.cost_per_turn_usd, digits=4)} per turn.",
        "",
    ]
    if usage.unpriced_models:
        lines += [
            "Cost is `not measured` because these models have no entry in `eval/pricing.yaml`: "
            + ", ".join(f"`{name}`" for name in usage.unpriced_models)
            + ". A partial cost would read as a complete one.",
            "",
        ]
    if usage.tool_call_distribution:
        lines += [
            "Tool-call distribution (the cap can only be justified from a distribution that was"
            " not truncated at it): "
            + ", ".join(
                f"{count} call(s): {turns}"
                for count, turns in sorted(usage.tool_call_distribution.items())
            ),
            "",
        ]
    if usage.tokens_by_caller:
        lines += [
            "Tokens by caller (the planner and synthesizer are the overhead the architecture"
            " adds, so they are counted): "
            + ", ".join(
                f"`{caller}` {total}" for caller, total in sorted(usage.tokens_by_caller.items())
            ),
            "",
        ]

    judge = result.judge
    lines += [
        "**Judge.** "
        f"faithfulness against what was retrieved {fmt(judge.faithfulness_retrieved)}; "
        f"against the golden set's relevant documents {fmt(judge.faithfulness_oracle)}; "
        f"n={judge.n_judged}; judge model `{judge.judge_model or NOT_MEASURED}` "
        f"(prompt `{judge.prompt_version or NOT_MEASURED}`)."
        + _reference_caveat(unverified, "reference_answer", "relevant_doc_ids"),
        "",
        "**Judge validation.** "
        + (
            f"raw agreement {fmt(judge.human_agreement)}, Cohen's kappa "
            f"{fmt(judge.cohens_kappa)}, over n={judge.n_human_labeled} human-labelled items."
            if judge.human_agreement is not None
            else "Agreement with a human has **not been measured**. The faithfulness numbers"
            " above therefore come from an unvalidated instrument, and must be read that way."
        ),
        "",
        "**Multi-turn.** "
        f"resolved {fmt(judge.multiturn_resolved)}; unresolved {fmt(judge.multiturn_unresolved)}; "
        f"misresolved {fmt(judge.multiturn_misresolved)}; n={judge.n_conversations}.",
        "",
    ]
    return lines


def _reference_caveat(unverified: UnverifiedLabels, *fields: str) -> str:
    """The one-clause form of the disclosure, appended to a paragraph that reports the metric.

    The table header and the line under the table say it for a reader working top to bottom; this
    says it again for one reading a single configuration's section on its own, which is how a
    reader who already knows which preset they care about actually reads the file.
    """
    named = [name for name in fields if unverified.fields.get(name)]
    if not named:
        return ""
    return (
        " Computed against "
        + " and ".join(f"`{name}`" for name in named)
        + ", which "
        + ("was" if len(named) == 1 else "were")
        + " written by a model and never verified by a person."
    )


def _plain(value: Any) -> Any:
    """Convert dataclasses, tuples and non-string dict keys into JSON-safe structures."""
    if is_dataclass(value) and not isinstance(value, type):
        return {key: _plain(item) for key, item in asdict(value).items()}
    if isinstance(value, Mapping):
        return {_key(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def _key(key: Any) -> str:
    """JSON object keys are strings; the confusion matrix is keyed by a pair."""
    if isinstance(key, tuple):
        return "->".join(str(part) for part in key)
    return str(key)


def ablation_rows(summary: RunSummary) -> Sequence[str]:
    """The ablation table's configuration names, in report order."""
    return [result.config for result in summary.results]
