"""The Vertex AI Gemini glue: contents assembly, tool translation, response unwrapping, accounting.

Same bargain as `tests/test_provider_clients.py`: a stub stands in place of the `google.genai`
client at the provider's own `client` constructor seam, and every assertion is about code in this
repository -- which role a tool result is sent under, which JSON Schema keywords are stripped before
Gemini sees them, whether thinking tokens reach the cost figure, and whether a turn that asked for a
tool is reported as a tool-call turn. Nothing here claims a call to Vertex would succeed; that is
what the measured run in `docs/gcp-migration-eval.md` is for.

This provider exists so the legacy router can run on Gemini, which is the arm that separates
"the orchestrator changed" from "the model changed" in that document.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from consilium.llm.base import Message, ToolCall
from consilium.llm.vertex_provider import (
    PROVIDER_LABEL,
    VertexProvider,
    to_gemini_contents,
    to_gemini_tools,
)
from consilium.skills import SkillRegistry
from consilium.trace import LLMCallEvent, MemorySink, Tracer

QUESTION = [Message(role="user", content="what is hypertension")]


# --- Gemini-shaped stubs ------------------------------------------------------------------------


@dataclass
class _FunctionCall:
    name: str
    args: dict[str, Any]


@dataclass
class _Part:
    text: str | None = None
    function_call: _FunctionCall | None = None
    thought: bool = False


@dataclass
class _Content:
    parts: list[_Part]


@dataclass
class _FinishReason:
    name: str


@dataclass
class _Candidate:
    content: _Content
    finish_reason: _FinishReason | None = None


@dataclass
class _Usage:
    prompt_token_count: int = 0
    candidates_token_count: int = 0
    thoughts_token_count: int = 0


@dataclass
class _Response:
    candidates: list[_Candidate]
    usage_metadata: _Usage = field(default_factory=_Usage)
    model_version: str | None = None


class _Models:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def generate_content(self, **kwargs: Any) -> _Response:
        self.calls.append(kwargs)
        return self.response


class _Aio:
    def __init__(self, models: _Models) -> None:
        self.models = models


class _Client:
    def __init__(self, response: _Response) -> None:
        self.models = _Models(response)
        self.aio = _Aio(self.models)


def _provider(response: _Response, **kwargs: Any) -> tuple[VertexProvider, _Client]:
    client = _Client(response)
    provider = VertexProvider(
        project="p", location="us-west1", model="gemini-2.5-flash", client=client, **kwargs
    )
    return provider, client


def _text_response(text: str = "Hypertension is raised blood pressure.") -> _Response:
    return _Response(
        candidates=[
            _Candidate(
                content=_Content(parts=[_Part(text=text)]), finish_reason=_FinishReason("STOP")
            )
        ]
    )


# --- translation --------------------------------------------------------------------------------


def test_the_system_prompt_is_lifted_out_of_the_messages() -> None:
    """Gemini takes it as `system_instruction`; a `system` role in `contents` is not a thing."""
    system, contents = to_gemini_contents(
        [Message(role="system", content="be careful"), Message(role="user", content="hi")]
    )

    assert system == "be careful"
    assert [c.role for c in contents] == ["user"]


def test_a_tool_result_is_a_user_turn_and_consecutive_results_merge_into_one() -> None:
    """There is no `tool` role. Separate turns would describe a conversation that did not happen."""
    _, contents = to_gemini_contents(
        [
            Message(role="user", content="q"),
            Message(
                role="assistant",
                tool_calls=[
                    ToolCall(id="call_0", name="search_knowledge", arguments={"query": "x"}),
                    ToolCall(id="call_1", name="assess_risk", arguments={"text": "y"}),
                ],
            ),
            Message(role="tool", name="search_knowledge", content="first", tool_call_id="call_0"),
            Message(role="tool", name="assess_risk", content="second", tool_call_id="call_1"),
        ]
    )

    assert [c.role for c in contents] == ["user", "model", "user"]
    assert contents[-1].parts is not None
    assert len(contents[-1].parts) == 2


def test_an_assistant_turn_carries_its_tool_calls_as_function_call_parts() -> None:
    _, contents = to_gemini_contents(
        [
            Message(role="user", content="q"),
            Message(
                role="assistant",
                content="let me look",
                tool_calls=[
                    ToolCall(id="call_0", name="search_knowledge", arguments={"query": "x"})
                ],
            ),
        ]
    )

    model_turn = contents[1]
    assert model_turn.role == "model"
    assert model_turn.parts is not None
    assert len(model_turn.parts) == 2  # the text, then the call


def test_the_registry_schemas_translate_without_the_keywords_gemini_rejects() -> None:
    """`additionalProperties` and friends are a 400, not a warning, so they are stripped."""
    tools = SkillRegistry.discover().to_tool_schemas()

    translated = to_gemini_tools(tools)

    assert len(translated) == 1  # one Tool holding every declaration
    declarations = translated[0].function_declarations
    assert declarations is not None
    assert len(declarations) == len(tools)
    serialised = str([d.parameters for d in declarations])
    assert "additionalProperties" not in serialised
    assert "$defs" not in serialised


# --- the call -----------------------------------------------------------------------------------


async def test_tools_are_sent_with_the_sdk_tool_loop_disabled() -> None:
    """The SDK would otherwise run its own loop, moving the budget outside `agents/loop.py`."""
    provider, client = _provider(_text_response())

    await provider.chat(QUESTION, tools=SkillRegistry.discover().to_tool_schemas())

    config = client.models.calls[0]["config"]
    assert config.tools
    assert config.automatic_function_calling is not None
    assert config.automatic_function_calling.disable is True


async def test_no_tools_key_and_no_tool_loop_config_when_none_are_offered() -> None:
    provider, client = _provider(_text_response())

    await provider.chat(QUESTION)

    assert not client.models.calls[0]["config"].tools


async def test_a_function_call_becomes_a_tool_call_with_a_synthesised_id() -> None:
    """Gemini returns no id; the ReAct loop needs one to match the result back to the call."""
    response = _Response(
        candidates=[
            _Candidate(
                content=_Content(
                    parts=[_Part(function_call=_FunctionCall("search_knowledge", {"query": "x"}))]
                ),
                finish_reason=_FinishReason("STOP"),
            )
        ]
    )
    provider, _ = _provider(response)

    result = await provider.chat(QUESTION, tools=SkillRegistry.discover().to_tool_schemas())

    assert [c.name for c in result.tool_calls] == ["search_knowledge"]
    assert result.tool_calls[0].id == "call_0"
    assert result.tool_calls[0].arguments == {"query": "x"}


async def test_a_turn_that_asked_for_a_tool_is_a_tool_call_turn_even_though_gemini_says_stop() -> (
    None
):
    """Gemini reports STOP alongside function calls; the loop branches on `stop_reason`."""
    response = _Response(
        candidates=[
            _Candidate(
                content=_Content(parts=[_Part(function_call=_FunctionCall("assess_risk", {}))]),
                finish_reason=_FinishReason("STOP"),
            )
        ]
    )
    provider, _ = _provider(response)

    assert (await provider.chat(QUESTION)).stop_reason == "tool_calls"


async def test_a_thinking_part_is_counted_but_never_delivered() -> None:
    """It is billed as output and it is not an answer; pasting it into `content` would ship it."""
    response = _Response(
        candidates=[
            _Candidate(
                content=_Content(
                    parts=[_Part(text="internal", thought=True), _Part(text="the answer")]
                ),
                finish_reason=_FinishReason("STOP"),
            )
        ],
        usage_metadata=_Usage(
            prompt_token_count=10, candidates_token_count=5, thoughts_token_count=7
        ),
    )
    provider, _ = _provider(response)

    result = await provider.chat(QUESTION)

    assert result.content == "the answer"
    assert result.usage.completion_tokens == 12  # 5 answer + 7 thinking


async def test_a_malformed_function_call_is_an_error_not_a_clean_stop() -> None:
    response = _Response(
        candidates=[
            _Candidate(
                content=_Content(parts=[]),
                finish_reason=_FinishReason("MALFORMED_FUNCTION_CALL"),
            )
        ]
    )
    provider, _ = _provider(response)

    assert (await provider.chat(QUESTION)).stop_reason == "error"


async def test_an_unknown_finish_reason_maps_to_other_rather_than_raising() -> None:
    response = _Response(
        candidates=[
            _Candidate(
                content=_Content(parts=[_Part(text="x")]), finish_reason=_FinishReason("WAT")
            )
        ]
    )
    provider, _ = _provider(response)

    assert (await provider.chat(QUESTION)).stop_reason == "other"


async def test_the_traced_call_carries_the_vertexai_label_the_cost_table_prices() -> None:
    """`eval/pricing.yaml` is keyed `vertexai/gemini-2.5-flash`; another label would read free."""
    sink = MemorySink()
    tracer = Tracer(session_id="s", turn_index=0, sink=sink)
    response = _text_response()
    response.usage_metadata = _Usage(prompt_token_count=11, candidates_token_count=3)
    provider, _ = _provider(response)

    await provider.chat(QUESTION, tracer=tracer, caller="planner")

    events = [e for e in sink.events if isinstance(e, LLMCallEvent)]
    assert len(events) == 1
    assert events[0].provider == PROVIDER_LABEL == "vertexai"
    assert events[0].model == "gemini-2.5-flash"
    assert events[0].prompt_tokens == 11
    assert events[0].completion_tokens == 3


async def test_the_returned_model_version_wins_over_the_configured_id() -> None:
    """What answered is what the trace should name, as it is on the other two providers."""
    response = _text_response()
    response.model_version = "gemini-2.5-flash-001"
    provider, _ = _provider(response)

    assert (await provider.chat(QUESTION)).model == "gemini-2.5-flash-001"


async def test_max_tokens_is_sent_only_when_configured() -> None:
    """Unlike Anthropic, Gemini does not require it, so the default is the model's own."""
    provider, client = _provider(_text_response())
    await provider.chat(QUESTION)
    assert client.models.calls[0]["config"].max_output_tokens is None

    provider, client = _provider(_text_response(), max_tokens=8192)
    await provider.chat(QUESTION)
    assert client.models.calls[0]["config"].max_output_tokens == 8192


# --- the judge seam -----------------------------------------------------------------------------


def test_the_judge_defaults_to_the_agents_provider() -> None:
    """One run, one key, nothing to configure: the judge grades with what the agents ran on."""
    from consilium.config import Settings
    from eval.run import judge_provider

    settings = Settings(provider="mock")

    class _Runtime:
        provider = object()

    assert judge_provider(settings, _Runtime()) is _Runtime.provider  # type: ignore[arg-type]


def test_the_judge_can_be_pinned_away_from_the_agents_provider() -> None:
    """A comparison across agent models must grade every arm with one instrument."""
    from consilium.config import Settings
    from eval.run import judge_provider

    settings = Settings(provider="vertexai", judge_provider="mock", judge_model="judge-model")

    class _Runtime:
        provider = object()

    pinned = judge_provider(settings, _Runtime())  # type: ignore[arg-type]

    assert pinned is not _Runtime.provider
    assert pinned.model == "judge-model"
