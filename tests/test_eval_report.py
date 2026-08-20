"""Report generation: `not measured`, `n/a`, and the JSON that has to survive being committed."""

from __future__ import annotations

import json
from pathlib import Path

from eval.metrics import (
    LatencyReport,
    RetrievalReport,
    RoutingReport,
    SafetyReport,
    UsageReport,
)
from eval.report import (
    NOT_APPLICABLE,
    NOT_MEASURED,
    ConfigResult,
    JudgeReport,
    RunSummary,
    fmt,
    fmt_cell,
    render_markdown,
    to_json,
    write_results,
)


def _result(config: str, **kwargs: object) -> ConfigResult:
    defaults: dict[str, object] = {
        "routing": RoutingReport(
            n=10,
            accuracy=0.8,
            accuracy_excluding_fallback=0.9,
            n_excluding_fallback=9,
            fallback_rate=0.1,
            confusion={("single", "single"): 8, ("parallel", "single"): 2},
            per_agent_set={"consultation": (8, 10)},
        ),
        "retrieval": RetrievalReport(10, 0.7, 0.6, 0.9, 0.55, 8.0, 1.4),
        "safety": SafetyReport(
            n_red_flag_items=4,
            red_flag_recall=0.75,
            red_flag_false_negatives=1,
            false_negative_item_ids=("g-042",),
            model_escalated_unaided=0.5,
            violations_per_100_turns=120.0,
            repairs_per_100_turns=100.0,
            violations_by_rule={"disclaimer": 10},
            repairs_by_rule={"disclaimer": 10},
            post_stream_repairs=0,
            negation_suppressed_turns=2,
        ),
        "latency": LatencyReport(10, 900.0, 2400.0, {"single": (8, 800.0, 1900.0)}),
        "usage": UsageReport(
            n_turns=10,
            tokens_per_turn=3200.0,
            prompt_tokens_per_turn=2900.0,
            completion_tokens_per_turn=300.0,
            llm_calls_per_turn=2.4,
            tokens_by_caller={"planner": 4000},
            tokens_by_mode={"single": 3200.0},
            tool_calls_per_turn=1.2,
            tool_call_distribution={0: 2, 1: 5, 2: 3},
            tool_calls_by_skill={"search_knowledge": 12},
            cost_per_turn_usd=None,
            unpriced_models=("openai/gpt-4o-mini",),
        ),
        "judge": JudgeReport(),
    }
    defaults.update(kwargs)
    return ConfigResult(config=config, n_items=10, **defaults)  # type: ignore[arg-type]


def _summary(*results: ConfigResult) -> RunSummary:
    return RunSummary(
        commit="abc123def456",
        started_at="2026-08-20T10:00:00+00:00",
        finished_at="2026-08-20T10:30:00+00:00",
        provider="openai",
        model="gpt-4o-mini",
        judge_model=None,
        golden_path="eval/data/golden.jsonl",
        multiturn_path="eval/data/multiturn.jsonl",
        n_items=150,
        limit=None,
        python="3.12.0",
        platform="Linux 6.6",
        pricing_source="pricing.yaml is empty, so cost is reported as not measured",
        results=list(results),
    )


def test_a_missing_metric_renders_as_not_measured_never_as_zero() -> None:
    assert fmt(None) == NOT_MEASURED
    assert fmt(0.0) == "0.000"
    assert fmt(1234.0, digits=0, suffix=" ms") == "1234 ms"


def test_a_structurally_undefined_cell_renders_as_n_a() -> None:
    """`n/a` and `not measured` are different claims and the table keeps them apart."""
    assert fmt_cell(None, "baseline_llm", "routing_accuracy") == NOT_APPLICABLE
    assert fmt_cell(0.8, "baseline_llm", "routing_accuracy") == NOT_APPLICABLE
    assert fmt_cell(None, "full", "routing_accuracy") == NOT_MEASURED
    assert fmt_cell(0.8, "full", "routing_accuracy") == "0.800"


def test_the_oracle_faithfulness_column_is_defined_for_the_baseline() -> None:
    """The control retrieves nothing, so the retrieved column is n/a and the oracle one is not."""
    assert fmt_cell(None, "baseline_llm", "faithfulness_retrieved") == NOT_APPLICABLE
    assert fmt_cell(0.5, "baseline_llm", "faithfulness_oracle") == "0.500"


def test_the_markdown_report_leads_with_the_ablation_table() -> None:
    text = render_markdown(_summary(_result("baseline_llm"), _result("full")))

    assert text.index("## Ablation") < text.index("## Per configuration")
    assert "Not medical advice." in text
    assert "`abc123def456`" in text
    assert "| `baseline_llm` | n/a | n/a |" in text


def test_the_report_names_red_flag_false_negatives_rather_than_only_counting_them() -> None:
    text = render_markdown(_summary(_result("full")))

    assert "**1 false negative(s)**" in text
    assert "`g-042`" in text


def test_the_report_says_the_judge_is_unvalidated_until_it_is_measured() -> None:
    text = render_markdown(_summary(_result("full")))
    assert "has **not been measured**" in text

    validated = _result(
        "full", judge=JudgeReport(human_agreement=0.9, cohens_kappa=0.72, n_human_labeled=40)
    )
    assert "Cohen's kappa 0.720" in render_markdown(_summary(validated))


def test_the_report_explains_why_cost_is_not_measured() -> None:
    text = render_markdown(_summary(_result("full")))
    assert "no entry in `eval/pricing.yaml`" in text
    assert "A partial cost would read as a complete one." in text


def test_the_report_prints_the_tool_call_distribution_not_only_its_mean() -> None:
    text = render_markdown(_summary(_result("full")))
    assert "Tool-call distribution" in text
    assert "1 call(s): 5" in text


def test_a_configuration_without_route_events_says_the_mode_split_is_undefined() -> None:
    text = render_markdown(
        _summary(_result("single_agent_rag", latency=LatencyReport(10, 800.0, 1500.0, {})))
    )
    assert "the split by mode is undefined" in text


def test_none_survives_into_the_committed_json() -> None:
    """`None` is the difference between measured-as-zero and not measured; dropping it erases it."""
    payload = json.loads(to_json(_summary(_result("full"))))

    (result,) = payload["results"]
    assert result["usage"]["cost_per_turn_usd"] is None
    assert result["judge"]["faithfulness_retrieved"] is None
    assert result["judge"]["human_agreement"] is None


def test_the_confusion_matrix_keys_survive_json_serialization() -> None:
    payload = json.loads(to_json(_summary(_result("full"))))
    assert payload["results"][0]["routing"]["confusion"] == {
        "single->single": 8,
        "parallel->single": 2,
    }


def test_writing_results_produces_both_artifacts(tmp_path: Path) -> None:
    """A published number whose evidence is not committed is a number nobody can check."""
    summary_path, report_path = write_results(tmp_path / "run", _summary(_result("full")))

    assert summary_path.name == "summary.json"
    assert report_path.name == "report.md"
    assert json.loads(summary_path.read_text(encoding="utf-8"))["commit"] == "abc123def456"
    assert report_path.read_text(encoding="utf-8").startswith("# Evaluation results")
