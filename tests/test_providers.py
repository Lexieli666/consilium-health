"""The two real providers, tested where they can be tested offline: the translation layer.

The point of a second real provider is that the two APIs disagree in ways a single-provider
abstraction would have encoded as universal. Those disagreements are pure functions here, so they
are checked with no network, no key and no client: the system prompt is a parameter and not a
message, tool results are user-turn content blocks and not a role, and Anthropic's tool schema is
the OpenAI one unwrapped rather than a second derivation from the Pydantic models.

The client calls themselves are not exercised. A test that mocked `AsyncOpenAI` would assert that
the mock was called the way the test told it to be, which is not a fact about the provider.
"""

from __future__ import annotations

import json

import pytest

from consilium.config import Settings
from consilium.llm import MockProvider, ProviderError, make_provider
from consilium.llm.anthropic_provider import to_anthropic_messages, to_anthropic_tools
from consilium.llm.base import Message, ToolCall
from consilium.llm.factory import UNSCRIPTED_MOCK_ANSWER
from consilium.llm.openai_provider import parse_tool_arguments, to_openai_messages
from consilium.skills import SkillRegistry

CONVERSATION = [
    Message(role="system", content="You are a specialist."),
    Message(role="user", content="what is hypertension"),
    Message(
        role="assistant",
        content=None,
        tool_calls=[ToolCall(id="call_1", name="search_knowledge", arguments={"query": "htn"})],
    ),
    Message(role="tool", tool_call_id="call_1", name="search_knowledge", content='{"data": 1}'),
    Message(role="tool", tool_call_id="call_2", name="search_knowledge", content='{"data": 2}'),
]


def test_openai_keeps_the_system_message_and_the_tool_role() -> None:
    wire = to_openai_messages(CONVERSATION)

    assert wire[0] == {"role": "system", "content": "You are a specialist."}
    assert wire[2]["role"] == "assistant"
    assert wire[2]["content"] is None
    assert json.loads(wire[2]["tool_calls"][0]["function"]["arguments"]) == {"query": "htn"}
    assert wire[3]["role"] == "tool"
    assert wire[3]["tool_call_id"] == "call_1"
    assert len(wire) == len(CONVERSATION)


def test_anthropic_lifts_the_system_prompt_out_of_the_messages() -> None:
    system, wire = to_anthropic_messages(CONVERSATION)

    assert system == "You are a specialist."
    assert all(message["role"] != "system" for message in wire)


def test_anthropic_merges_consecutive_tool_results_into_one_user_turn() -> None:
    """Separate messages are accepted but read as several turns of user input, which is not true."""
    _, wire = to_anthropic_messages(CONVERSATION)

    tool_turn = wire[-1]
    assert tool_turn["role"] == "user"
    assert [block["tool_use_id"] for block in tool_turn["content"]] == ["call_1", "call_2"]
    assert all(block["type"] == "tool_result" for block in tool_turn["content"])


def test_anthropic_renders_tool_calls_as_assistant_content_blocks() -> None:
    _, wire = to_anthropic_messages(CONVERSATION)

    assistant = wire[1]
    assert assistant["role"] == "assistant"
    (block,) = assistant["content"]
    assert block == {
        "type": "tool_use",
        "id": "call_1",
        "name": "search_knowledge",
        "input": {"query": "htn"},
    }


def test_anthropic_concatenates_multiple_system_messages() -> None:
    system, _ = to_anthropic_messages(
        [Message(role="system", content="one"), Message(role="system", content="two")]
    )
    assert system == "one\n\ntwo"


def test_anthropic_tools_are_the_openai_schemas_unwrapped(registry: SkillRegistry) -> None:
    """One derivation, two presentations. A second derivation could drift from the first."""
    openai_tools = registry.to_tool_schemas(["assess_risk"])
    (unwrapped,) = to_anthropic_tools(openai_tools)

    assert unwrapped["name"] == "assess_risk"
    assert unwrapped["description"] == openai_tools[0]["function"]["description"]
    assert unwrapped["input_schema"] == openai_tools[0]["function"]["parameters"]


def test_malformed_tool_arguments_degrade_instead_of_raising() -> None:
    """The model produced this string, so it can be invalid; the skill validator handles it."""
    parsed = parse_tool_arguments("{not json", call_id="call_9")

    assert parsed["__unparsed_arguments__"] == "{not json"
    assert parse_tool_arguments(None, call_id="c") == {}
    assert parse_tool_arguments('{"a": 1}', call_id="c") == {"a": 1}


def test_a_json_array_of_arguments_is_not_treated_as_a_mapping() -> None:
    assert parse_tool_arguments("[1, 2]", call_id="c") == {"__unparsed_arguments__": "[1, 2]"}


def test_the_factory_builds_an_unscripted_mock_by_default() -> None:
    provider = make_provider(Settings())

    assert isinstance(provider, MockProvider)
    assert provider.remaining == 1


async def test_the_unscripted_mock_answer_cannot_be_mistaken_for_a_real_one() -> None:
    provider = make_provider(Settings())
    response = await provider.chat([Message(role="user", content="anything")])

    assert response.content == UNSCRIPTED_MOCK_ANSWER
    assert "mock provider" in UNSCRIPTED_MOCK_ANSWER


@pytest.mark.parametrize("provider_name", ["openai", "anthropic"])
def test_a_real_provider_without_its_key_says_so_and_never_invents_one(provider_name: str) -> None:
    with pytest.raises(ProviderError, match="is not set"):
        make_provider(Settings.model_validate({"provider": provider_name}))


def test_the_factory_loads_a_script_for_the_mock(tmp_path: object) -> None:
    from pathlib import Path

    assert isinstance(tmp_path, Path)
    script = tmp_path / "script.yaml"
    script.write_text("responses:\n  - content: scripted\n", encoding="utf-8")

    provider = make_provider(Settings(), script=script)
    assert isinstance(provider, MockProvider)
    assert provider.remaining == 1
