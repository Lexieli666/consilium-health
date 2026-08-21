"""Every metric in the brief's section 5.2, over synthesized trace events.

Synthesized rather than recorded, because the point of each test is a definition: what recall@5
means when a turn retrieves three times, what routing accuracy does with a fallback, what red-flag
recall reports when the guard rather than the model produced the escalation. A recorded trace would
make those cases hard to construct and impossible to read.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from consilium.trace import (
    FusedHit,
    LLMCallEvent,
    PlannedSubtask,
    RetrievalEvent,
    RouteEvent,
    RouteMode,
    SafetyEvent,
    SafetyKind,
    ToolCallEvent,
    TraceEvent,
    TurnEvent,
)
from eval.items import ExpectedRoute, GoldenItem, PhrasingStratum
from eval.metrics import (
    StratumRecall,
    hit_at_k,
    latency_report,
    percentile,
    recall_at_k,
    reciprocal_rank,
    retrieval_outcome,
    retrieval_report,
    route_event,
    routing_outcome,
    routing_report,
    safety_report,
    turn_event,
    usage_report,
)

TS = datetime(2026, 1, 1, tzinfo=UTC)
COMMON = {"ts": TS, "trace_id": "t", "session_id": "s", "turn_index": 0}


def _item(
    item_id: str = "g-001",
    *,
    mode: RouteMode = "single",
    agents: tuple[str, ...] = ("consultation",),
    relevant: tuple[str, ...] = ("doc-a",),
    red_flag: bool = False,
    stratum: PhrasingStratum | None = None,
) -> GoldenItem:
    return GoldenItem(
        id=item_id,
        question="q",
        category="general_health",
        expected_route=ExpectedRoute(mode=mode, agents=agents),
        relevant_doc_ids=relevant,
        reference_answer="a",
        red_flag=red_flag,
        labeled=True,
        phrasing_stratum=stratum,
    )


def _route(
    mode: RouteMode = "single",
    agents: tuple[str, ...] = ("consultation",),
    fallback: bool = False,
) -> RouteEvent:
    return RouteEvent(
        **COMMON,
        mode=mode,
        agents=list(agents),
        subtasks=[
            PlannedSubtask(subtask_id=f"1-{agents[0]}", agent=agents[0], objective="o", why="w")
        ],
        fallback=fallback,
        latency_ms=12.0,
    )


def _retrieval(
    doc_ids: list[str], *, returned_k: int = 5, skill: str = "search_knowledge"
) -> RetrievalEvent:
    return RetrievalEvent(
        **COMMON,
        skill=skill,
        query="q",
        category_filter=None,
        fused_topk=[
            FusedHit(doc_id=doc_id, chunk_index=0, rrf_score=1.0 / (index + 1))
            for index, doc_id in enumerate(doc_ids)
        ],
        returned_k=returned_k,
        latency_ms=3.0,
    )


def _turn(
    *,
    wall_ms: float = 100.0,
    escalated_post: bool = False,
    escalated_pre: bool = False,
    repaired: bool = False,
    suppressed: bool = False,
) -> TurnEvent:
    return TurnEvent(
        **COMMON,
        question="q",
        answer="a",
        risk_level="routine",
        wall_ms=wall_ms,
        red_flag_matched=False,
        red_flag_matched_raw=suppressed,
        red_flag_negation_suppressed=suppressed,
        escalation_present_pre_repair=escalated_pre,
        escalation_present_post_repair=escalated_post,
        repair_applied=repaired,
    )


def _llm(
    caller: str = "agent:consultation", prompt: int = 100, completion: int = 20
) -> LLMCallEvent:
    return LLMCallEvent(
        **COMMON,
        caller=caller,
        provider="openai",
        model="gpt-4o-mini",
        prompt_tokens=prompt,
        completion_tokens=completion,
        latency_ms=50.0,
        tools_offered=[],
        stop_reason="stop",
    )


def _tool(skill: str = "search_knowledge", sources: tuple[str, ...] = ("doc-a",)) -> ToolCallEvent:
    return ToolCallEvent(
        **COMMON,
        agent="consultation",
        skill=skill,
        args={},
        ok=True,
        error=None,
        latency_ms=2.0,
        source_doc_ids=list(sources),
    )


def _safety(event: SafetyKind, rule: str, *, post_stream: bool = False) -> SafetyEvent:
    return SafetyEvent(
        **COMMON,
        event=event,
        rule=rule,
        scope="output",
        agent="consultation",
        detail="d",
        post_stream=post_stream,
    )


# --- event selection -----------------------------------------------------------------------------


def test_a_missing_route_event_is_none_rather_than_an_error() -> None:
    """`single_agent_rag` and `baseline_llm` make no routing decision; that is data, not a gap."""
    assert route_event([_turn()]) is None
    assert turn_event([]) is None


# --- routing -------------------------------------------------------------------------------------


def test_an_exactly_matching_route_is_correct() -> None:
    outcome = routing_outcome(_item(), [_route()])
    assert outcome is not None
    assert outcome.correct is True


def test_agent_order_does_not_change_the_verdict() -> None:
    """The planner's ordering is not part of the decision the router acts on."""
    item = _item(mode="parallel", agents=("research", "diagnostic"))
    outcome = routing_outcome(item, [_route("parallel", ("diagnostic", "research"))])
    assert outcome is not None and outcome.correct is True


def test_a_wrong_agent_set_is_incorrect_even_with_the_right_mode() -> None:
    outcome = routing_outcome(_item(), [_route("single", ("diagnostic",))])
    assert outcome is not None
    assert outcome.correct is False
    assert outcome.mode_correct is True


def test_routing_is_not_scored_for_an_item_with_no_label() -> None:
    item = GoldenItem(id="x", question="q", category="general_health")
    assert routing_outcome(item, [_route()]) is None


def test_accuracy_is_reported_both_with_and_without_fallback_turns() -> None:
    """Reporting only the fallback-excluded number lets a broken planner look perfect."""
    outcomes = [
        routing_outcome(_item("a"), [_route()]),
        routing_outcome(_item("b", agents=("diagnostic",)), [_route(fallback=True)]),
    ]
    report = routing_report([outcome for outcome in outcomes if outcome])

    assert report.n == 2
    assert report.accuracy == 0.5  # the fallback answered the wrong agent, and that counts
    assert report.accuracy_excluding_fallback == 1.0
    assert report.n_excluding_fallback == 1
    assert report.fallback_rate == 0.5


def test_the_confusion_matrix_is_over_modes_and_the_breakdown_is_per_agent_set() -> None:
    outcomes = [
        routing_outcome(_item("a", mode="parallel", agents=("diagnostic", "research")), [_route()]),
        routing_outcome(_item("b"), [_route()]),
    ]
    report = routing_report([outcome for outcome in outcomes if outcome])

    assert report.confusion[("parallel", "single")] == 1
    assert report.confusion[("single", "single")] == 1
    assert report.per_agent_set["consultation"] == (1, 1)
    assert report.per_agent_set["diagnostic+research"] == (0, 1)


def test_routing_is_not_measured_when_nothing_routed() -> None:
    outcome = routing_outcome(_item(), [_turn()])
    assert outcome is not None and outcome.routed is False
    report = routing_report([outcome])
    assert report.accuracy is None
    assert report.fallback_rate is None


# --- retrieval -----------------------------------------------------------------------------------


def test_recall_is_genuine_recall_not_hit_rate() -> None:
    assert recall_at_k(["doc-a", "doc-x"], ["doc-a", "doc-b"]) == 0.5
    assert hit_at_k(["doc-a", "doc-x"], ["doc-a", "doc-b"]) is True
    assert recall_at_k([], ["doc-a"]) == 0.0
    assert recall_at_k(["doc-a"], []) == 0.0


def test_reciprocal_rank_is_the_first_relevant_position() -> None:
    assert reciprocal_rank(["x", "y", "doc-a"], ["doc-a"]) == pytest.approx(1 / 3)
    assert reciprocal_rank(["x"], ["doc-a"]) == 0.0


def test_recall_is_reported_three_ways_over_a_multi_retrieval_turn() -> None:
    """A union that beats the first event because the turn retrieved twice is a different result."""
    item = _item(relevant=("doc-a", "doc-b"))
    events: list[TraceEvent] = [
        _retrieval(["doc-a", "x", "y", "z", "w", "v6", "v7", "v8", "v9", "v10"]),
        _retrieval(["doc-b", "p", "q", "r", "s"]),
        _turn(),
    ]

    outcome = retrieval_outcome(item, events)
    assert outcome is not None
    assert outcome.retrieval_events == 2
    report = retrieval_report([outcome])

    assert report.recall_at_5_union == 1.0
    assert report.recall_at_5_first_event == 0.5
    assert report.hit_at_5 == 1.0
    assert report.docs_retrieved_per_turn == 15.0


def test_mrr_is_computed_over_the_fused_top_ten_not_the_returned_five() -> None:
    """The trace carries ranks 6-10 the model never saw, precisely so this is computable."""
    item = _item(relevant=("doc-z",))
    ranked = ["a", "b", "c", "d", "e", "f", "g", "doc-z", "i", "j"]

    outcome = retrieval_outcome(item, [_retrieval(ranked), _turn()])
    assert outcome is not None
    report = retrieval_report([outcome])

    assert report.recall_at_5_union == 0.0  # not in the top five
    assert report.mrr_at_10 == pytest.approx(1 / 8)


def test_a_turn_with_no_retrieval_scores_zero_recall_rather_than_being_skipped() -> None:
    """`baseline_llm` retrieves nothing; that is a zero, not a missing measurement."""
    outcome = retrieval_outcome(_item(), [_turn()])
    assert outcome is not None
    assert retrieval_report([outcome]).recall_at_5_union == 0.0


def test_retrieval_is_not_scored_for_an_item_with_no_relevance_label() -> None:
    item = GoldenItem(id="x", question="q", category="general_health")
    assert retrieval_outcome(item, [_retrieval(["doc-a"])]) is None


def test_retrieval_is_not_measured_when_there_are_no_outcomes() -> None:
    report = retrieval_report([])
    assert report.recall_at_5_union is None
    assert report.mrr_at_10 is None


# --- safety --------------------------------------------------------------------------------------


def test_red_flag_recall_is_measured_on_the_delivered_answer() -> None:
    """The guard, not the model, produced this escalation -- and it still counts as recall."""
    scored = [(_item(red_flag=True), [_turn(escalated_post=True, repaired=True)])]

    report = safety_report(scored)

    assert report.red_flag_recall == 1.0
    assert report.model_escalated_unaided == 0.0
    assert report.red_flag_false_negatives == 0


def test_the_false_negative_count_is_raw_and_the_items_are_named() -> None:
    """A rate of 0.93 on 30 items hides two people."""
    scored = [
        (_item("a", red_flag=True), [_turn(escalated_post=True)]),
        (_item("b", red_flag=True), [_turn(escalated_post=False)]),
    ]

    report = safety_report(scored)

    assert report.red_flag_recall == 0.5
    assert report.red_flag_false_negatives == 1
    assert report.false_negative_item_ids == ("b",)


def test_a_turn_with_no_turn_event_is_a_red_flag_false_negative() -> None:
    """A turn that died produced no escalation, and silence is not recall."""
    report = safety_report([(_item(red_flag=True), [])])
    assert report.red_flag_false_negatives == 1


def test_violations_and_repairs_are_two_rates_never_summed() -> None:
    scored: list[tuple[GoldenItem, list[TraceEvent]]] = [
        (
            _item(),
            [
                _safety("violation", "disclaimer"),
                _safety("violation", "false_reassurance"),
                _safety("repair", "disclaimer", post_stream=True),
                _turn(),
            ],
        )
    ]

    report = safety_report(scored)

    assert report.violations_per_100_turns == 200.0
    assert report.repairs_per_100_turns == 100.0
    assert report.violations_by_rule == {"disclaimer": 1, "false_reassurance": 1}
    assert report.repairs_by_rule == {"disclaimer": 1}
    assert report.post_stream_repairs == 1


def test_the_negation_guard_s_discordant_turns_are_counted() -> None:
    """The set that lets both negation policies be compared from one run."""
    report = safety_report([(_item(), [_turn(suppressed=True)]), (_item("b"), [_turn()])])
    assert report.negation_suppressed_turns == 1


def test_red_flag_recall_is_split_by_phrasing_stratum_and_never_only_pooled() -> None:
    """The pooled figure moves with the drafting ratio; the split moves with the system."""
    scored = [
        (_item("a", red_flag=True, stratum="hard"), [_turn(escalated_post=False)]),
        (_item("b", red_flag=True, stratum="hard"), [_turn(escalated_post=True)]),
        (_item("c", red_flag=True, stratum="easy"), [_turn(escalated_post=True)]),
    ]

    report = safety_report(scored)

    assert report.red_flag_recall == pytest.approx(2 / 3)
    assert report.by_stratum["hard"].recall == 0.5
    assert report.by_stratum["hard"].false_negative_item_ids == ("a",)
    assert report.by_stratum["easy"] == StratumRecall(
        n=1, escalated=1, recall=1.0, false_negative_item_ids=()
    )


def test_an_item_with_no_stratum_is_in_the_pooled_figure_and_in_no_stratum() -> None:
    """Only red-flag *candidates* carry a stratum, and a stratum is never inferred."""
    report = safety_report([(_item(red_flag=True), [_turn(escalated_post=True)])])

    assert report.red_flag_recall == 1.0
    assert report.by_stratum == {}


def test_red_flag_recall_is_not_measured_when_the_run_has_no_red_flag_items() -> None:
    """Zero would read as a system that failed on every emergency."""
    report = safety_report([(_item(red_flag=False), [_turn()])])
    assert report.red_flag_recall is None
    assert report.n_red_flag_items == 0


# --- latency -------------------------------------------------------------------------------------


def test_percentiles_are_nearest_rank_and_report_a_value_a_turn_actually_had() -> None:
    values = [10.0, 20.0, 30.0, 40.0, 100.0]
    assert percentile(values, 50) == 30.0
    assert percentile(values, 90) == 100.0
    assert percentile([], 50) is None


def test_latency_splits_by_route_mode_only_where_a_route_event_exists() -> None:
    scored: list[tuple[GoldenItem, list[TraceEvent]]] = [
        (_item("a"), [_route("single"), _turn(wall_ms=100.0)]),
        (_item("b"), [_route("parallel"), _turn(wall_ms=300.0)]),
        (_item("c"), [_turn(wall_ms=50.0)]),  # no routing decision
    ]

    report = latency_report(scored)

    assert report.n == 3
    assert report.p50_ms == 100.0
    assert set(report.by_mode) == {"single", "parallel"}
    assert report.by_mode["parallel"][0] == 1


def test_latency_by_mode_is_empty_for_a_configuration_that_does_not_route() -> None:
    report = latency_report([(_item(), [_turn()])])
    assert report.by_mode == {}


# --- usage ---------------------------------------------------------------------------------------


def test_tokens_are_summed_over_every_caller_including_the_planner() -> None:
    """The planner and synthesizer are the overhead the architecture adds, so they are counted."""
    scored: list[tuple[GoldenItem, list[TraceEvent]]] = [
        (
            _item(),
            [
                _llm("planner", 200, 30),
                _llm("agent:diagnostic", 400, 60),
                _llm("synthesizer", 300, 50),
                _route("parallel"),
                _turn(),
            ],
        )
    ]

    report = usage_report(scored)

    assert report.tokens_per_turn == 1040.0
    assert report.llm_calls_per_turn == 3.0
    assert report.tokens_by_caller == {
        "planner": 230,
        "agent:diagnostic": 460,
        "synthesizer": 350,
    }
    assert report.tokens_by_mode == {"parallel": 1040.0}


def test_the_tool_call_distribution_is_reported_not_only_its_mean() -> None:
    """A cap cannot be justified from a distribution truncated at the cap."""
    scored: list[tuple[GoldenItem, list[TraceEvent]]] = [
        (_item("a"), [_tool(), _tool(), _turn()]),
        (_item("b"), [_tool(), _turn()]),
        (_item("c"), [_turn()]),
    ]

    report = usage_report(scored)

    assert report.tool_calls_per_turn == pytest.approx(1.0)
    assert report.tool_call_distribution == {0: 1, 1: 1, 2: 1}
    assert report.tool_calls_by_skill == {"search_knowledge": 3}


def test_cost_is_not_measured_when_a_model_has_no_published_rate() -> None:
    """A partial cost reads as a complete one, which is worse than none."""
    scored = [(_item(), [_llm(), _turn()])]

    report = usage_report(scored, pricing={})

    assert report.cost_per_turn_usd is None
    assert report.unpriced_models == ("openai/gpt-4o-mini",)


def test_cost_is_computed_when_every_model_used_is_priced() -> None:
    scored = [(_item(), [_llm(prompt=1_000_000, completion=1_000_000), _turn()])]

    report = usage_report(scored, pricing={"openai/gpt-4o-mini": {"input": 1.0, "output": 4.0}})

    assert report.cost_per_turn_usd == pytest.approx(5.0)
    assert report.unpriced_models == ()


def test_a_turn_with_no_turn_event_is_excluded_from_usage_denominators() -> None:
    report = usage_report([(_item(), [_llm()])])
    assert report.n_turns == 0
    assert report.tokens_per_turn is None
