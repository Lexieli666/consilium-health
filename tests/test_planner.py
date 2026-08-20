"""The planner: one call, one validated plan, and a counted fallback for every way it can fail.

The fallback tests carry the weight. Planner fallback rate is a reported metric and routing accuracy
is reported both including and excluding fallback turns, so every path that produces a fallback has
to actually set the flag -- a silent recovery would show up as a routing success.
"""

from __future__ import annotations

import json

import pytest

from consilium.llm import MockProvider, ScriptedResponse
from consilium.router import FALLBACK_AGENT, Planner, extract_json_object
from consilium.safety import Policy
from consilium.trace import LLMCallEvent, MemorySink, Tracer
from tests.stubs import FailingProvider, RecordingProvider

PLAN = {
    "subtasks": [
        {"agent": "diagnostic", "objective": "Assess urgency.", "why": "A symptom."},
        {"agent": "research", "objective": "Report the guideline.", "why": "A guideline question."},
    ]
}


def _planner(policy: Policy, *contents: str) -> Planner:
    return Planner(
        provider=MockProvider([ScriptedResponse(content=content) for content in contents]),
        policy=policy,
    )


async def test_a_valid_plan_is_parsed_and_numbered(policy: Policy) -> None:
    subtasks, fallback = await _planner(policy, json.dumps(PLAN)).plan("q")

    assert fallback is False
    assert [s.agent for s in subtasks] == ["diagnostic", "research"]
    assert [s.subtask_id for s in subtasks] == ["1-diagnostic", "2-research"]
    assert subtasks[0].objective == "Assess urgency."


async def test_subtask_ids_are_deterministic_across_runs(policy: Policy) -> None:
    """Two traces of one plan must be comparable; a uuid would make them textually different."""
    first, _ = await _planner(policy, json.dumps(PLAN)).plan("q")
    second, _ = await _planner(policy, json.dumps(PLAN)).plan("q")

    assert [s.subtask_id for s in first] == [s.subtask_id for s in second]


async def test_the_planner_call_is_traced_as_the_planner(
    policy: Policy, memory_sink: MemorySink
) -> None:
    """Planner tokens are part of what the multi-agent architecture costs, so they are counted."""
    tracer = Tracer(session_id="s", turn_index=0, sink=memory_sink)

    await _planner(policy, json.dumps(PLAN)).plan("q", tracer=tracer)

    (event,) = memory_sink.of_type(LLMCallEvent)
    assert event.caller == "planner"
    assert event.tools_offered == []


async def test_json_wrapped_in_prose_and_fences_is_still_parsed(policy: Policy) -> None:
    wrapped = f"Sure, here is the plan:\n```json\n{json.dumps(PLAN)}\n```\nHope that helps."

    subtasks, fallback = await _planner(policy, wrapped).plan("q")

    assert fallback is False
    assert len(subtasks) == 2


@pytest.mark.parametrize(
    ("reply", "reason"),
    [
        ("", "empty reply"),
        ("I do not think I should answer that.", "no JSON at all"),
        ('{"subtasks": [', "truncated JSON"),
        ('{"subtasks": []}', "an empty plan"),
        ('{"subtasks": [{"agent": "cardiology", "objective": "x", "why": "y"}]}', "unknown agent"),
        ('{"subtasks": [{"agent": "diagnostic"}]}', "a missing objective"),
        ('{"plans": [{"agent": "diagnostic", "objective": "x", "why": "y"}]}', "the wrong key"),
        (
            '{"subtasks": [{"agent": "diagnostic", "objective": "x", "why": "y", "extra": 1}]}',
            "an invented field",
        ),
    ],
)
async def test_every_unusable_reply_falls_back_and_is_counted(
    policy: Policy, reply: str, reason: str
) -> None:
    subtasks, fallback = await _planner(policy, reply).plan("q")

    assert fallback is True, reason
    assert [s.agent for s in subtasks] == [FALLBACK_AGENT]
    assert subtasks[0].objective


async def test_an_unknown_agent_invalidates_the_plan_rather_than_being_dropped(
    policy: Policy,
) -> None:
    """Dropping it would record a narrower route as a successful one."""
    reply = json.dumps(
        {
            "subtasks": [
                {"agent": "diagnostic", "objective": "x", "why": "y"},
                {"agent": "cardiology", "objective": "x", "why": "y"},
            ]
        }
    )

    subtasks, fallback = await _planner(policy, reply).plan("q")

    assert fallback is True
    assert [s.agent for s in subtasks] == [FALLBACK_AGENT]


async def test_a_repeated_agent_is_collapsed_to_one_subtask(policy: Policy) -> None:
    """Two subtasks for one specialist buy one perspective twice, and break the routing metric."""
    reply = json.dumps(
        {
            "subtasks": [
                {"agent": "research", "objective": "first", "why": "a"},
                {"agent": "research", "objective": "second", "why": "b"},
            ]
        }
    )

    subtasks, fallback = await _planner(policy, reply).plan("q")

    assert fallback is False
    assert [s.agent for s in subtasks] == ["research"]
    assert subtasks[0].objective == "first"


async def test_a_provider_outage_falls_back_instead_of_ending_the_turn(policy: Policy) -> None:
    provider = FailingProvider()
    planner = Planner(provider=provider, policy=policy)

    subtasks, fallback = await planner.plan("q")

    assert fallback is True
    assert [s.agent for s in subtasks] == [FALLBACK_AGENT]
    assert provider.calls == 1


def test_the_capability_block_is_read_from_the_policy(policy: Policy) -> None:
    """One description, so the planner cannot be told an agent does what the policy forbids."""
    block = _planner(policy).capability_block()

    for name in policy:
        assert policy.description(name) in block


def test_the_prompt_tells_the_planner_to_assign_the_fewest_agents(policy: Policy) -> None:
    system = _planner(policy).messages("q")[0].content or ""

    assert "FEWEST" in system
    assert "Never assign the same specialist twice." in system
    assert '{"subtasks"' in system  # the output schema is shown, not described


async def test_the_planner_is_never_offered_tools(policy: Policy) -> None:
    """It decides who answers; it must not start answering."""
    provider = RecordingProvider(content=json.dumps(PLAN))

    _, fallback = await Planner(provider=provider, policy=policy).plan("q")

    assert fallback is False
    assert provider.tools_offered == [None]
    assert provider.callers == ["planner"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"a": 1}', '{"a": 1}'),
        ('prefix {"a": {"b": 2}} suffix', '{"a": {"b": 2}}'),
        ('{"a": "}"}', '{"a": "}"}'),
        ('{"a": "\\\\"}"}', '{"a": "\\\\"}'),
        ("no braces here", None),
        ('{"unbalanced": ', None),
    ],
)
def test_the_json_extractor_handles_braces_inside_strings(text: str, expected: str | None) -> None:
    assert extract_json_object(text) == expected
