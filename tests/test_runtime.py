"""The composition root and the turn boundary.

One user turn is one tracer, one trace file, and one `turn` event written last. These tests are
about that invariant and about the honesty of the escalation fields in this phase: there is no
`OutputRepair` yet, so the delivered answer is the model's own and pre == post.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from consilium.config import RunConfig, Settings
from consilium.llm import MockProvider, ScriptedResponse
from consilium.llm.mock import ScriptedToolCall
from consilium.runtime import Runtime, build_runtime, run_turn
from consilium.trace import MemorySink, RetrievalEvent, Tracer, TurnEvent, read_trace

ROOT = Path(__file__).resolve().parents[1]


def _settings() -> Settings:
    return Settings(root_dir=ROOT, data_dir=ROOT / "data", corpus_dir=ROOT / "data" / "corpus")


def _plan(*agents: str) -> ScriptedResponse:
    """A planner reply assigning one subtask per named agent.

    Scripted rather than pinned, because the router now makes a planner call before any specialist
    does: a test that forgets it would silently hand the planner's slot to the agent's script.
    """
    return ScriptedResponse(
        content=json.dumps(
            {
                "subtasks": [
                    {"agent": name, "objective": f"Handle the {name} part.", "why": "test"}
                    for name in agents
                ]
            }
        )
    )


def _runtime(responses: list[ScriptedResponse], *, config: RunConfig | None = None) -> Runtime:
    """A runtime on the offline seams: hash embeddings into an in-memory store, a scripted model."""
    return build_runtime(
        _settings(),
        config=config,
        provider=MockProvider(responses),
        embedder="hash",
        store="numpy",
    )


def test_build_runtime_wires_every_layer_from_settings() -> None:
    runtime = _runtime([])

    assert len(runtime.registry) == 7
    assert len(runtime.policy) == 3
    assert len(runtime.red_flags) >= 10
    assert runtime.retriever is not None
    assert len(runtime.documents) == 78


def test_retrieval_off_leaves_the_retriever_absent_rather_than_faked() -> None:
    """`baseline_llm` is defined by the absence of retrieval, so it has to be expressible."""
    runtime = _runtime([], config=RunConfig(name="baseline_llm", retrieval=False, router="none"))

    assert runtime.retriever is None
    context = runtime.context(agent="consultation")
    assert context.retriever is None
    assert context.red_flags is not None  # safety substrate is never ablated away


def test_the_run_config_budgets_reach_the_loop() -> None:
    runtime = _runtime([], config=RunConfig(name="full_budget_6", max_tool_calls=6))
    agent = runtime.agent("consultation")

    assert agent.loop.max_tool_calls == 6


def test_an_unknown_agent_name_names_the_ones_that_exist() -> None:
    runtime = _runtime([])
    with pytest.raises(KeyError, match="known agents"):
        runtime.agent("triage")


async def test_a_turn_writes_exactly_one_turn_event_last(memory_sink: MemorySink) -> None:
    runtime = _runtime(
        [
            _plan("consultation"),
            ScriptedResponse(
                tool_calls=[ScriptedToolCall(name="search_knowledge", arguments={"query": "htn"})]
            ),
            ScriptedResponse(content="Hypertension is persistently raised blood pressure."),
        ]
    )
    tracer = Tracer(session_id="s", turn_index=0, sink=memory_sink)

    outcome = await run_turn(runtime, "what is hypertension", tracer=tracer)

    assert outcome.answer.startswith("Hypertension")
    assert outcome.mode == "single"
    assert outcome.agents == ("consultation",)
    assert outcome.fallback is False
    assert outcome.sources
    (turn,) = memory_sink.of_type(TurnEvent)
    assert memory_sink.events[-1] is turn
    assert turn.question == "what is hypertension"
    assert memory_sink.of_type(RetrievalEvent)


async def test_a_red_flag_question_records_both_negation_policies(memory_sink: MemorySink) -> None:
    runtime = _runtime([ScriptedResponse(content="Call emergency services now.")])
    tracer = Tracer(session_id="s", turn_index=0, sink=memory_sink)

    outcome = await run_turn(
        runtime, "I have crushing chest pain", tracer=tracer, agent="diagnostic"
    )

    assert outcome.risk_level == "emergency"
    (turn,) = memory_sink.of_type(TurnEvent)
    assert turn.red_flag_matched is True
    assert turn.red_flag_matched_raw is True
    assert turn.red_flag_negation_suppressed is False
    assert turn.escalation_present_post_repair is True


async def test_a_negated_red_flag_is_recorded_as_suppressed(memory_sink: MemorySink) -> None:
    runtime = _runtime(
        [_plan("consultation"), ScriptedResponse(content="Tiredness has many causes.")]
    )
    tracer = Tracer(session_id="s", turn_index=0, sink=memory_sink)

    outcome = await run_turn(runtime, "I have no chest pain, just tired", tracer=tracer)

    assert outcome.risk_level == "routine"
    (turn,) = memory_sink.of_type(TurnEvent)
    assert turn.red_flag_matched_raw is True
    assert turn.red_flag_matched is False
    assert turn.red_flag_negation_suppressed is True


async def test_pre_and_post_repair_agree_while_there_is_no_repair(memory_sink: MemorySink) -> None:
    """Phase 4 delivers the model's own answer, so the two fields describe the same string."""
    runtime = _runtime([_plan("diagnostic"), ScriptedResponse(content="No escalation here.")])
    tracer = Tracer(session_id="s", turn_index=0, sink=memory_sink)

    await run_turn(runtime, "I have crushing chest pain", tracer=tracer)

    (turn,) = memory_sink.of_type(TurnEvent)
    assert turn.escalation_present_pre_repair is False
    assert turn.escalation_present_post_repair is False
    assert turn.repair_applied is False


async def test_the_trace_file_round_trips_through_its_own_schema(tmp_path: Path) -> None:
    """Every reported number is read back out of this file, so it has to validate."""
    runtime = _runtime([_plan("consultation"), ScriptedResponse(content="An answer.")])

    with Tracer.for_turn(session_id="round-trip", turn_index=2, runs_dir=tmp_path) as tracer:
        await run_turn(runtime, "a question", tracer=tracer)

    events = read_trace(tmp_path / "round-trip" / "2.jsonl")
    assert [event.type for event in events] == [
        "llm_call",  # the planner
        "route",
        "blackboard",  # assigned
        "blackboard",  # started
        "llm_call",  # the specialist
        "blackboard",  # completed
        "turn",
    ]
    assert all(event.session_id == "round-trip" for event in events)
    assert all(event.turn_index == 2 for event in events)
