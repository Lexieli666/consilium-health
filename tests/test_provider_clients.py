"""The provider glue: request assembly, response unwrapping, retries, streaming accumulation.

**On the stub clients in this file.** The offline rule is satisfied for the LLM layer by
`MockProvider` -- a real second implementation of `LLMProvider`, which is what the rest of the suite
runs against and what a reviewer should look at. It cannot, however, exercise `OpenAIProvider` or
`AnthropicProvider` at all, and those two classes contain real behaviour with real bugs available to
it: which keys go into the request, which exceptions are retried and which are not, and how a
stream's pieces are accumulated into one recorded `llm_call`.

So these tests inject a small stub in place of the SDK client and assert on our own glue. That is a
different thing from mocking `sentence_transformers` or `chromadb`: the seam being used here is the
provider's own `client` constructor argument, the assertions are about code in this repository, and
nothing here claims that a call to a real endpoint would succeed. The parts that only a real
endpoint can prove are marked `network` and excluded by default.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import anthropic
import httpx
import openai
import pytest

from consilium.llm.anthropic_provider import AnthropicProvider
from consilium.llm.base import Message
from consilium.llm.openai_provider import OpenAIProvider
from consilium.skills import SkillRegistry
from consilium.trace import LLMCallEvent, MemorySink, Tracer

QUESTION = [Message(role="user", content="what is hypertension")]


# --- OpenAI-shaped stubs ------------------------------------------------------------------------


@dataclass
class _Function:
    name: str
    arguments: str


@dataclass
class _OpenAIToolCall:
    id: str
    function: _Function


@dataclass
class _OpenAIMessage:
    content: str | None = None
    tool_calls: list[_OpenAIToolCall] = field(default_factory=list)


@dataclass
class _OpenAIChoice:
    message: _OpenAIMessage
    finish_reason: str = "stop"


@dataclass
class _OpenAIUsage:
    prompt_tokens: int = 11
    completion_tokens: int = 7


@dataclass
class _OpenAICompletion:
    choices: list[_OpenAIChoice]
    model: str = "gpt-4o-mini"
    usage: _OpenAIUsage = field(default_factory=_OpenAIUsage)


class _OpenAIClient:
    """Records the request it was handed and replays a scripted sequence of outcomes."""

    def __init__(self, outcomes: list[Any]) -> None:
        self.requests: list[dict[str, Any]] = []
        self._outcomes = list(outcomes)
        self.chat = self  # the SDK's `client.chat.completions.create` path
        self.completions = self

    async def create(self, **request: Any) -> Any:
        self.requests.append(request)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _completion(content: str = "An answer.", **kwargs: Any) -> _OpenAICompletion:
    return _OpenAICompletion(
        choices=[_OpenAIChoice(message=_OpenAIMessage(content=content), **kwargs)]
    )


def _transient() -> openai.APIConnectionError:
    return openai.APIConnectionError(request=httpx.Request("POST", "https://example.invalid"))


def _provider(client: _OpenAIClient) -> OpenAIProvider:
    return OpenAIProvider(api_key="not-a-real-key", client=client, backoff_multiplier=0.0)


async def test_openai_request_carries_the_model_messages_and_tools(
    registry: SkillRegistry, memory_sink: MemorySink
) -> None:
    client = _OpenAIClient([_completion()])
    provider = _provider(client)
    tracer = Tracer(session_id="s", turn_index=0, sink=memory_sink)

    response = await provider.chat(
        QUESTION,
        tools=registry.to_tool_schemas(["assess_risk"]),
        tracer=tracer,
        caller="agent:diagnostic",
        temperature=0.0,
    )

    (request,) = client.requests
    assert request["model"] == "gpt-4o-mini"
    assert request["messages"] == [{"role": "user", "content": "what is hypertension"}]
    assert request["tools"][0]["function"]["name"] == "assess_risk"
    assert request["temperature"] == 0.0

    assert response.content == "An answer."
    assert response.provider == "openai"
    (event,) = memory_sink.of_type(LLMCallEvent)
    assert event.caller == "agent:diagnostic"
    assert (event.prompt_tokens, event.completion_tokens) == (11, 7)
    assert event.tools_offered == ["assess_risk"]


async def test_openai_omits_the_tools_key_entirely_when_none_are_offered() -> None:
    """A forced answer must not carry an empty tool list; some endpoints treat that differently."""
    client = _OpenAIClient([_completion()])

    await _provider(client).chat(QUESTION, tools=None)

    assert "tools" not in client.requests[0]


async def test_openai_unwraps_tool_calls_and_their_arguments() -> None:
    completion = _OpenAICompletion(
        choices=[
            _OpenAIChoice(
                message=_OpenAIMessage(
                    content=None,
                    tool_calls=[
                        _OpenAIToolCall(
                            id="call_1",
                            function=_Function(
                                name="search_knowledge", arguments='{"query": "htn"}'
                            ),
                        )
                    ],
                ),
                finish_reason="tool_calls",
            )
        ]
    )

    response = await _provider(_OpenAIClient([completion])).chat(QUESTION)

    assert response.stop_reason == "tool_calls"
    (call,) = response.tool_calls
    assert (call.id, call.name, call.arguments) == ("call_1", "search_knowledge", {"query": "htn"})


async def test_openai_maps_an_unknown_finish_reason_to_other_rather_than_raising() -> None:
    """A new finish reason is a provider change; dropping a good answer over a label is worse."""
    response = await _provider(_OpenAIClient([_completion(finish_reason="something_new")])).chat(
        QUESTION
    )

    assert response.stop_reason == "other"
    assert response.content == "An answer."


async def test_openai_retries_a_transient_failure_and_then_succeeds() -> None:
    client = _OpenAIClient([_transient(), _completion("Recovered.")])

    response = await _provider(client).chat(QUESTION)

    assert response.content == "Recovered."
    assert len(client.requests) == 2


async def test_openai_does_not_retry_a_bad_request() -> None:
    """Three attempts at a malformed request is three times the latency and the same error."""
    error = openai.BadRequestError(
        message="bad",
        response=httpx.Response(400, request=httpx.Request("POST", "https://x.invalid")),
        body=None,
    )
    client = _OpenAIClient([error])

    with pytest.raises(openai.BadRequestError):
        await _provider(client).chat(QUESTION)

    assert len(client.requests) == 1


async def test_openai_gives_up_after_the_attempt_limit_and_reraises() -> None:
    client = _OpenAIClient([_transient() for _ in range(4)])
    provider = OpenAIProvider(api_key="k", client=client, max_attempts=3, backoff_multiplier=0.0)

    with pytest.raises(openai.APIConnectionError):
        await provider.chat(QUESTION)

    assert len(client.requests) == 3


# --- streaming ----------------------------------------------------------------------------------


@dataclass
class _StreamDelta:
    content: str | None = None


@dataclass
class _StreamChoice:
    delta: _StreamDelta
    finish_reason: str | None = None


@dataclass
class _StreamEvent:
    choices: list[_StreamChoice] = field(default_factory=list)
    usage: _OpenAIUsage | None = None


class _OpenAIStream:
    def __init__(self, events: list[_StreamEvent]) -> None:
        self._events = events

    def __aiter__(self) -> AsyncIterator[_StreamEvent]:
        async def _gen() -> AsyncIterator[_StreamEvent]:
            for event in self._events:
                yield event

        return _gen()


async def test_openai_streaming_yields_content_then_a_final_delta_with_usage(
    memory_sink: MemorySink,
) -> None:
    stream = _OpenAIStream(
        [
            _StreamEvent([_StreamChoice(_StreamDelta("Hyper"))]),
            _StreamEvent([_StreamChoice(_StreamDelta("tension."))]),
            _StreamEvent([_StreamChoice(_StreamDelta(None), finish_reason="stop")]),
            _StreamEvent(usage=_OpenAIUsage(prompt_tokens=5, completion_tokens=2)),
        ]
    )
    client = _OpenAIClient([stream])
    tracer = Tracer(session_id="s", turn_index=0, sink=memory_sink)

    deltas = [
        delta
        async for delta in _provider(client).stream_chat(
            QUESTION, tracer=tracer, caller="agent:consultation"
        )
    ]

    assert [d.content for d in deltas[:-1]] == ["Hyper", "tension."]
    assert deltas[-1].content is None
    assert deltas[-1].usage is not None and deltas[-1].usage.completion_tokens == 2
    assert client.requests[0]["stream"] is True

    # One llm_call event for the whole stream, with the assembled answer's token counts.
    (event,) = memory_sink.of_type(LLMCallEvent)
    assert event.caller == "agent:consultation"
    assert (event.prompt_tokens, event.completion_tokens) == (5, 2)


# --- Anthropic-shaped stubs ---------------------------------------------------------------------


@dataclass
class _TextBlock:
    text: str
    type: str = "text"


@dataclass
class _ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]
    type: str = "tool_use"


@dataclass
class _AnthropicUsage:
    input_tokens: int = 13
    output_tokens: int = 4


@dataclass
class _AnthropicMessage:
    content: list[Any]
    stop_reason: str = "end_turn"
    model: str = "claude-sonnet-4-5"
    usage: _AnthropicUsage = field(default_factory=_AnthropicUsage)


class _AnthropicStream:
    def __init__(self, pieces: list[str], final: _AnthropicMessage) -> None:
        self._pieces = pieces
        self._final = final

    async def __aenter__(self) -> _AnthropicStream:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    @property
    def text_stream(self) -> AsyncIterator[str]:
        async def _gen() -> AsyncIterator[str]:
            for piece in self._pieces:
                yield piece

        return _gen()

    async def get_final_message(self) -> _AnthropicMessage:
        return self._final


class _AnthropicClient:
    def __init__(self, outcomes: list[Any], stream: _AnthropicStream | None = None) -> None:
        self.requests: list[dict[str, Any]] = []
        self._outcomes = list(outcomes)
        self._stream = stream
        self.messages = self

    async def create(self, **request: Any) -> Any:
        self.requests.append(request)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def stream(self, **request: Any) -> _AnthropicStream:
        self.requests.append(request)
        assert self._stream is not None
        return self._stream


def _anthropic(client: _AnthropicClient) -> AnthropicProvider:
    return AnthropicProvider(api_key="not-a-real-key", client=client, backoff_multiplier=0.0)


async def test_anthropic_request_lifts_the_system_prompt_and_requires_max_tokens(
    registry: SkillRegistry,
) -> None:
    client = _AnthropicClient([_AnthropicMessage(content=[_TextBlock("An answer.")])])
    messages = [Message(role="system", content="rules"), *QUESTION]

    response = await _anthropic(client).chat(
        messages, tools=registry.to_tool_schemas(["find_guideline"])
    )

    (request,) = client.requests
    assert request["system"] == "rules"
    assert request["max_tokens"] > 0
    assert request["messages"] == [{"role": "user", "content": "what is hypertension"}]
    assert request["tools"][0]["name"] == "find_guideline"
    assert response.content == "An answer."
    assert response.provider == "anthropic"


async def test_anthropic_unwraps_text_and_tool_use_blocks() -> None:
    payload = _AnthropicMessage(
        content=[
            _TextBlock("Let me look that up."),
            _ToolUseBlock(id="tu_1", name="find_guideline", input={"topic": "hypertension"}),
        ],
        stop_reason="tool_use",
    )

    response = await _anthropic(_AnthropicClient([payload])).chat(QUESTION)

    assert response.content == "Let me look that up."
    assert response.stop_reason == "tool_calls"
    (call,) = response.tool_calls
    assert (call.id, call.name, call.arguments) == (
        "tu_1",
        "find_guideline",
        {"topic": "hypertension"},
    )


async def test_anthropic_omits_system_when_there_is_no_system_message() -> None:
    client = _AnthropicClient([_AnthropicMessage(content=[_TextBlock("x")])])

    await _anthropic(client).chat(QUESTION)

    assert "system" not in client.requests[0]


async def test_anthropic_retries_a_transient_failure() -> None:
    error = anthropic.APIConnectionError(request=httpx.Request("POST", "https://example.invalid"))
    client = _AnthropicClient([error, _AnthropicMessage(content=[_TextBlock("Recovered.")])])

    response = await _anthropic(client).chat(QUESTION)

    assert response.content == "Recovered."
    assert len(client.requests) == 2


async def test_anthropic_streaming_accumulates_and_records_one_call(
    memory_sink: MemorySink,
) -> None:
    final = _AnthropicMessage(
        content=[_TextBlock("Hypertension.")],
        usage=_AnthropicUsage(input_tokens=9, output_tokens=3),
    )
    client = _AnthropicClient([], stream=_AnthropicStream(["Hyper", "tension."], final))
    tracer = Tracer(session_id="s", turn_index=0, sink=memory_sink)

    deltas = [
        delta
        async for delta in _anthropic(client).stream_chat(
            QUESTION, tracer=tracer, caller="agent:research"
        )
    ]

    assert [d.content for d in deltas[:-1]] == ["Hyper", "tension."]
    assert deltas[-1].stop_reason == "stop"
    (event,) = memory_sink.of_type(LLMCallEvent)
    assert (event.prompt_tokens, event.completion_tokens) == (9, 3)


def test_the_retry_sets_exclude_the_errors_that_retrying_cannot_fix() -> None:
    from consilium.llm.anthropic_provider import RETRYABLE as ANTHROPIC_RETRYABLE
    from consilium.llm.openai_provider import RETRYABLE as OPENAI_RETRYABLE

    assert openai.RateLimitError in OPENAI_RETRYABLE
    assert openai.BadRequestError not in OPENAI_RETRYABLE
    assert openai.AuthenticationError not in OPENAI_RETRYABLE
    assert anthropic.RateLimitError in ANTHROPIC_RETRYABLE
    assert anthropic.BadRequestError not in ANTHROPIC_RETRYABLE
