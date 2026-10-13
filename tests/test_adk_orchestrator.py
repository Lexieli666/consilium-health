"""`ORCHESTRATOR=adk`: the same turn, through Google ADK agents, into the same trace.

Offline, like every other test here: a scripted ``BaseLlm`` is registered with ADK's model
registry under a fake model name, so the whole path -- ``run_turn`` -> ``AdkOrchestrator`` ->
root agent -> planner / specialists / synthesizer -> ADK tools -> ``SkillRegistry`` -- runs with no
key, no network and no Gemini.  What is asserted is the thing the migration must not break: the
events in the trace are the legacy events, with the legacy ``caller`` labels, so the eval harness
reads an ADK trace unchanged.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from dataclasses import replace
from pathlib import Path

import pytest
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.models.registry import LLMRegistry
from google.genai import types

from consilium.config import RunConfig, Settings
from consilium.llm import MockProvider
from consilium.mcp_server import build_server
from consilium.runtime import Runtime, build_runtime, run_turn
from consilium.trace import (
    BlackboardEvent,
    LLMCallEvent,
    MemorySink,
    RetrievalEvent,
    RouteEvent,
    ToolCallEvent,
    Tracer,
    TurnEvent,
)

ROOT = Path(__file__).resolve().parents[1]
FAKE_MODEL = "consilium-fake-gemini"

#: ``(who, tools offered)`` per model call, in call order.  Module-level because ADK constructs
#: the model from the registry, so a test cannot hold the instance.
CALLS: list[tuple[str, list[str]]] = []


class ScriptedGemini(BaseLlm):
    """A stand-in for Vertex AI Gemini that behaves like a cooperative model.

    The planner gets a two-agent plan; a specialist calls one permitted skill, then answers; the
    synthesizer merges.  Which agent is asking is read off the system instruction, the same way a
    real model would tell them apart.
    """

    model: str = FAKE_MODEL

    @classmethod
    def supported_models(cls) -> list[str]:
        return [FAKE_MODEL]

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        del stream
        system = str(llm_request.config.system_instruction or "")
        offered = [
            d.name
            for tool in (llm_request.config.tools or [])
            for d in (getattr(tool, "function_declarations", None) or [])
            if d.name
        ]
        usage = types.GenerateContentResponseUsageMetadata(
            prompt_token_count=100, candidates_token_count=20, thoughts_token_count=5
        )
        if "You are the planner" in system:
            text = json.dumps(
                {
                    "subtasks": [
                        {"agent": "diagnostic", "objective": "Assess urgency.", "why": "t"},
                        {"agent": "consultation", "objective": "Explain.", "why": "t"},
                    ]
                }
            )
            CALLS.append(("planner", offered))
            yield LlmResponse(
                content=types.Content(role="model", parts=[types.Part(text=text)]),
                usage_metadata=usage,
                finish_reason=types.FinishReason.STOP,
            )
            return
        if "You merge the answers" in system:
            CALLS.append(("synthesizer", offered))
            yield LlmResponse(
                content=types.Content(role="model", parts=[types.Part(text="Merged answer.")]),
                usage_metadata=usage,
                finish_reason=types.FinishReason.STOP,
            )
            return
        CALLS.append(("specialist", offered))
        if "search_knowledge" in offered:
            part = types.Part.from_function_call(
                name="search_knowledge", args={"query": "hypertension"}
            )
            yield LlmResponse(
                content=types.Content(role="model", parts=[part]),
                usage_metadata=usage,
                finish_reason=types.FinishReason.STOP,
            )
            return
        yield LlmResponse(
            content=types.Content(
                role="model", parts=[types.Part(text="Specialist answer, grounded.")]
            ),
            usage_metadata=usage,
            finish_reason=types.FinishReason.STOP,
        )


LLMRegistry.register(ScriptedGemini)


def _adk_runtime(config: RunConfig | None = None) -> Runtime:
    settings = Settings(
        root_dir=ROOT,
        data_dir=ROOT / "data",
        corpus_dir=ROOT / "data" / "corpus",
        orchestrator="adk",
        adk_model=FAKE_MODEL,
    )
    return build_runtime(
        settings, config=config, provider=MockProvider([]), embedder="hash", store="numpy"
    )


@pytest.fixture(autouse=True)
def _reset_calls() -> None:
    CALLS.clear()


async def test_adk_turn_lands_in_the_legacy_trace_schema(memory_sink: MemorySink) -> None:
    """Every event type the harness reads, with the legacy caller labels, from ADK callbacks."""
    runtime = _adk_runtime(RunConfig(name="full", max_tool_calls=1))
    tracer = Tracer(session_id="adk-smoke", turn_index=0, sink=memory_sink)

    outcome = await run_turn(runtime, "Is my blood pressure of 150/95 dangerous?", tracer=tracer)

    assert outcome.answer.startswith("Merged answer.") or "Merged answer." in outcome.answer
    assert outcome.mode == "parallel"
    assert outcome.agents == ("diagnostic", "consultation")
    assert not outcome.fallback
    assert outcome.missing == ()

    (route,) = memory_sink.of_type(RouteEvent)
    assert route.agents == ["diagnostic", "consultation"]
    assert route.fallback is False

    callers = [event.caller for event in memory_sink.of_type(LLMCallEvent)]
    assert callers[0] == "planner"
    assert callers[-1] == "synthesizer"
    assert "agent:diagnostic" in callers and "agent:consultation" in callers
    # max_tool_calls=1: after one executed skill the next call goes out with tools withheld.
    assert callers.count("forced_answer") == 2
    for event in memory_sink.of_type(LLMCallEvent):
        assert event.provider == "vertexai"
        assert event.model == FAKE_MODEL
        assert event.prompt_tokens == 100
        assert event.completion_tokens == 25  # candidates + thinking tokens

    tool_calls = memory_sink.of_type(ToolCallEvent)
    assert sorted(event.agent for event in tool_calls) == ["consultation", "diagnostic"]
    assert all(event.skill == "search_knowledge" and event.ok for event in tool_calls)
    assert all(event.transport == "internal" for event in tool_calls)
    assert len(memory_sink.of_type(RetrievalEvent)) == 2
    assert outcome.sources, "the merged answer carries the workers' cited doc_ids"

    kinds = [event.event for event in memory_sink.of_type(BlackboardEvent)]
    assert kinds.count("assigned") == 2 and kinds.count("completed") == 2

    (turn,) = memory_sink.of_type(TurnEvent)
    assert memory_sink.events[-1] is turn
    assert turn.answer == outcome.answer


async def test_adk_tools_are_withheld_when_the_budget_is_zero(memory_sink: MemorySink) -> None:
    """``baseline_llm`` on ADK: no tools offered, no forced_answer label, no tool_call event."""
    runtime = _adk_runtime(
        RunConfig(
            name="baseline_llm", retrieval=False, router="none", memory=False, max_tool_calls=0
        )
    )
    tracer = Tracer(session_id="adk-baseline", turn_index=0, sink=memory_sink)

    outcome = await run_turn(runtime, "What is GERD?", tracer=tracer)

    assert outcome.mode == "single" and outcome.agents == ("consultation",)
    assert memory_sink.of_type(RouteEvent) == []
    (call,) = memory_sink.of_type(LLMCallEvent)
    assert call.caller == "agent:consultation"
    assert call.tools_offered == []
    assert memory_sink.of_type(ToolCallEvent) == []


def test_the_mcp_server_still_builds_on_the_adk_runtime() -> None:
    """The MCP server serves the registry, not the orchestrator; ORCHESTRATOR=adk is irrelevant."""
    runtime = _adk_runtime()
    server = build_server(runtime, session_id="mcp-adk")
    assert server.name


def test_the_default_orchestrator_is_still_legacy() -> None:
    runtime = replace(_adk_runtime(), settings=Settings(root_dir=ROOT))
    assert runtime.settings.orchestrator == "legacy"
    assert type(runtime.orchestrator()).__name__ == "Router"
