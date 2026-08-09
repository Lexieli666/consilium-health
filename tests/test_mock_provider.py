"""MockProvider is the offline half of the LLM seam, so its selection rules and its streaming path
are pinned here: a mock that quietly invents replies turns every test above it into a test that
passes for the wrong reason.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from consilium.llm import (
    Delta,
    LLMResponse,
    Message,
    MockProvider,
    ScriptedResponse,
    ScriptExhaustedError,
    Usage,
)
from consilium.llm.mock import ScriptedToolCall
from consilium.trace import LLMCallEvent, MemorySink, Tracer

QUESTION = [Message(role="user", content="What is a normal blood pressure reading?")]


async def test_chat_returns_the_scripted_answer(mock_provider: MockProvider) -> None:
    response = await mock_provider.chat(QUESTION)

    assert response.content == "A scripted answer about blood pressure."
    assert response.stop_reason == "stop"
    assert response.provider == "mock"
    assert response.usage.total_tokens > 0


async def test_chat_emits_one_llm_call_event_with_token_accounting(
    tracer: Tracer, memory_sink: MemorySink, mock_provider: MockProvider
) -> None:
    tools = [{"type": "function", "function": {"name": "search_knowledge", "parameters": {}}}]

    await mock_provider.chat(QUESTION, tools=tools, tracer=tracer, caller="agent:consultation")

    events = memory_sink.of_type(LLMCallEvent)
    assert len(events) == 1
    event = events[0]
    assert event.caller == "agent:consultation"
    assert event.provider == "mock"
    assert event.tools_offered == ["search_knowledge"]
    assert event.prompt_tokens > 0
    assert event.completion_tokens > 0


async def test_no_trace_event_without_a_tracer(mock_provider: MockProvider) -> None:
    """A provider used outside a turn (a smoke script) must not require a tracer."""
    response = await mock_provider.chat(QUESTION)
    assert isinstance(response, LLMResponse)


async def test_when_clause_selects_a_response_out_of_order() -> None:
    provider = MockProvider(
        [
            ScriptedResponse(content="generic fallback"),
            ScriptedResponse(when="ICD-10", content="I10 is essential hypertension."),
        ]
    )

    matched = await provider.chat([Message(role="user", content="What is the ICD-10 code?")])
    unmatched = await provider.chat([Message(role="user", content="Anything else?")])

    assert matched.content == "I10 is essential hypertension."
    assert unmatched.content == "generic fallback"


async def test_tool_calls_are_returned_with_ids_and_a_tool_calls_stop_reason() -> None:
    provider = MockProvider(
        [
            ScriptedResponse(
                tool_calls=[
                    ScriptedToolCall(name="assess_risk", arguments={"symptoms": "chest pain"})
                ]
            )
        ]
    )

    response = await provider.chat(QUESTION)

    assert response.stop_reason == "tool_calls"
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].name == "assess_risk"
    assert response.tool_calls[0].arguments == {"symptoms": "chest pain"}
    assert response.tool_calls[0].id


async def test_exhausted_script_raises_rather_than_inventing_a_reply(
    mock_provider: MockProvider,
) -> None:
    await mock_provider.chat(QUESTION)
    await mock_provider.chat(QUESTION)

    assert mock_provider.remaining == 0
    with pytest.raises(ScriptExhaustedError):
        await mock_provider.chat(QUESTION)


async def test_reset_makes_the_script_replayable(mock_provider: MockProvider) -> None:
    await mock_provider.chat(QUESTION)
    mock_provider.reset()

    assert mock_provider.remaining == 2
    assert (await mock_provider.chat(QUESTION)).content == "A scripted answer about blood pressure."


async def test_stream_chat_reassembles_to_the_same_answer() -> None:
    answer = "Blood pressure below 120 over 80 is generally considered normal for adults."
    provider = MockProvider([ScriptedResponse(content=answer)], chunk_size=7)

    deltas: list[Delta] = [delta async for delta in provider.stream_chat(QUESTION)]

    assert "".join(delta.content or "" for delta in deltas) == answer
    assert len(deltas) > 2, "a single-delta stream would not exercise the SSE assembly path"
    assert deltas[-1].content is None
    assert deltas[-1].stop_reason == "stop"
    assert deltas[-1].usage is not None


async def test_stream_chat_emits_exactly_one_llm_call_event(
    tracer: Tracer, memory_sink: MemorySink
) -> None:
    provider = MockProvider([ScriptedResponse(content="a" * 100)], chunk_size=8)

    async for _ in provider.stream_chat(QUESTION, tracer=tracer, caller="agent:consultation"):
        pass

    assert len(memory_sink.of_type(LLMCallEvent)) == 1


async def test_scripted_usage_overrides_the_synthetic_estimate() -> None:
    provider = MockProvider(
        [ScriptedResponse(content="short", usage=Usage(prompt_tokens=999, completion_tokens=7))]
    )

    response = await provider.chat(QUESTION)

    assert response.usage.prompt_tokens == 999
    assert response.usage.completion_tokens == 7


def test_from_file_loads_a_yaml_script(tmp_path: Path) -> None:
    path = tmp_path / "script.yaml"
    path.write_text(
        "responses:\n"
        "  - when: hypertension\n"
        "    content: Hypertension is elevated blood pressure.\n"
        "  - content: Fallback.\n",
        encoding="utf-8",
    )

    provider = MockProvider.from_file(path)

    assert provider.remaining == 2


def test_invalid_chunk_size_is_rejected() -> None:
    with pytest.raises(ValueError, match="chunk_size"):
        MockProvider([], chunk_size=0)
