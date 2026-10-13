"""`ORCHESTRATOR=langgraph`: the same turn, through a LangGraph graph, into the same trace.

Offline, like every other test here: the mock provider scripts the planner and the synthesizer,
the MCP server runs in-process over the SDK's memory streams, and the index is the hash/numpy
pipeline.  What is asserted is what the evaluation harness depends on -- the events in the turn's
trace are the legacy events (``llm_call`` with the legacy ``caller`` labels, ``tool_call`` with
``transport="mcp"``, ``retrieval``, ``turn``) -- and the two behaviours the graph adds: the
bounded validator -> synthesizer repair loop, and the tool budget enforced inside the MCP tools.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import replace
from typing import Any

import pytest

from consilium.agents.loop import BUDGET_EXHAUSTED_OBSERVATION
from consilium.config import RunConfig, Settings
from consilium.graph import LangGraphOrchestrator, close_all
from consilium.graph.chat_model import from_provider_response, to_provider_message
from consilium.graph.prompts import repair_instruction
from consilium.llm import MockProvider, ScriptedResponse
from consilium.llm.base import Delta, LLMResponse, Message, ToolCall, ToolSchema, Usage
from consilium.llm.mock import ScriptedToolCall
from consilium.runtime import Runtime, run_turn
from consilium.safety.validator import Violation
from consilium.trace import (
    LLMCallEvent,
    MemorySink,
    RetrievalEvent,
    RouteEvent,
    SafetyEvent,
    ToolCallEvent,
    Tracer,
    TurnEvent,
)

DISCLAIMER = (
    "Not medical advice. This is an educational software project. It does not diagnose or treat, "
    "and it must not be used for real medical decisions."
)
RuntimeFactory = Callable[[Any], Runtime]
GROUNDED = "Hypertension is persistently raised blood pressure (condition-hypertension)."


@pytest.fixture(autouse=True)
async def _close_graphs() -> AsyncIterator[None]:
    """Every test gets its own event loop; the MCP sessions opened on it are closed with it."""
    yield
    await close_all()


def _graph_runtime(runtime: Runtime, **config: Any) -> Runtime:
    settings: Settings = runtime.settings.model_copy(update={"orchestrator": "langgraph"})
    run_config = runtime.config.model_copy(update=config) if config else runtime.config
    return replace(runtime, settings=settings, config=run_config)


def _tracer(session: str = "graph-test", turn: int = 0) -> tuple[Tracer, MemorySink]:
    sink = MemorySink()
    return Tracer(session_id=session, turn_index=turn, sink=sink), sink


def _script(*responses: ScriptedResponse) -> MockProvider:
    return MockProvider(list(responses))


def _search(query: str = "hypertension") -> ScriptedResponse:
    return ScriptedResponse(
        tool_calls=[ScriptedToolCall(name="search_knowledge", arguments={"query": query})]
    )


# ----------------------------------------------------------------------------------------------
# The trace: the harness reads a LangGraph turn exactly as it reads a legacy one.
# ----------------------------------------------------------------------------------------------


async def test_a_turn_writes_the_legacy_events_with_mcp_tool_calls(
    offline_runtime: RuntimeFactory,
) -> None:
    runtime = _graph_runtime(
        offline_runtime(_script(_search(), ScriptedResponse(content=f"{GROUNDED} {DISCLAIMER}")))
    )
    tracer, sink = _tracer()
    with tracer:
        outcome = await run_turn(runtime, "What is hypertension?", tracer=tracer)

    assert outcome.answer.startswith(GROUNDED)
    assert outcome.agents == ("planner",)
    assert outcome.mode == "single"
    assert not outcome.fallback

    calls = sink.of_type(LLMCallEvent)
    assert [call.caller for call in calls] == ["planner", "synthesizer"]
    assert len(calls[0].tools_offered) == 7 and calls[1].tools_offered == []

    tools = sink.of_type(ToolCallEvent)
    assert [tool.skill for tool in tools] == ["search_knowledge"]
    assert tools[0].transport == "mcp" and tools[0].ok and tools[0].agent == "planner"
    assert tools[0].args == {"query": "hypertension"}

    retrievals = sink.of_type(RetrievalEvent)
    assert len(retrievals) == 1 and retrievals[0].query == "hypertension"
    assert retrievals[0].fused_topk  # the eval's recall@5 reads this ranking
    assert outcome.sources == tuple(tools[0].source_doc_ids)
    assert len(outcome.tool_results) == 1 and outcome.tool_results[0].skill == "search_knowledge"

    # No specialists were routed, so no route event: routing accuracy is n/a, not 0 or 100.
    assert sink.of_type(RouteEvent) == []
    (turn,) = sink.of_type(TurnEvent)
    assert turn.answer == outcome.answer
    # The server-side envelope events are not forwarded; the boundary found nothing to repair.
    assert [event.rule for event in sink.of_type(SafetyEvent)] == []


async def test_a_planner_that_needs_no_tool_goes_straight_to_the_synthesizer(
    offline_runtime: RuntimeFactory,
) -> None:
    runtime = _graph_runtime(
        offline_runtime(
            _script(
                ScriptedResponse(content="Hello."),
                ScriptedResponse(content=f"Hello. Ask me about a condition. {DISCLAIMER}"),
            )
        )
    )
    tracer, sink = _tracer()
    with tracer:
        outcome = await run_turn(runtime, "hi", tracer=tracer)

    assert outcome.answer.startswith("Hello. Ask me")
    assert sink.of_type(ToolCallEvent) == []
    assert [call.caller for call in sink.of_type(LLMCallEvent)] == ["planner", "synthesizer"]


# ----------------------------------------------------------------------------------------------
# The repair loop: validator -> synthesizer, at most `graph_max_repairs` times.
# ----------------------------------------------------------------------------------------------


async def test_a_draft_missing_the_disclaimer_is_resynthesized_once(
    offline_runtime: RuntimeFactory,
) -> None:
    runtime = _graph_runtime(
        offline_runtime(
            _script(
                _search(),
                ScriptedResponse(content=GROUNDED),
                ScriptedResponse(content=f"{GROUNDED} {DISCLAIMER}"),
            )
        )
    )
    tracer, sink = _tracer()
    with tracer:
        outcome = await run_turn(runtime, "What is hypertension?", tracer=tracer)

    callers = [call.caller for call in sink.of_type(LLMCallEvent)]
    assert callers == ["planner", "synthesizer", "synthesizer"]
    # The second synthesis fixed it, so the boundary's deterministic repair had nothing to do.
    assert outcome.safety.repairs == ()
    assert [event.rule for event in sink.of_type(SafetyEvent)] == []
    assert outcome.answer.endswith(DISCLAIMER)


async def test_the_repair_instruction_quotes_the_policy_not_the_model(
    offline_runtime: RuntimeFactory,
) -> None:
    """What the synthesizer is told on a repair round is derived from policy.yaml."""
    runtime = offline_runtime(MockProvider([]))
    required = runtime.policy.output.required_element("disclaimer")
    text = repair_instruction(
        runtime.policy,
        [
            Violation(rule="disclaimer", scope="output", detail="missing"),
            Violation(rule="escalation_required", scope="output", detail="missing"),
            Violation(
                rule="dosing_instruction",
                scope="output",
                detail="dose",
                sentence="Take 500 mg twice daily.",
            ),
        ],
    )
    assert required.normalized in text
    assert runtime.policy.output.escalation.text in text
    assert "Take 500 mg twice daily." in text


async def test_the_loop_stops_at_max_repairs_and_the_boundary_guard_still_delivers(
    offline_runtime: RuntimeFactory,
) -> None:
    runtime = _graph_runtime(
        offline_runtime(
            _script(
                _search(),
                ScriptedResponse(content=GROUNDED),
                ScriptedResponse(content=GROUNDED),
                ScriptedResponse(content=GROUNDED),
            )
        )
    )
    tracer, sink = _tracer()
    with tracer:
        outcome = await run_turn(runtime, "What is hypertension?", tracer=tracer)

    callers = [call.caller for call in sink.of_type(LLMCallEvent)]
    assert callers == ["planner", "synthesizer", "synthesizer", "synthesizer"]
    # Measured once, at the boundary, on the draft the graph handed over: one violation, one
    # deterministic repair -- the same accounting as the legacy orchestrator.
    assert [(e.event, e.rule) for e in sink.of_type(SafetyEvent)] == [
        ("violation", "disclaimer"),
        ("repair", "disclaimer"),
    ]
    assert outcome.answer.endswith(DISCLAIMER)
    assert outcome.safety.repair_applied


async def test_max_repairs_zero_disables_the_loop(offline_runtime: RuntimeFactory) -> None:
    runtime = offline_runtime(_script(_search(), ScriptedResponse(content=GROUNDED)))
    runtime = replace(
        runtime,
        settings=runtime.settings.model_copy(
            update={"orchestrator": "langgraph", "graph_max_repairs": 0}
        ),
    )
    tracer, sink = _tracer()
    with tracer:
        outcome = await run_turn(runtime, "What is hypertension?", tracer=tracer)

    assert [c.caller for c in sink.of_type(LLMCallEvent)] == ["planner", "synthesizer"]
    assert outcome.safety.repairs == ("disclaimer",)


# ----------------------------------------------------------------------------------------------
# The tool budget, enforced inside the tools, exactly as the legacy loop enforces it.
# ----------------------------------------------------------------------------------------------


async def test_calls_past_the_budget_are_refused_without_a_tool_call_event(
    offline_runtime: RuntimeFactory,
) -> None:
    planner = ScriptedResponse(
        tool_calls=[
            ScriptedToolCall(name="search_knowledge", arguments={"query": "a"}),
            ScriptedToolCall(name="lookup_disease_code", arguments={"condition": "asthma"}),
            ScriptedToolCall(name="search_knowledge", arguments={"query": "c"}),
        ]
    )
    recorder = _Recorder(planner, ScriptedResponse(content=f"{GROUNDED} {DISCLAIMER}"))
    runtime = _graph_runtime(offline_runtime(recorder), max_tool_calls=2)
    tracer, sink = _tracer()
    with tracer:
        outcome = await run_turn(runtime, "asthma", tracer=tracer)

    executed = sink.of_type(ToolCallEvent)
    assert len(executed) == 2
    assert {tool.skill for tool in executed} == {"search_knowledge", "lookup_disease_code"}
    assert len(outcome.tool_results) == 2
    # The synthesizer still saw a tool message for the refused call, as the API requires.
    synthesizer_messages = recorder.seen[1]
    observations = [m.content for m in synthesizer_messages if m.role == "tool"]
    assert len(observations) == 3
    assert BUDGET_EXHAUSTED_OBSERVATION in observations


# ----------------------------------------------------------------------------------------------
# Session memory: the checkpointer carries the thread; `memory=False` gives every turn a new one.
# ----------------------------------------------------------------------------------------------


async def test_the_thread_carries_prior_turns_when_memory_is_on(
    offline_runtime: RuntimeFactory,
) -> None:
    recorder = _Recorder(
        ScriptedResponse(content="first"),
        ScriptedResponse(content=f"First answer. {DISCLAIMER}"),
        ScriptedResponse(content="second"),
        ScriptedResponse(content=f"Second answer. {DISCLAIMER}"),
    )
    runtime = _graph_runtime(offline_runtime(recorder), memory=True)
    for turn, question in enumerate(["first question", "second question"]):
        tracer, _ = _tracer("graph-memory", turn)
        with tracer:
            await run_turn(runtime, question, tracer=tracer)

    second_planner = recorder.seen[2]
    contents = [m.content for m in second_planner if m.role in ("user", "assistant")]
    # The thread holds the first turn's question and its answer, and nothing of its scratch.
    assert contents[0] == "first question" and str(contents[1]).startswith("First answer.")
    assert contents[-1] == "second question"
    assert not any(m.role == "tool" for m in second_planner)


async def test_memory_off_gives_every_turn_a_fresh_thread(offline_runtime: RuntimeFactory) -> None:
    recorder = _Recorder(
        ScriptedResponse(content="first"),
        ScriptedResponse(content=f"First answer. {DISCLAIMER}"),
        ScriptedResponse(content="second"),
        ScriptedResponse(content=f"Second answer. {DISCLAIMER}"),
    )
    runtime = _graph_runtime(offline_runtime(recorder), memory=False)
    for turn, question in enumerate(["first question", "second question"]):
        tracer, _ = _tracer("graph-no-memory", turn)
        with tracer:
            await run_turn(runtime, question, tracer=tracer)

    contents = [m.content for m in recorder.seen[2] if m.role in ("user", "assistant")]
    assert contents == ["second question"]


# ----------------------------------------------------------------------------------------------
# Wiring and conversions.
# ----------------------------------------------------------------------------------------------


def test_settings_accept_the_langgraph_orchestrator(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORCHESTRATOR", "langgraph")
    monkeypatch.setenv("CONSILIUM_GRAPH_MAX_REPAIRS", "1")
    monkeypatch.setenv("CONSILIUM_GRAPH_MCP", "stdio")
    settings = Settings.from_env(load_env_file=False)
    assert settings.orchestrator == "langgraph"
    assert settings.graph_max_repairs == 1
    assert settings.graph_mcp == "stdio"

    monkeypatch.setenv("CONSILIUM_GRAPH_MAX_REPAIRS", "two")
    with pytest.raises(ValueError, match="CONSILIUM_GRAPH_MAX_REPAIRS"):
        Settings.from_env(load_env_file=False)


def test_the_runtime_builds_the_langgraph_orchestrator(offline_runtime: RuntimeFactory) -> None:
    runtime = _graph_runtime(offline_runtime(MockProvider([])))
    assert isinstance(runtime.orchestrator(), LangGraphOrchestrator)


def test_message_conversion_round_trips_tool_calls() -> None:
    response = LLMResponse(
        content=None,
        tool_calls=[ToolCall(id="call_1", name="search_knowledge", arguments={"query": "x"})],
        stop_reason="tool_calls",
        provider="mock",
        model="m",
        usage=Usage(prompt_tokens=3, completion_tokens=4),
    )
    message = from_provider_response(response)
    assert message.tool_calls[0]["name"] == "search_knowledge"
    assert message.usage_metadata == {"input_tokens": 3, "output_tokens": 4, "total_tokens": 7}
    back = to_provider_message(message)
    assert back.role == "assistant" and back.content is None
    assert back.tool_calls == response.tool_calls


class _Recorder:
    """A scripted provider that also keeps every message list it was given, in call order."""

    name = "mock"
    model = "mock-model"

    def __init__(self, *responses: ScriptedResponse) -> None:
        self._inner = MockProvider(list(responses))
        self.seen: list[list[Message]] = []

    async def chat(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSchema] | None = None,
        tracer: Tracer | None = None,
        caller: str | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        self.seen.append(list(messages))
        return await self._inner.chat(messages, tools=tools, tracer=tracer, caller=caller, **kwargs)

    def stream_chat(
        self,
        messages: Sequence[Message],
        *,
        tracer: Tracer | None = None,
        caller: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[Delta]:
        return self._inner.stream_chat(messages, tracer=tracer, caller=caller, **kwargs)


def _unused(_: RunConfig) -> None:  # pragma: no cover - keeps the import honest for mypy
    pass
