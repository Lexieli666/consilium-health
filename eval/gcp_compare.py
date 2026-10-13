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
        [--config full] [--markdown docs/gcp-migration-eval.md]  # prints a table; writes a block
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


def compare(legacy_dir: Path, adk_dir: Path, *, config: str, golden: Path) -> tuple[str, list[str]]:
    items = load_golden(golden)
    a, b = summary_row(legacy_dir, config), summary_row(adk_dir, config)
    va, vb = read_run(legacy_dir, config, items), read_run(adk_dir, config, items)

    lines = [
        "| metric | legacy (`ORCHESTRATOR=legacy`) | ADK (`ORCHESTRATOR=adk`) |",
        "|---|---|---|",
        f"| agent model (from `llm_call` events) | {models(va)} | {models(vb)} |",
        f"| judge model | {a['judge_model']} | {b['judge_model']} |",
        f"| items with a `turn` event | {sum(v.present for v in va.values())}/{a['n_items']} | {sum(v.present for v in vb.values())}/{b['n_items']} |",
        f"| recall@5 (union, n labelled) | {fmt(a['recall_at_5'], 'pct')} | {fmt(b['recall_at_5'], 'pct')} |",
        f"| faithfulness, retrieved sources (judge mean, n={a['n_judged']}/{b['n_judged']}) | {fmt(a['faithfulness_retrieved'], 'pct')} | {fmt(b['faithfulness_retrieved'], 'pct')} |",
        f"| faithfulness, oracle sources | {fmt(a['faithfulness_oracle'], 'pct')} | {fmt(b['faithfulness_oracle'], 'pct')} |",
        f"| red-flag recall (post-repair) | {fmt(a['red_flag_recall'], 'pct')} | {fmt(b['red_flag_recall'], 'pct')} |",
        f"| red-flag false negatives | {', '.join(a['red_flag_fn']) or 'none'} | {', '.join(b['red_flag_fn']) or 'none'} |",
        f"| routing accuracy / fallback rate | {fmt(a['routing_accuracy'], 'pct')} / {fmt(a['fallback_rate'], 'pct')} | {fmt(b['routing_accuracy'], 'pct')} / {fmt(b['fallback_rate'], 'pct')} |",
        f"| latency p50 / p90 / **p95** | {fmt(a['p50_ms'], 'ms')} / {fmt(a['p90_ms'], 'ms')} / **{fmt(p95(va), 'ms')}** | {fmt(b['p50_ms'], 'ms')} / {fmt(b['p90_ms'], 'ms')} / **{fmt(p95(vb), 'ms')}** |",
        f"| cost per request (traced `llm_call`s, pricing.yaml) | {fmt(a['cost_per_turn_usd'], 'usd')} | {fmt(b['cost_per_turn_usd'], 'usd')} |",
        f"| tokens / LLM calls / tool calls per turn | {a['tokens_per_turn']:.0f} / {a['llm_calls_per_turn']:.2f} / {a['tool_calls_per_turn']:.2f} | {b['tokens_per_turn']:.0f} / {b['llm_calls_per_turn']:.2f} / {b['tool_calls_per_turn']:.2f} |",
        f"| run (results dir, commit, started) | `{a['run']}`, `{a['commit']}`, {a['started_at']} | `{b['run']}`, `{b['commit']}`, {b['started_at']} |",
    ]
    if a["unpriced_models"] or b["unpriced_models"]:
        lines.append(
            f"| unpriced models (cost is `not measured`) | {a['unpriced_models']} | {b['unpriced_models']} |"
        )

    deltas: list[str] = []
    for item in items:
        x, y = va[item.id], vb[item.id]
        notes: list[str] = []
        if x.present != y.present:
            notes.append(f"turn present legacy={x.present} adk={y.present}")
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
            deltas.append(
                f"- `{item.id}`: " + "; ".join(notes) + f"  (`{x.trace_path}` vs `{y.trace_path}`)"
            )
    return "\n".join(lines), deltas


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("legacy", type=Path)
    parser.add_argument("adk", type=Path)
    parser.add_argument("--config", default="full")
    parser.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN)
    args = parser.parse_args(argv)
    table, deltas = compare(args.legacy, args.adk, config=args.config, golden=args.golden)
    print(table)
    print()
    print(f"per-item differences: {len(deltas)}")
    for line in deltas:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
