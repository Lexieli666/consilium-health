"""``--max-cost``: the spend guard that stops a sweep before it finishes spending the budget.

The full sweep is the one paid step in this project, and its cost is not known in advance -- that
is what ``--limit 10`` is run to find out. The cap is the guard for the run after that one, and the
three properties it has to have are the three these tests are about: it fires, it refuses to
pretend it can fire when the model has no published rate, and it changes nothing at all when it is
not passed.

The cost is priced from ``llm_call`` trace events through ``eval/pricing.yaml``, like every other
number in ``eval/metrics.py`` -- so the fake pricing table below is the only fixture the accounting
needs, and the token counts come from the same ``Usage`` the provider reported.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from consilium.config import Settings, get_preset
from consilium.llm import MockProvider, ScriptedResponse, Usage
from consilium.runtime import Runtime, build_runtime
from consilium.trace import LLMCallEvent, TraceEvent
from eval.items import ExpectedRoute, GoldenItem
from eval.metrics import llm_call_cost, rate_for, turn_event
from eval.report import CostCap, RunSummary, UnverifiedLabels, render_markdown, to_json
from eval.run import (
    EXIT_COST_CAP,
    CostMeter,
    UnpriceableCapError,
    build_parser,
    refuse_unpriceable_cap,
    sweep,
)

ROOT = Path(__file__).resolve().parents[1]

#: A rate card that is not a price list. One dollar per million tokens each way, so a call whose
#: usage is pinned below costs exactly what the arithmetic says and a cap can be placed between
#: two items rather than near them.
FAKE_PRICING: dict[str, dict[str, float]] = {"mock/mock-model": {"input": 1.0, "output": 1.0}}

#: Pinned rather than derived from the prompt text: an item's cost has to be the same on every
#: machine for a cap to fire after a known number of them. 400_000 + 600_000 tokens at the rates
#: above is exactly $1.00 per call, and `baseline_llm` makes exactly one call per item.
ONE_DOLLAR = Usage(prompt_tokens=400_000, completion_tokens=600_000)


def _priced_call(prompt: int = 1, completion: int = 1) -> LLMCallEvent:
    """One synthesized `llm_call`, for the accounting cases a real sweep cannot stage cheaply."""
    return LLMCallEvent(
        ts=datetime(2026, 8, 30, tzinfo=UTC),
        trace_id="t",
        session_id="s",
        turn_index=0,
        caller="agent:consultation",
        provider="mock",
        model="mock-model",
        prompt_tokens=prompt,
        completion_tokens=completion,
        latency_ms=1.0,
        tools_offered=[],
        stop_reason="stop",
    )


def _settings() -> Settings:
    return Settings(root_dir=ROOT, data_dir=ROOT / "data", corpus_dir=ROOT / "data" / "corpus")


def _item(index: int) -> GoldenItem:
    return GoldenItem(
        id=f"g-gh-{index:03d}",
        question="what is hypertension",
        category="general_health",
        expected_route=ExpectedRoute(mode="single", agents=("consultation",)),
        relevant_doc_ids=("condition-hypertension",),
        reference_answer="Persistently raised blood pressure.",
        red_flag=False,
        labeled=True,
    )


def _runtime(*, calls: int, usage: Usage | None = ONE_DOLLAR, model: str = "mock-model") -> Runtime:
    """A `baseline_llm` runtime: no router and no tools, so one item is exactly one LLM call."""
    return build_runtime(
        _settings(),
        provider=MockProvider(
            [ScriptedResponse(content="An answer.", usage=usage) for _ in range(calls)],
            model=model,
        ),
        config=get_preset("baseline_llm"),
        embedder="hash",
        store="numpy",
    )


# --- pricing the trace ----------------------------------------------------------------------------


async def test_llm_call_cost_prices_what_it_can_and_names_what_it_cannot(tmp_path: Path) -> None:
    """The float and the unpriced list come back together, so nobody can take the undercount alone.

    An unpriced model contributes nothing to the total. A caller handed only the number would get a
    figure that reads as complete and is not -- the same error `unpriced_models` exists to prevent
    in the results table, met one layer lower.
    """
    from eval.harness import run_golden_item

    runtime = _runtime(calls=1)
    run = await run_golden_item(runtime, _item(1), runs_dir=tmp_path, prefix="baseline_llm")

    priced, missing = llm_call_cost(run.events, pricing=FAKE_PRICING)
    unpriced, named = llm_call_cost(run.events, pricing={})

    assert priced == pytest.approx(1.0)
    assert missing == ()
    assert unpriced == 0.0
    assert named == ("mock/mock-model",)


def test_one_lookup_answers_the_priced_question_for_both_callers() -> None:
    """The refusal and the results table must not disagree about which key form counts as priced."""
    assert rate_for(FAKE_PRICING, "mock", "mock-model") == {"input": 1.0, "output": 1.0}
    assert rate_for({"mock-model": {"input": 1.0, "output": 1.0}}, "mock", "mock-model") is not None
    assert rate_for(FAKE_PRICING, "openai", "gpt-4o-mini") is None
    assert rate_for({}, "mock", "mock-model") is None
    # A half-filled entry is not a rate: a cost from it would be an undercount, not a cost.
    assert rate_for({"mock/mock-model": {"input": 1.0}}, "mock", "mock-model") is None


# --- the cap fires --------------------------------------------------------------------------------


async def test_the_cap_stops_the_sweep_and_records_where(tmp_path: Path) -> None:
    """Four items at a dollar each under a $2.50 cap: three run, the fourth never starts."""
    runtime = _runtime(calls=6)
    meter = CostMeter(pricing=FAKE_PRICING, cap_usd=2.50)

    runs = await sweep(
        runtime,
        [_item(index) for index in range(1, 5)],
        prefix="baseline_llm",
        runs_dir=tmp_path,
        meter=meter,
    )

    assert len(runs) == 3
    assert meter.aborted
    assert meter.spent_usd == pytest.approx(3.0)
    assert meter.items_completed == 3
    assert meter.aborted_at_config == "baseline_llm"
    assert meter.aborted_after_item == "g-gh-003"
    assert "passed the --max-cost $2.50 cap" in meter.reason()


async def test_the_cap_stops_new_work_rather_than_killing_a_turn(tmp_path: Path) -> None:
    """The overshoot is bounded by one item, and the item it overshot on is a complete item.

    Charged after an item finishes, because that is when its events exist. Killing a turn part-way
    would leave a trace file whose `turn` event never arrived, and every metric in `eval/metrics.py`
    counts turns by that event -- so the abort would silently drop the item it aborted on out of
    every denominator.
    """
    runtime = _runtime(calls=6)
    meter = CostMeter(pricing=FAKE_PRICING, cap_usd=0.5)

    runs = await sweep(
        runtime,
        [_item(index) for index in range(1, 5)],
        prefix="baseline_llm",
        runs_dir=tmp_path,
        meter=meter,
    )

    assert len(runs) == 1
    assert runs[0].ok
    assert turn_event(runs[0].events) is not None
    assert meter.spent_usd == pytest.approx(1.0)  # one item's overshoot, and no more


async def test_a_sweep_that_stays_under_the_cap_runs_every_item(tmp_path: Path) -> None:
    runtime = _runtime(calls=6)
    meter = CostMeter(pricing=FAKE_PRICING, cap_usd=100.0)

    runs = await sweep(
        runtime,
        [_item(index) for index in range(1, 5)],
        prefix="baseline_llm",
        runs_dir=tmp_path,
        meter=meter,
    )

    assert len(runs) == 4
    assert not meter.aborted
    assert meter.spent_usd == pytest.approx(4.0)
    assert "finished at $4.0000" in meter.to_cap(items_planned=4).sentence()


async def test_a_model_with_no_rate_mid_run_aborts_rather_than_counting_as_free(
    tmp_path: Path,
) -> None:
    """Otherwise the running total stops tracking the spend while the cap goes on looking enforced.

    Same condition `refuse_unpriceable_cap` refuses up front, met later, so it gets the same answer.
    """
    runtime = _runtime(calls=6, model="unbilled-model")
    meter = CostMeter(pricing=FAKE_PRICING, cap_usd=100.0)

    runs = await sweep(
        runtime,
        [_item(index) for index in range(1, 5)],
        prefix="baseline_llm",
        runs_dir=tmp_path,
        meter=meter,
    )

    assert len(runs) == 1
    assert meter.aborted
    assert meter.spent_usd == 0.0
    assert meter.unpriced_models == ("mock/unbilled-model",)
    assert "could no longer be enforced" in meter.reason()


# --- the cap refuses to pretend -------------------------------------------------------------------


def test_a_cap_on_an_unpriceable_model_is_refused() -> None:
    """An unpriceable run cannot be capped, and the error says exactly that."""
    refuse_unpriceable_cap(FAKE_PRICING, provider="mock", model="mock-model")

    with pytest.raises(UnpriceableCapError, match="An unpriceable run cannot be capped"):
        refuse_unpriceable_cap({}, provider="mock", model="mock-model")

    with pytest.raises(UnpriceableCapError, match="CONSILIUM_MODEL is unset"):
        refuse_unpriceable_cap(FAKE_PRICING, provider="mock", model=None)


async def test_the_runner_refuses_the_cap_before_it_spends_anything(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`eval/pricing.yaml` ships empty on purpose, so the shipped state of the repo refuses a cap.

    Refused before `build_runtime`, so no item is paid for to discover that the guard the operator
    thought they had could never have fired.
    """
    from eval.run import main

    monkeypatch.setenv("CONSILIUM_PROVIDER", "openai")
    monkeypatch.setenv("CONSILIUM_MODEL", "gpt-4o-mini")
    monkeypatch.setenv("OPENAI_API_KEY", "not-a-real-key")

    code = await main(["--config", "full", "--max-cost", "1.0"])

    assert code == 2
    err = capsys.readouterr().err
    assert "openai/gpt-4o-mini has no entry in pricing.yaml" in err
    assert "An unpriceable run cannot be capped" in err


async def test_a_cap_of_zero_or_less_is_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """It would abort after the first item every time, which is not a spend guard."""
    from eval.run import main

    monkeypatch.setenv("CONSILIUM_PROVIDER", "mock")
    code = await main(["--config", "full", "--max-cost", "0"])

    assert code == 2
    assert "positive number of US dollars" in capsys.readouterr().err


# --- no cap means nothing changed -----------------------------------------------------------------


async def test_an_uncapped_sweep_is_the_sweep_it_always_was(tmp_path: Path) -> None:
    """No meter, no early stop, no accounting: the default path is untouched."""
    runtime = _runtime(calls=6)

    runs = await sweep(
        runtime,
        [_item(index) for index in range(1, 5)],
        prefix="baseline_llm",
        runs_dir=tmp_path,
        meter=None,
    )

    assert len(runs) == 4
    assert all(run.ok for run in runs)


def test_the_flag_defaults_to_no_cap_and_the_summary_says_so() -> None:
    assert build_parser().parse_args([]).max_cost is None
    assert _summary(cost_cap=None).cost_cap is None
    assert "Spend cap" not in render_markdown(_summary(cost_cap=None))
    assert '"cost_cap": null' in to_json(_summary(cost_cap=None))


# --- what the abort leaves on the record ------------------------------------------------------


def _summary(*, cost_cap: CostCap | None) -> RunSummary:
    return RunSummary(
        commit="abc123",
        started_at="2026-08-30T00:00:00+00:00",
        finished_at="2026-08-30T00:05:00+00:00",
        provider="openai",
        model="gpt-4o-mini",
        judge_model="gpt-4o-mini",
        golden_path="eval/data/golden.jsonl",
        multiturn_path="eval/data/multiturn.jsonl",
        n_items=150,
        limit=None,
        python="3.12.0",
        platform="Linux 6.6",
        pricing_source="pricing.yaml (1 model(s) priced)",
        unverified_labels=UnverifiedLabels(),
        cost_cap=cost_cap,
    )


def test_the_abort_marker_and_the_spend_are_in_the_summary_and_above_the_table() -> None:
    """Above the table, because a partial ablation row looks exactly like a complete one.

    A reader who meets the table first has already read the numbers as a result; the marker has to
    reach them before the numbers do, which is the same rule the unverified-reference caveat
    follows.
    """
    cap = CostCap(
        cap_usd=25.0,
        spent_usd=25.4131,
        aborted=True,
        aborted_at_config="full",
        aborted_after_item="g-cc-018",
        items_completed=108,
        items_planned=600,
        unpriced_models=(),
    )

    rendered = render_markdown(_summary(cost_cap=cap))
    marker = rendered.index("ABORTED ON THE SPEND CAP")

    assert marker < rendered.index("## Ablation")
    assert "g-cc-018" in rendered
    assert "108 of 600 planned item(s)" in rendered
    assert "$25.4131" in rendered
    assert "its numbers are not a sweep result" in rendered
    assert "the judge's own calls are not traced and are not counted" in rendered
    assert '"spent_usd": 25.4131' in to_json(_summary(cost_cap=cap))


def test_an_unpriceable_abort_says_that_rather_than_naming_the_cap() -> None:
    """Two different failures, and reading the wrong one sends the operator to the wrong fix."""
    cap = CostCap(
        cap_usd=25.0,
        spent_usd=3.0,
        aborted=True,
        aborted_at_config="full",
        aborted_after_item="g-gh-004",
        items_completed=4,
        items_planned=600,
        unpriced_models=("openai/gpt-5-preview",),
    )

    sentence = cap.sentence()

    assert "openai/gpt-5-preview" in sentence
    assert "could no longer be enforced" in sentence
    assert "passed the" not in sentence


def test_the_capped_exit_status_is_not_the_argument_error_status() -> None:
    """A CI job that sets a cap has to tell "the cap fired" from "the command line was wrong"."""
    assert EXIT_COST_CAP != 0
    assert EXIT_COST_CAP != 2


def test_the_abort_marker_names_the_item_that_passed_the_cap_not_the_last_one_charged() -> None:
    """A later charge must not move the marker onto an item that ran before the cap was reached."""
    meter = CostMeter(pricing=FAKE_PRICING, cap_usd=0.0000005)
    call: list[TraceEvent] = [_priced_call()]

    meter.charge(call, config="full", item_id="g-gh-001")
    meter.charge(call, config="full", item_id="g-gh-002")

    assert meter.aborted_after_item == "g-gh-001"
    assert meter.items_completed == 2


def test_the_meter_charges_an_item_with_no_llm_calls_without_moving_the_total() -> None:
    """An item that failed before its first call is counted as run and costs nothing."""
    meter = CostMeter(pricing=FAKE_PRICING, cap_usd=1.0)
    empty: list[TraceEvent] = []

    meter.charge(empty, config="full", item_id="g-gh-001")

    assert meter.items_completed == 1
    assert meter.spent_usd == 0.0
    assert not meter.aborted
