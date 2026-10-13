"""Side-by-side of the published baseline and a LangGraph run, for the README's results table.

A reader of the existing harness, not a change to it.  Every aggregate comes from the two runs'
``summary.json`` files; the per-item facts and the one figure the harness does not report --
**p95 latency** -- are read off the per-turn traces with the harness's own ``eval/metrics.py``
functions (``percentile`` over ``turn.wall_ms``).  Nothing here is recomputed by a second
implementation of a metric.

Two trace layouts are read, because the baseline is published and the new run is not yet:
``eval/results/published/traces/<session>.json`` (one file per session, as ``eval/publish.py``
writes it) and ``eval/results/<stamp>/runs/<session>/0.jsonl`` (as the harness writes it).  The
layout is detected per directory.

The per-item section is the input to a failure classification, not the classification.  For every
golden item where the LangGraph run did worse than the baseline on recall@5 or on red-flag
escalation it prints the evidence a reader needs -- which tools were called, whether the red-flag
table matched, whether the draft escalated before repair -- and a *suggested* category from the
four the project uses (routing miss, retrieval miss, unsupported answer, tool-result misuse),
derived by the rules in :func:`suggest_category`.  "Unsupported answer" cannot be read off a trace
(the judge's per-item verdicts are not stored), so it is only ever suggested as "read the trace".

Usage::

    uv run python -m eval.graph_compare eval/results/<langgraph-run> \\
        [--baseline eval/results/published] [--config full] [--graph-config full]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from consilium.trace import AnyEvent, LLMCallEvent, ToolCallEvent, parse_event, read_trace
from eval.harness import session_id_for
from eval.items import GoldenItem, load_golden
from eval.metrics import (
    RetrievalOutcome,
    llm_call_cost,
    percentile,
    recall_at_k,
    retrieval_outcome,
    turn_event,
)
from eval.run import DEFAULT_GOLDEN, load_pricing

#: The published rows a LangGraph run is set beside, in report order.
BASELINE_CONFIGS: tuple[str, ...] = ("baseline_llm", "single_agent_rag", "full", "full_no_memory")


@dataclass(frozen=True)
class ItemView:
    """What one run did on one item, read off its trace."""

    item_id: str
    #: A trace file exists for the item; ``False`` for an item a ``--limit`` run never reached.
    attempted: bool
    present: bool
    recall_at_5: float | None
    retrieved_any: bool
    red_flag_item: bool
    red_flag_matched: bool | None
    escalated_pre_repair: bool | None
    escalated: bool | None
    repair_applied: bool | None
    tools: tuple[str, ...]
    wall_ms: float | None
    cost_usd: float | None
    llm_calls: int
    models: tuple[str, ...]
    trace_path: str


def load_turn_events(
    run_dir: Path, session: str, turn_index: int = 0
) -> tuple[list[AnyEvent], Path] | None:
    """The events of one turn, from whichever layout ``run_dir`` uses."""
    published = run_dir / "traces" / f"{session}.json"
    if published.is_file():
        payload = json.loads(published.read_text(encoding="utf-8"))
        for turn in payload["turns"]:
            if int(turn["turn_index"]) == turn_index:
                return [parse_event(json.dumps(event)) for event in turn["events"]], published
        return None
    raw = run_dir / "runs" / session / f"{turn_index}.jsonl"
    if raw.is_file():
        return read_trace(raw), raw
    return None


def read_run(run_dir: Path, config: str, items: Sequence[GoldenItem]) -> dict[str, ItemView]:
    pricing = load_pricing()
    views: dict[str, ItemView] = {}
    for item in items:
        session = session_id_for(item.id, prefix=config)
        loaded = load_turn_events(run_dir, session)
        if loaded is None:
            views[item.id] = ItemView(
                item.id,
                False,
                False,
                None,
                False,
                bool(item.red_flag),
                None,
                None,
                None,
                None,
                (),
                None,
                None,
                0,
                (),
                str(run_dir / session),
            )
            continue
        events, path = loaded
        turn = turn_event(events)
        retrieval: RetrievalOutcome | None = retrieval_outcome(item, events)
        cost, missing = llm_call_cost(events, pricing=pricing)
        tools = tuple(e.skill for e in events if isinstance(e, ToolCallEvent))
        views[item.id] = ItemView(
            item_id=item.id,
            attempted=True,
            present=turn is not None,
            recall_at_5=(
                recall_at_k(retrieval.union_top5, retrieval.relevant) if retrieval else None
            ),
            retrieved_any=bool(retrieval and retrieval.union_top5),
            red_flag_item=bool(item.red_flag),
            red_flag_matched=turn.red_flag_matched if turn else None,
            escalated_pre_repair=turn.escalation_present_pre_repair if turn else None,
            escalated=turn.escalation_present_post_repair if turn else None,
            repair_applied=turn.repair_applied if turn else None,
            tools=tools,
            wall_ms=turn.wall_ms if turn else None,
            cost_usd=None if missing else cost,
            llm_calls=sum(isinstance(e, LLMCallEvent) for e in events),
            models=tuple(sorted({e.model for e in events if isinstance(e, LLMCallEvent)})),
            trace_path=str(path),
        )
    return views


def summary_row(run_dir: Path, config: str) -> dict[str, Any] | None:
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    result = next((r for r in summary["results"] if r["config"] == config), None)
    if result is None:
        return None
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
        "recall_n": result["retrieval"]["n"],
        "faithfulness_retrieved": judge.get("faithfulness_retrieved"),
        "faithfulness_oracle": judge.get("faithfulness_oracle"),
        "n_judged": judge.get("n_judged"),
        "red_flag_recall": result["safety"]["red_flag_recall"],
        "red_flag_n": result["safety"]["n_red_flag_items"],
        "red_flag_fn": result["safety"]["false_negative_item_ids"],
        "violations_per_100": result["safety"]["violations_per_100_turns"],
        "repairs_per_100": result["safety"]["repairs_per_100_turns"],
        "routing_accuracy": result["routing"]["accuracy"],
        "fallback_rate": result["routing"]["fallback_rate"],
        "p50_ms": result["latency"]["p50_ms"],
        "p90_ms": result["latency"]["p90_ms"],
        "cost_per_turn_usd": result["usage"]["cost_per_turn_usd"],
        "tokens_per_turn": result["usage"]["tokens_per_turn"],
        "llm_calls_per_turn": result["usage"]["llm_calls_per_turn"],
        "tool_calls_per_turn": result["usage"]["tool_calls_per_turn"],
        "unpriced_models": result["usage"]["unpriced_models"],
    }


def p95(views: dict[str, ItemView]) -> float | None:
    return percentile([v.wall_ms for v in views.values() if v.wall_ms is not None], 95)


def fmt(value: Any, kind: str = "num") -> str:
    if value is None:
        return "n/a"
    if kind == "pct":
        return f"{float(value) * 100:.1f}%"
    if kind == "ms":
        return f"{float(value) / 1000:.2f} s"
    if kind == "usd":
        return f"${float(value):.5f}"
    if kind == "per100":
        return f"{float(value):.1f}"
    return str(value)


def column(run_dir: Path, config: str, items: Sequence[GoldenItem]) -> dict[str, Any] | None:
    """One table column: the summary row plus p95 and the agent model read off the traces."""
    row = summary_row(run_dir, config)
    if row is None:
        return None
    views = read_run(run_dir, config, items)
    row["p95_ms"] = p95(views)
    row["present"] = sum(v.present for v in views.values())
    row["agent_models"] = ", ".join(sorted({m for v in views.values() for m in v.models})) or "n/a"
    row["cost_per_run_usd"] = (
        None if row["cost_per_turn_usd"] is None else row["cost_per_turn_usd"] * row["n_items"]
    )
    row["views"] = views
    return row


METRICS: tuple[tuple[str, str, str], ...] = (
    ("items with a `turn` event", "present", "num"),
    ("retrieval recall@5 (union; n labelled)", "recall_at_5", "pct"),
    ("faithfulness, retrieved sources (judge mean)", "faithfulness_retrieved", "pct"),
    ("faithfulness, oracle sources (judge mean)", "faithfulness_oracle", "pct"),
    ("red-flag recall, post-repair (n=28)", "red_flag_recall", "pct"),
    ("red-flag false negatives", "red_flag_fn", "list"),
    ("output violations / repairs per 100 turns", "violations_per_100", "viol"),
    ("routing accuracy / planner fallback rate", "routing_accuracy", "routing"),
    ("latency p50", "p50_ms", "ms"),
    ("latency p90", "p90_ms", "ms"),
    ("latency p95 (from `turn.wall_ms`, this script)", "p95_ms", "ms"),
    ("cost per turn (traced `llm_call`s, `eval/pricing.yaml`)", "cost_per_turn_usd", "usd"),
    ("cost per run (cost per turn x n items)", "cost_per_run_usd", "usd"),
    ("tokens / LLM calls / tool calls per turn", "tokens_per_turn", "usage"),
    ("agent model (from `llm_call` events)", "agent_models", "num"),
    ("judge model", "judge_model", "num"),
    ("run directory, commit, started", "run", "run"),
)


def cell(row: dict[str, Any] | None, key: str, kind: str) -> str:
    if row is None:
        return "not run"
    if kind == "list":
        return ", ".join(row[key]) or "none"
    if kind == "viol":
        return (
            f"{fmt(row['violations_per_100'], 'per100')} / {fmt(row['repairs_per_100'], 'per100')}"
        )
    if kind == "routing":
        if row["routing_accuracy"] is None:
            return "n/a (no `route` event)"
        return f"{fmt(row['routing_accuracy'], 'pct')} / {fmt(row['fallback_rate'], 'pct')}"
    if kind == "usage":
        return (
            f"{row['tokens_per_turn']:.0f} / {row['llm_calls_per_turn']:.2f} / "
            f"{row['tool_calls_per_turn']:.2f}"
        )
    if kind == "run":
        return f"`{row['run']}`, `{row['commit']}`, {row['started_at']}"
    return fmt(row[key], kind)


def table(columns: list[tuple[str, dict[str, Any] | None]]) -> str:
    header = "| metric | " + " | ".join(name for name, _ in columns) + " |"
    rule = "|---|" + "---|" * len(columns)
    lines = [header, rule]
    for label, key, kind in METRICS:
        lines.append(
            f"| {label} | " + " | ".join(cell(row, key, kind) for _, row in columns) + " |"
        )
    return "\n".join(lines)


def suggest_category(item: GoldenItem, base: ItemView, graph: ItemView) -> str | None:
    """A *suggested* failure category for an item the graph did worse on; rules stated inline."""
    worse_recall = (
        base.recall_at_5 is not None
        and graph.recall_at_5 is not None
        and graph.recall_at_5 < base.recall_at_5
    )
    lost_escalation = item.red_flag and base.escalated and not graph.escalated
    if not graph.present:
        return "turn failed (no `turn` event): read the trace"
    if worse_recall and not graph.retrieved_any:
        return "routing miss (planner called no corpus-backed tool)"
    if worse_recall:
        return "retrieval miss (a corpus tool ran; the relevant note was not in its top 5)"
    if lost_escalation and graph.red_flag_matched:
        return "tool-result misuse (red-flag table matched, draft did not escalate, repair did not restore it)"
    if lost_escalation:
        return "unsupported answer or escalation miss (table did not match): read the trace"
    return None


def deltas(
    items: Sequence[GoldenItem], base: dict[str, ItemView], graph: dict[str, ItemView]
) -> list[str]:
    lines: list[str] = []
    for item in items:
        x, y = base[item.id], graph[item.id]
        if not y.attempted:
            continue  # a --limit run never reached it; absence is not a failure
        notes: list[str] = []
        if x.present != y.present:
            notes.append(f"turn present baseline={x.present} langgraph={y.present}")
        if (
            x.recall_at_5 is not None
            and y.recall_at_5 is not None
            and x.recall_at_5 != y.recall_at_5
        ):
            notes.append(f"recall@5 {x.recall_at_5:.2f} -> {y.recall_at_5:.2f}")
        if item.red_flag and x.escalated != y.escalated:
            notes.append(f"red-flag escalated {x.escalated} -> {y.escalated}")
        if not notes:
            continue
        suggestion = suggest_category(item, x, y)
        evidence = (
            f"tools baseline={list(x.tools)} langgraph={list(y.tools)}; "
            f"red_flag_matched={y.red_flag_matched}; escalated_pre_repair={y.escalated_pre_repair}; "
            f"repair_applied={y.repair_applied}; llm_calls={x.llm_calls}->{y.llm_calls}"
        )
        lines.append(
            f"- `{item.id}`: "
            + "; ".join(notes)
            + f"\n    evidence: {evidence}"
            + (
                f"\n    suggested: {suggestion}"
                if suggestion
                else "\n    suggested: improvement, not a failure"
            )
            + f"\n    traces: `{x.trace_path}` vs `{y.trace_path}`"
        )
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("langgraph", type=Path, help="the LangGraph run (results dir)")
    parser.add_argument("--baseline", type=Path, default=Path("eval/results/published"))
    parser.add_argument("--config", default="full", help="baseline config for per-item deltas")
    parser.add_argument("--graph-config", default="full", help="config name used in the graph run")
    parser.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN)
    parser.add_argument("--no-deltas", action="store_true")
    args = parser.parse_args(argv)

    items = load_golden(args.golden)
    columns: list[tuple[str, dict[str, Any] | None]] = [
        (f"`{name}` (legacy, published)", column(args.baseline, name, items))
        for name in BASELINE_CONFIGS
    ]
    graph_column = (
        column(args.langgraph, args.graph_config, items) if args.langgraph.exists() else None
    )
    columns.append((f"`{args.graph_config}` (`ORCHESTRATOR=langgraph`)", graph_column))
    print(table(columns))
    if args.no_deltas or graph_column is None:
        if graph_column is None:
            print(f"\n(no LangGraph run at {args.langgraph}; the column reads `not run`)")
        return 0

    base = next(row for name, row in columns if name.startswith(f"`{args.config}`"))
    if base is None:
        print(f"\nbaseline config {args.config!r} is not in {args.baseline}", file=sys.stderr)
        return 2
    lines = deltas(items, base["views"], graph_column["views"])
    attempted = sum(v.attempted for v in graph_column["views"].values())
    print(
        f"\nper-item differences against `{args.config}` over {attempted} attempted item(s): {len(lines)}"
    )
    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
