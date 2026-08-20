"""The ReAct loop: the budget, the forced answer, and what is and is not counted as a tool call.

The loop is where the promise "the user always gets a response" is kept, and where the tool-call
distribution that `full_budget_6` exists to measure is produced. Both are pinned here.
"""

from __future__ import annotations

import pytest

from consilium.agents.loop import (
    BUDGET_EXHAUSTED_OBSERVATION,
    EMPTY_ANSWER_FALLBACK,
    ReActLoop,
)
from consilium.llm import MockProvider, ScriptedResponse
from consilium.llm.mock import ScriptedToolCall
from consilium.skills import SkillContext, SkillRegistry
from consilium.trace import LLMCallEvent, MemorySink, ToolCallEvent

PERMITTED = ("search_knowledge", "recommend_lifestyle")
SYSTEM = "You are a test specialist."


def _search(query: str = "hypertension") -> ScriptedToolCall:
    return ScriptedToolCall(name="search_knowledge", arguments={"query": query})


def _loop(
    registry: SkillRegistry, responses: list[ScriptedResponse], **kwargs: int
) -> tuple[ReActLoop, MockProvider]:
    provider = MockProvider(responses)
    return ReActLoop(provider=provider, registry=registry, **kwargs), provider


async def test_a_direct_answer_uses_one_call_and_no_tools(
    registry: SkillRegistry, skill_context: SkillContext, memory_sink: MemorySink
) -> None:
    loop, _ = _loop(registry, [ScriptedResponse(content="Blood pressure is a measurement.")])

    result = await loop.run(
        system_prompt=SYSTEM, question="what is bp", ctx=skill_context, permitted=PERMITTED
    )

    assert result.answer == "Blood pressure is a measurement."
    assert result.iterations == 1
    assert result.tool_calls_used == 0
    assert result.forced is False
    assert [event.caller for event in memory_sink.of_type(LLMCallEvent)] == ["agent:consultation"]


async def test_a_tool_call_is_executed_observed_and_answered(
    registry: SkillRegistry, skill_context: SkillContext, memory_sink: MemorySink
) -> None:
    loop, _ = _loop(
        registry,
        [
            ScriptedResponse(tool_calls=[_search()]),
            ScriptedResponse(content="Grounded answer."),
        ],
    )

    result = await loop.run(
        system_prompt=SYSTEM,
        question="what is hypertension",
        ctx=skill_context,
        permitted=PERMITTED,
    )

    assert result.answer == "Grounded answer."
    assert result.tool_calls_used == 1
    assert result.sources  # the skill's doc_ids reached the loop result
    assert [event.skill for event in memory_sink.of_type(ToolCallEvent)] == ["search_knowledge"]
    # The transcript keeps the assistant's request and the tool's observation, in that order.
    roles = [message.role for message in result.messages]
    assert roles == ["system", "user", "assistant", "tool"]


async def test_the_tool_budget_is_enforced_by_the_loop_not_the_prompt(
    registry: SkillRegistry, skill_context: SkillContext, memory_sink: MemorySink
) -> None:
    """The model asks for a third tool call; it never happens and the answer is forced."""
    loop, _ = _loop(
        registry,
        [
            ScriptedResponse(tool_calls=[_search("one")]),
            ScriptedResponse(tool_calls=[_search("two")]),
            ScriptedResponse(content="Answer from what was gathered."),
        ],
        max_tool_calls=2,
        max_iterations=5,
    )

    result = await loop.run(
        system_prompt=SYSTEM, question="q", ctx=skill_context, permitted=PERMITTED
    )

    assert result.tool_calls_used == 2
    assert result.forced is True
    assert result.answer == "Answer from what was gathered."
    callers = [event.caller for event in memory_sink.of_type(LLMCallEvent)]
    assert callers == ["agent:consultation", "agent:consultation", "forced_answer"]


async def test_calls_requested_beyond_the_budget_are_refused_without_a_tool_call_event(
    registry: SkillRegistry, skill_context: SkillContext, memory_sink: MemorySink
) -> None:
    """Counting a refused call would inflate the distribution `full_budget_6` exists to measure."""
    loop, _ = _loop(
        registry,
        [
            ScriptedResponse(tool_calls=[_search("a"), _search("b"), _search("c")]),
            ScriptedResponse(content="Done."),
        ],
        max_tool_calls=1,
    )

    result = await loop.run(
        system_prompt=SYSTEM, question="q", ctx=skill_context, permitted=PERMITTED
    )

    assert result.tool_calls_used == 1
    assert len(memory_sink.of_type(ToolCallEvent)) == 1
    refused = [m for m in result.messages if m.content == BUDGET_EXHAUSTED_OBSERVATION]
    assert len(refused) == 2


async def test_every_requested_call_still_gets_a_tool_message(
    registry: SkillRegistry, skill_context: SkillContext
) -> None:
    """A tool result with no matching request is rejected by every provider that supports tools."""
    loop, _ = _loop(
        registry,
        [
            ScriptedResponse(tool_calls=[_search("a"), _search("b")]),
            ScriptedResponse(content="Done."),
        ],
        max_tool_calls=1,
    )

    result = await loop.run(
        system_prompt=SYSTEM, question="q", ctx=skill_context, permitted=PERMITTED
    )

    requested = sum(len(m.tool_calls) for m in result.messages if m.role == "assistant")
    observed = sum(1 for m in result.messages if m.role == "tool")
    assert requested == observed == 2


async def test_max_iterations_stops_a_model_that_never_answers(
    registry: SkillRegistry, skill_context: SkillContext, memory_sink: MemorySink
) -> None:
    """The guard `max_tool_calls` cannot provide: a model looping without exhausting the budget."""
    loop, _ = _loop(
        registry,
        [ScriptedResponse(tool_calls=[_search()]) for _ in range(2)]
        + [ScriptedResponse(content="Forced.")],
        max_iterations=2,
        max_tool_calls=10,
    )

    result = await loop.run(
        system_prompt=SYSTEM, question="q", ctx=skill_context, permitted=PERMITTED
    )

    assert result.iterations == 3  # two budgeted iterations plus the forced call
    assert result.forced is True
    assert result.answer == "Forced."
    assert memory_sink.of_type(LLMCallEvent)[-1].caller == "forced_answer"


async def test_zero_tool_budget_never_offers_tools_and_is_not_called_forced(
    registry: SkillRegistry, skill_context: SkillContext, memory_sink: MemorySink
) -> None:
    """`baseline_llm` has max_tool_calls=0 by configuration; nothing was exhausted."""
    loop, _ = _loop(registry, [ScriptedResponse(content="Ungrounded.")], max_tool_calls=0)

    result = await loop.run(
        system_prompt=SYSTEM, question="q", ctx=skill_context, permitted=PERMITTED
    )

    assert result.forced is False
    assert result.tool_calls_used == 0
    (event,) = memory_sink.of_type(LLMCallEvent)
    assert event.caller == "agent:consultation"
    assert event.tools_offered == []


async def test_a_skill_outside_the_permitted_list_is_refused_not_executed(
    registry: SkillRegistry, skill_context: SkillContext, memory_sink: MemorySink
) -> None:
    loop, _ = _loop(
        registry,
        [
            ScriptedResponse(
                tool_calls=[ScriptedToolCall(name="deep_research", arguments={"question": "q"})]
            ),
            ScriptedResponse(content="Answered without it."),
        ],
    )

    result = await loop.run(
        system_prompt=SYSTEM, question="q", ctx=skill_context, permitted=PERMITTED
    )

    (attempt,) = result.tool_results
    assert attempt.ok is False
    assert attempt.error is not None and "not permitted" in attempt.error
    assert result.answer == "Answered without it."
    # No `retrieval` event: the refusal happened before the skill ran.
    assert memory_sink.of_type(ToolCallEvent) == []


async def test_an_empty_forced_answer_falls_back_to_a_fixed_sentence(
    registry: SkillRegistry, skill_context: SkillContext
) -> None:
    """The user always gets a response; an empty string is not a response."""
    loop, _ = _loop(
        registry,
        [ScriptedResponse(content=None), ScriptedResponse(content=None)],
        max_iterations=1,
    )

    result = await loop.run(
        system_prompt=SYSTEM, question="q", ctx=skill_context, permitted=PERMITTED
    )

    assert result.answer == EMPTY_ANSWER_FALLBACK
    assert result.forced is True


async def test_tool_calls_returned_when_none_were_offered_are_ignored(
    registry: SkillRegistry, skill_context: SkillContext
) -> None:
    """A provider bug must not cost the turn: the loop takes the content and stops."""
    loop, _ = _loop(
        registry,
        [ScriptedResponse(content="Answer anyway.", tool_calls=[_search()])],
        max_tool_calls=0,
    )

    result = await loop.run(
        system_prompt=SYSTEM, question="q", ctx=skill_context, permitted=PERMITTED
    )

    assert result.answer == "Answer anyway."
    assert result.tool_calls_used == 0
    assert result.iterations == 1


async def test_history_is_placed_between_the_system_prompt_and_the_question(
    registry: SkillRegistry, skill_context: SkillContext
) -> None:
    from consilium.llm import Message

    loop, _ = _loop(registry, [ScriptedResponse(content="ok")])
    history = [Message(role="user", content="earlier"), Message(role="assistant", content="reply")]

    result = await loop.run(
        system_prompt=SYSTEM,
        question="now",
        ctx=skill_context,
        permitted=PERMITTED,
        history=history,
    )

    assert [m.content for m in result.messages[:4]] == [SYSTEM, "earlier", "reply", "now"]


async def test_per_call_budget_overrides_the_constructor_default(
    registry: SkillRegistry, skill_context: SkillContext
) -> None:
    """`full_budget_6` differs from `full` in exactly this number, at call time."""
    loop, _ = _loop(
        registry,
        [
            ScriptedResponse(tool_calls=[_search("a")]),
            ScriptedResponse(tool_calls=[_search("b")]),
            ScriptedResponse(tool_calls=[_search("c")]),
            ScriptedResponse(content="Done."),
        ],
        max_tool_calls=1,
        max_iterations=6,
    )

    result = await loop.run(
        system_prompt=SYSTEM,
        question="q",
        ctx=skill_context,
        permitted=PERMITTED,
        max_tool_calls=3,
    )

    assert result.tool_calls_used == 3


@pytest.mark.parametrize(("iterations", "tool_calls"), [(0, 2), (3, -1)])
def test_nonsensical_budgets_are_rejected_at_construction(
    registry: SkillRegistry, iterations: int, tool_calls: int
) -> None:
    with pytest.raises(ValueError, match="must be >="):
        ReActLoop(
            provider=MockProvider([]),
            registry=registry,
            max_iterations=iterations,
            max_tool_calls=tool_calls,
        )
