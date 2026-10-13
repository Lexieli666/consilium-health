"""`eval/graph_compare.py`: reads both trace layouts, adds p95, suggests but does not decide.

The published run is the fixture: its numbers are the committed evidence, so the assertions here
are that the script reads them back unchanged (recall@5 from `summary.json`, p95 from the traces
with the harness's own percentile) and that the suggested-category rules say what the docstring
says they say.
"""

from __future__ import annotations

from pathlib import Path

from eval.graph_compare import (
    BASELINE_CONFIGS,
    ItemView,
    column,
    load_turn_events,
    suggest_category,
    table,
)
from eval.items import load_golden
from eval.metrics import percentile, turn_event
from eval.run import DEFAULT_GOLDEN

ROOT = Path(__file__).resolve().parents[1]
PUBLISHED = ROOT / "eval" / "results" / "published"


def test_the_published_columns_read_back_their_own_numbers() -> None:
    items = load_golden(DEFAULT_GOLDEN)
    full = column(PUBLISHED, "full", items)
    assert full is not None
    assert full["n_items"] == 150 and full["present"] == 150
    assert abs(full["recall_at_5"] - 0.8568232662192394) < 1e-12
    # p95 is the harness's percentile over the same 150 wall_ms values the script read.
    walls = [v.wall_ms for v in full["views"].values() if v.wall_ms is not None]
    assert len(walls) == 150
    assert full["p95_ms"] == percentile(walls, 95)
    assert full["p90_ms"] <= full["p95_ms"]
    assert full["agent_models"] == "gpt-4o-mini-2024-07-18"


def test_the_published_layout_is_read_per_turn() -> None:
    loaded = load_turn_events(PUBLISHED, "full-g-cc-010")
    assert loaded is not None
    events, path = loaded
    assert path.name == "full-g-cc-010.json"
    turn = turn_event(events)
    assert turn is not None and turn.session_id == "full-g-cc-010"


def test_the_table_marks_a_missing_run_as_not_run() -> None:
    items = load_golden(DEFAULT_GOLDEN)[:1]
    columns = [(f"`{name}`", column(PUBLISHED, name, items)) for name in BASELINE_CONFIGS]
    columns.append(("`full` (langgraph)", None))
    rendered = table(columns)
    assert rendered.count("not run") == rendered.count("\n") - 1  # every metric row
    assert "n/a (no `route` event)" in rendered


def _view(**overrides: object) -> ItemView:
    base = {
        "item_id": "g-x",
        "attempted": True,
        "present": True,
        "recall_at_5": 1.0,
        "retrieved_any": True,
        "red_flag_item": False,
        "red_flag_matched": False,
        "escalated_pre_repair": False,
        "escalated": False,
        "repair_applied": False,
        "tools": ("search_knowledge",),
        "wall_ms": 1.0,
        "cost_usd": 0.0,
        "llm_calls": 2,
        "models": ("m",),
        "trace_path": "t",
    }
    base.update(overrides)
    return ItemView(**base)  # type: ignore[arg-type]


def test_suggested_categories_follow_the_stated_rules() -> None:
    item = next(item for item in load_golden(DEFAULT_GOLDEN) if item.red_flag)
    base = _view(escalated=True)
    assert suggest_category(item, base, _view(present=False)) is not None
    assert str(
        suggest_category(item, base, _view(recall_at_5=0.0, retrieved_any=False))
    ).startswith("routing miss")
    assert str(suggest_category(item, base, _view(recall_at_5=0.0))).startswith("retrieval miss")
    assert str(suggest_category(item, base, _view(red_flag_matched=True))).startswith(
        "tool-result misuse"
    )
    assert str(suggest_category(item, base, _view(red_flag_matched=False))).startswith(
        "unsupported"
    )
    assert suggest_category(item, base, _view(escalated=True)) is None
