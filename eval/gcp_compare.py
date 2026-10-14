"""Side-by-side of two golden-set runs, for docs/gcp-migration-eval.md.

A reader of the existing harness, not a change to it: every number comes from ``summary.json``
and the per-turn traces a run wrote, through the same ``eval/metrics.py`` functions that produced
them, plus the one figure the harness does not report -- **p95 latency** -- computed here from the
``turn.wall_ms`` of the same traces with the harness's own :func:`eval.metrics.percentile`.

Per-item deltas are what makes a regression explainable rather than merely visible: for every
item the script says whether recall@5, the red-flag escalation and the route changed between the
two runs, and names the trace file so the cause can be read off the events.

Usage::

    uv run python -m eval.gcp_compare eval/results/<legacy-run> eval/results/<adk-run>
        [--config full] [--golden <path>] > docs/gcp-migration-eval.md   # prints the block
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from consilium.trace import AnyEvent, LLMCallEvent, read_trace
from eval.harness import session_id_for
from eval.items import GoldenItem, load_golden
from eval.metrics import (
    llm_call_cost,
    percentile,
    recall_at_k,
    retrieval_outcome,
    route_event,
    routing_outcome,
    turn_event,
)
from eval.run import DEFAULT_GOLDEN, load_pricing

#: The judge that graded ``eval/results/published/`` and the only judge whose agreement with a
#: human has been measured on this project (Cohen's kappa 0.592, n=40, docs/EVALUATION.md §4.2).
#: A run graded by anything else produces faithfulness numbers that are comparable within that
#: run's pair and **not** to the published column, and :func:`judge_caveat` says so underneath
#: the table.  It is emitted by the tool and not typed into the document, because a caveat that
#: has to be re-typed every time the table is regenerated is a caveat that will eventually be
#: left off the regeneration that matters.
VALIDATED_JUDGE = "gpt-4o-mini"


@dataclass(frozen=True)
class ItemView:
    """What one run did on one item, read off its trace."""

    item_id: str
    present: bool
    recall_at_5: float | None
    red_flag_item: bool | None
    escalated: bool | None
    repair_applied: bool | None
    route_agents: tuple[str, ...]
    route_fallback: bool | None
    wall_ms: float | None
    cost_usd: float | None
    models: tuple[str, ...]
    trace_path: str


def read_run(run_dir: Path, config: str, items: Sequence[GoldenItem]) -> dict[str, ItemView]:
    pricing = load_pricing()
    views: dict[str, ItemView] = {}
    for item in items:
        session = session_id_for(item.id, prefix=config)
        path = run_dir / "runs" / session / "0.jsonl"
        if not path.is_file():
            views[item.id] = ItemView(
                item.id, False, None, item.red_flag, None, None, (), None, None, None, (), str(path)
            )
            continue
        events: list[AnyEvent] = read_trace(path)
        turn = turn_event(events)
        retrieval = retrieval_outcome(item, events)
        route = route_event(events)
        routing = routing_outcome(item, events)
        cost, missing = llm_call_cost(events, pricing=pricing)
        views[item.id] = ItemView(
            item_id=item.id,
            present=turn is not None,
            recall_at_5=(
                recall_at_k(retrieval.union_top5, retrieval.relevant) if retrieval else None
            ),
            red_flag_item=item.red_flag,
            escalated=turn.escalation_present_post_repair if turn else None,
            repair_applied=turn.repair_applied if turn else None,
            route_agents=tuple(route.agents) if route else (),
            route_fallback=route.fallback if route else None,
            wall_ms=turn.wall_ms if turn else None,
            cost_usd=None if missing else cost,
            models=tuple(sorted({e.model for e in events if isinstance(e, LLMCallEvent)})),
            trace_path=str(path.relative_to(run_dir.parent.parent)),
        )
        del routing  # routing accuracy is read from summary.json; the outcome is for the delta
    return views


def summary_row(run_dir: Path, config: str) -> dict[str, Any]:
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    result = next(r for r in summary["results"] if r["config"] == config)
    judge = result.get("judge") or {}
    return {
        "run": run_dir.name,
        "commit": summary.get("commit"),
        "started_at": summary.get("started_at"),
        "provider": summary.get("provider"),
        "model": summary.get("model"),
        "judge_model": judge.get("judge_model"),
        "n_items": result["n_items"],
        "recall_at_5": result["retrieval"]["recall_at_5_union"],
        "faithfulness_retrieved": judge.get("faithfulness_retrieved"),
        "faithfulness_oracle": judge.get("faithfulness_oracle"),
        "n_judged": judge.get("n_judged"),
        "red_flag_recall": result["safety"]["red_flag_recall"],
        "red_flag_fn": result["safety"]["false_negative_item_ids"],
        "routing_accuracy": result["routing"]["accuracy"],
        "fallback_rate": result["routing"]["fallback_rate"],
        "p50_ms": result["latency"]["p50_ms"],
        "p90_ms": result["latency"]["p90_ms"],
        "cost_per_turn_usd": result["usage"]["cost_per_turn_usd"],
        "tokens_per_turn": result["usage"]["tokens_per_turn"],
        "llm_calls_per_turn": result["usage"]["llm_calls_per_turn"],
        "tool_calls_per_turn": result["usage"]["tool_calls_per_turn"],
        "unpriced_models": result["usage"]["unpriced_models"],
        "cost_cap": summary.get("cost_cap"),
    }


def judge_caveat(*judge_models: str | None) -> list[str]:
    """The footnote for a run graded by an unvalidated judge.  Empty when there is nothing to say."""
    others = sorted({m for m in judge_models if m and not m.startswith(VALIDATED_JUDGE)})
    if not others:
        return []
    return [
        "",
        f"**The judge is not the published one.** These runs were graded by "
        f"{', '.join(f'`{m}`' for m in others)}; `eval/results/published/` was graded by "
        f"`{VALIDATED_JUDGE}`, which is the only judge on this project whose agreement with a "
        "human has been measured (Cohen's kappa 0.592, n=40, `docs/EVALUATION.md` §4.2, itself "
        "0.008 below the 0.6 usability line). **No kappa has been measured for the judge used "
        "here**, on `eval/judges/faithfulness_v2.md` or any other prompt. The two faithfulness "
        "rows above are therefore comparable to each other and not to any published number, and "
        "re-validating would take a fresh blind sample (`--human-sample 40 --sample-seed <new> "
        "--exclude-sample <each prior CSV>`) and 40 human labels.",
    ]


def models(views: dict[str, ItemView]) -> str:
    return ", ".join(sorted({m for v in views.values() for m in v.models})) or "n/a"


def p95(views: dict[str, ItemView]) -> float | None:
    return percentile([v.wall_ms for v in views.values() if v.wall_ms is not None], 95)


def fmt(value: Any, kind: str = "num") -> str:
    if value is None:
        return "not measured"
    if kind == "pct":
        return f"{float(value) * 100:.1f}%"
    if kind == "ms":
        return f"{float(value) / 1000:.2f} s"
    if kind == "usd":
        return f"${float(value):.5f}"
    return str(value)


def _label_for(row: dict[str, Any], views: dict[str, ItemView]) -> str:
    """A column heading that names what actually ran, not what was asked for.

    The orchestrator is not in ``summary.json`` -- it is a deployment setting, not a result -- so
    the heading is built from the two facts that are: the provider the run was configured with and
    the agent model its own ``llm_call`` events name.  That is what distinguishes the third arm
    (legacy router, Gemini agents) from the second (ADK tree, Gemini agents), which would otherwise
    print identical headings.
    """
    return f"{row['run']}<br/>`{row['provider']}` · {models(views)}"


def compare(run_dirs: Sequence[Path], *, config: str, golden: Path) -> tuple[str, list[str]]:
    """A column per run, and per-item deltas of every later run against the first.

    Two runs was the original shape and is still the common one; three is what
    ``docs/gcp-migration-eval.md`` needs to separate the orchestrator from the agent model, and a
    hand-assembled third column would be the one number in that document nobody could recompute.
    The first directory is the baseline every delta is measured against.
    """
    if len(run_dirs) < 2:
        raise ValueError("compare needs at least two runs")

    items = load_golden(golden)
    rows = [summary_row(d, config) for d in run_dirs]
    views = [read_run(d, config, items) for d in run_dirs]
    n = len(rows)

    def cells(render: Any) -> str:
        return " | ".join(render(row, view) for row, view in zip(rows, views, strict=True))

    lines = [
        "| metric | "
        + " | ".join(_label_for(r, v) for r, v in zip(rows, views, strict=True))
        + " |",
        "|---" * (n + 1) + "|",
        "| agent model (from `llm_call` events) | " + cells(lambda _r, v: models(v)) + " |",
        "| judge model | " + cells(lambda r, _v: str(r["judge_model"])) + " |",
        "| items with a `turn` event | "
        + cells(lambda r, v: f"{sum(x.present for x in v.values())}/{r['n_items']}")
        + " |",
        "| recall@5 (union, n labelled) | "
        + cells(lambda r, _v: fmt(r["recall_at_5"], "pct"))
        + " |",
        "| faithfulness, retrieved sources (judge mean) | "
        + cells(lambda r, _v: f"{fmt(r['faithfulness_retrieved'], 'pct')} (n={r['n_judged']})")
        + " |",
        "| faithfulness, oracle sources | "
        + cells(lambda r, _v: fmt(r["faithfulness_oracle"], "pct"))
        + " |",
        "| **red-flag recall (post-repair)** | "
        + cells(lambda r, _v: f"**{fmt(r['red_flag_recall'], 'pct')}**")
        + " |",
        "| red-flag false negatives | "
        + cells(lambda r, _v: ", ".join(r["red_flag_fn"]) or "none")
        + " |",
        "| routing accuracy / fallback rate | "
        + cells(
            lambda r, _v: f"{fmt(r['routing_accuracy'], 'pct')} / {fmt(r['fallback_rate'], 'pct')}"
        )
        + " |",
        "| latency p50 / p90 / **p95** | "
        + cells(
            lambda r, v: (
                f"{fmt(r['p50_ms'], 'ms')} / {fmt(r['p90_ms'], 'ms')} / **{fmt(p95(v), 'ms')}**"
            )
        )
        + " |",
        "| cost per request (traced `llm_call`s, pricing.yaml) | "
        + cells(lambda r, _v: fmt(r["cost_per_turn_usd"], "usd"))
        + " |",
        "| tokens / LLM calls / tool calls per turn | "
        + cells(
            lambda r, _v: (
                f"{r['tokens_per_turn']:.0f} / {r['llm_calls_per_turn']:.2f} / "
                f"{r['tool_calls_per_turn']:.2f}"
            )
        )
        + " |",
        "| run (commit, started) | "
        + cells(lambda r, _v: f"`{r['commit']}`, {r['started_at']}")
        + " |",
    ]
    if any(r["unpriced_models"] for r in rows):
        lines.append(
            "| unpriced models (cost is `not measured`) | "
            + cells(lambda r, _v: str(r["unpriced_models"]) or "none")
            + " |"
        )

    base_row, base_view = rows[0], views[0]
    deltas: list[str] = []
    for other_row, other_view in zip(rows[1:], views[1:], strict=True):
        header = f"\n### `{base_row['run']}` → `{other_row['run']}`\n"
        block: list[str] = []
        for item in items:
            x, y = base_view[item.id], other_view[item.id]
            notes: list[str] = []
            if x.present != y.present:
                notes.append(f"turn present {x.present} -> {y.present}")
            if (
                x.recall_at_5 is not None
                and y.recall_at_5 is not None
                and x.recall_at_5 != y.recall_at_5
            ):
                notes.append(f"recall@5 {x.recall_at_5:.2f} -> {y.recall_at_5:.2f}")
            if item.red_flag and x.escalated != y.escalated:
                notes.append(f"red-flag escalated {x.escalated} -> {y.escalated}")
            if x.route_agents != y.route_agents:
                notes.append(f"route {list(x.route_agents)} -> {list(y.route_agents)}")
            if x.route_fallback != y.route_fallback:
                notes.append(f"planner fallback {x.route_fallback} -> {y.route_fallback}")
            if notes:
                block.append(
                    f"- `{item.id}`: "
                    + "; ".join(notes)
                    + f"  (`{x.trace_path}` vs `{y.trace_path}`)"
                )
        deltas.append(header + f"{len(block)} item(s) differ")
        deltas.extend(block)

    lines += judge_caveat(*(str(r["judge_model"]) if r["judge_model"] else None for r in rows))
    return "\n".join(lines), deltas


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "runs",
        type=Path,
        nargs="+",
        help="two or more eval/results/<run> directories; the first is the baseline",
    )
    parser.add_argument("--config", default="full")
    parser.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN)
    args = parser.parse_args(argv)
    if len(args.runs) < 2:
        parser.error("give at least two run directories")
    table, deltas = compare(args.runs, config=args.config, golden=args.golden)
    print(table)
    print()
    print("## Per-item differences against the baseline")
    for line in deltas:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
