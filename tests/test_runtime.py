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
from consilium.trace import (
    MemorySink,
    RetrievalEvent,
    SafetyEvent,
    Tracer,
    TurnEvent,
    read_trace,
)

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


async def test_a_red_flag_answer_that_does_not_escalate_is_repaired(
    memory_sink: MemorySink,
) -> None:
    """Pre-repair records the model's failure; post-repair is red-flag recall."""
    runtime = _runtime([_plan("diagnostic"), ScriptedResponse(content="No escalation here.")])
    tracer = Tracer(session_id="s", turn_index=0, sink=memory_sink)

    outcome = await run_turn(runtime, "I have crushing chest pain", tracer=tracer)

    (turn,) = memory_sink.of_type(TurnEvent)
    assert turn.escalation_present_pre_repair is False
    assert turn.escalation_present_post_repair is True
    assert turn.repair_applied is True
    assert outcome.answer.startswith("**Seek emergency care now.**")


async def test_an_answer_that_already_escalates_needs_no_escalation_repair(
    memory_sink: MemorySink,
) -> None:
    """A correctly handled red flag emits no escalation repair; measuring repairs would miss it."""
    runtime = _runtime(
        [
            _plan("diagnostic"),
            ScriptedResponse(content="Call emergency services now. This can be an emergency."),
        ]
    )
    tracer = Tracer(session_id="s", turn_index=0, sink=memory_sink)

    await run_turn(runtime, "I have crushing chest pain", tracer=tracer)

    (turn,) = memory_sink.of_type(TurnEvent)
    assert turn.escalation_present_pre_repair is True
    assert turn.escalation_present_post_repair is True
    repairs = [event.rule for event in memory_sink.of_type(SafetyEvent) if event.event == "repair"]
    assert "escalation_required" not in repairs
    assert repairs == ["disclaimer"]  # only the boilerplate was missing


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
        "safety",  # violation: the answer carried no disclaimer
        "safety",  # repair: the disclaimer was appended
        "turn",
    ]
    assert all(event.session_id == "round-trip" for event in events)
    assert all(event.turn_index == 2 for event in events)


# --- memory (Phase 6) ----------------------------------------------------------------------------


async def test_a_second_turn_sees_the_first_one_s_history(memory_sink: MemorySink) -> None:
    """The multi-turn case the golden set exercises: "what about diet?" after a condition."""
    runtime = _runtime(
        [
            _plan("consultation"),
            ScriptedResponse(content="Hypertension is persistently raised blood pressure."),
            _plan("consultation"),
            ScriptedResponse(content="For that condition, sodium reduction is described."),
        ]
    )

    for turn_index, question in enumerate(["what is hypertension", "what about diet?"]):
        tracer = Tracer(session_id="multi", turn_index=turn_index, sink=memory_sink)
        await run_turn(runtime, question, tracer=tracer)

    working = runtime.memory.get("multi")
    assert len(working) == 2
    assert working.exchanges[0].question == "what is hypertension"
    assert working.exchanges[1].answer.startswith("For that condition")


async def test_two_sessions_in_one_runtime_do_not_share_a_history(
    memory_sink: MemorySink,
) -> None:
    runtime = _runtime(
        [
            _plan("consultation"),
            ScriptedResponse(content="Answer for alice."),
            _plan("consultation"),
            ScriptedResponse(content="Answer for bob."),
        ]
    )

    for session in ("alice", "bob"):
        tracer = Tracer(session_id=session, turn_index=0, sink=memory_sink)
        await run_turn(runtime, f"question from {session}", tracer=tracer)

    assert runtime.memory.get("alice").exchanges[0].question == "question from alice"
    assert runtime.memory.get("bob").exchanges[0].question == "question from bob"
    assert len(runtime.memory.get("alice")) == 1
    assert runtime.memory.get("alice") is not runtime.memory.get("bob")
    assert "bob" not in str(runtime.memory.get("alice").history())


async def test_memory_off_records_nothing_and_sends_no_history(memory_sink: MemorySink) -> None:
    """`full_no_memory` is an ablation row, so it has to be a flag rather than a code path."""
    runtime = _runtime(
        [_plan("consultation"), ScriptedResponse(content="An answer.")],
        config=RunConfig(name="full_no_memory", memory=False),
    )
    tracer = Tracer(session_id="nomem", turn_index=0, sink=memory_sink)

    await run_turn(runtime, "a question", tracer=tracer)

    assert len(runtime.memory.get("nomem")) == 0


async def test_the_history_reaches_the_specialist_that_answers(memory_sink: MemorySink) -> None:
    from consilium.memory import SOURCES_PREFIX

    runtime = _runtime(
        [
            _plan("consultation"),
            ScriptedResponse(content="First answer."),
            _plan("consultation"),
            ScriptedResponse(content="Second answer."),
        ]
    )
    for turn_index, question in enumerate(["first question", "second question"]):
        tracer = Tracer(session_id="hist", turn_index=turn_index, sink=memory_sink)
        await run_turn(runtime, question, tracer=tracer)

    history = runtime.memory.get("hist").history()
    assert [message.role for message in history] == ["user", "assistant", "user", "assistant"]
    assert history[0].content == "first question"
    assert SOURCES_PREFIX not in (history[1].content or "")  # no tools ran, so no citations


async def test_episodic_memory_records_one_row_per_session_and_recalls_nothing_by_default(
    tmp_path: Path, memory_sink: MemorySink
) -> None:
    settings = Settings(
        root_dir=ROOT,
        data_dir=ROOT / "data",
        corpus_dir=ROOT / "data" / "corpus",
        episodic_db_path=tmp_path / "episodic.db",
    )
    runtime = build_runtime(
        settings,
        provider=MockProvider(
            [_plan("consultation"), ScriptedResponse(content="Remembered answer.")]
        ),
        embedder="hash",
        store="numpy",
        episodic=True,
    )
    tracer = Tracer(session_id="episodes", turn_index=0, sink=memory_sink)

    await run_turn(runtime, "a question worth remembering", tracer=tracer)

    assert runtime.episodic is not None
    # `with` rather than a trailing `close()`: a failed assertion would otherwise leak the SQLite
    # connection, and a leaked connection fails an unrelated test whenever the collector next runs.
    with runtime.episodic as episodic:
        assert episodic.store.count() == 1
        assert episodic.recall_enabled is False
        assert episodic.recall("a question worth remembering") == []
