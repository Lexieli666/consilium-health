"""The three specialists, and the claim that they differ only in prompt and policy.

If that claim is true, the tests for it are structural: the classes have no tool wiring, the
permitted lists come from the file, and every agent shares the same non-negotiable rules.
"""

from __future__ import annotations

import pytest

from consilium.agents import (
    AGENT_TYPES,
    DEFAULT_AGENT,
    SHARED_RULES,
    BaseAgent,
    ConsultationAgent,
    DiagnosticAgent,
    ResearchAgent,
)
from consilium.llm import MockProvider, ScriptedResponse
from consilium.llm.mock import ScriptedToolCall
from consilium.safety import Policy, PolicyError
from consilium.safety.policy import AgentPolicy
from consilium.skills import SKILL_NAMES, SkillContext, SkillRegistry
from consilium.trace import LLMCallEvent, MemorySink, ToolCallEvent


def _agent(
    cls: type[BaseAgent],
    registry: SkillRegistry,
    policy: Policy,
    responses: list[ScriptedResponse],
) -> BaseAgent:
    return cls(provider=MockProvider(responses), registry=registry, policy=policy)


def test_the_three_agent_names_match_the_policy_file(policy: Policy) -> None:
    assert set(AGENT_TYPES) == set(policy)
    assert ConsultationAgent.name == DEFAULT_AGENT


def test_every_agent_name_is_a_legal_trace_caller() -> None:
    import re

    from consilium.trace import CALLER_PATTERN

    for name in AGENT_TYPES:
        assert re.match(CALLER_PATTERN, f"agent:{name}"), name


def test_permitted_skills_come_from_the_policy_not_the_class(
    registry: SkillRegistry, policy: Policy
) -> None:
    for name, cls in AGENT_TYPES.items():
        agent = _agent(cls, registry, policy, [])
        assert agent.permitted == policy.permitted_skills(name)
        assert set(agent.registry.names) == set(agent.permitted)


def test_every_permitted_skill_is_one_of_the_seven(policy: Policy) -> None:
    for name in policy:
        assert set(policy.permitted_skills(name)) <= set(SKILL_NAMES), name


def test_all_three_agents_can_reach_the_unfiltered_search(policy: Policy) -> None:
    """A routing mistake should cost answer quality, not the whole turn."""
    for name in policy:
        assert "search_knowledge" in policy.permitted_skills(name)


def test_the_specialists_partition_their_own_tools(policy: Policy) -> None:
    """Beyond the shared search, no two specialists offer the same skill."""
    owned = {name: set(policy.permitted_skills(name)) - {"search_knowledge"} for name in policy}
    for left in owned:
        for right in owned:
            if left != right:
                assert not owned[left] & owned[right], (left, right)


def test_a_policy_naming_an_unknown_skill_cannot_build_an_agent(
    registry: SkillRegistry,
) -> None:
    """A typo in the policy must be loud, not one silently missing tool."""
    broken = Policy(
        {
            "consultation": AgentPolicy(
                description="d", permitted_skills=("search_knowledge", "not_a_skill")
            )
        }
    )
    with pytest.raises(KeyError, match="unknown skill"):
        ConsultationAgent(provider=MockProvider([]), registry=registry, policy=broken)


def test_an_agent_with_no_policy_entry_cannot_be_built(registry: SkillRegistry) -> None:
    only_consultation = Policy(
        {"consultation": AgentPolicy(description="d", permitted_skills=("search_knowledge",))}
    )
    with pytest.raises(PolicyError, match="no policy for agent 'diagnostic'"):
        DiagnosticAgent(provider=MockProvider([]), registry=registry, policy=only_consultation)


@pytest.mark.parametrize("cls", [ConsultationAgent, DiagnosticAgent, ResearchAgent])
def test_every_prompt_carries_the_shared_rules(cls: type[BaseAgent]) -> None:
    assert SHARED_RULES in cls.system_prompt
    assert cls.system_prompt != SHARED_RULES  # and adds a specialty on top


@pytest.mark.parametrize("cls", [ConsultationAgent, DiagnosticAgent, ResearchAgent])
def test_no_agent_class_hard_codes_a_tool_list(cls: type[BaseAgent]) -> None:
    """The specialization lives in the prompt and the policy file, nowhere else."""
    declared = {name for name in vars(cls) if not name.startswith("__")}
    assert declared == {"name", "system_prompt"}, declared


async def test_an_agent_answers_and_stamps_its_own_name_on_every_event(
    registry: SkillRegistry, policy: Policy, skill_context: SkillContext, memory_sink: MemorySink
) -> None:
    agent = _agent(
        DiagnosticAgent,
        registry,
        policy,
        [
            ScriptedResponse(
                tool_calls=[
                    ScriptedToolCall(name="assess_risk", arguments={"symptoms": "chest pain"})
                ]
            ),
            ScriptedResponse(content="Call emergency services now."),
        ],
    )
    ctx = agent.for_turn(skill_context)

    result = await agent.answer("I have chest pain", ctx=ctx)

    assert result.agent == "diagnostic"
    assert result.answer == "Call emergency services now."
    assert result.sources
    assert {event.agent for event in memory_sink.of_type(ToolCallEvent)} == {"diagnostic"}
    assert {event.caller for event in memory_sink.of_type(LLMCallEvent)} == {"agent:diagnostic"}


async def test_a_planner_objective_is_prepended_and_leaves_the_prompt_alone(
    registry: SkillRegistry, policy: Policy, skill_context: SkillContext
) -> None:
    agent = _agent(ResearchAgent, registry, policy, [ScriptedResponse(content="ok")])

    result = await agent.answer(
        "what is the target",
        ctx=agent.for_turn(skill_context),
        objective="Report only what guidance says about thresholds.",
    )

    assert result.answer == "ok"
    assert agent.system_prompt == ResearchAgent.system_prompt  # unchanged by the objective


def test_for_turn_relabels_the_context_without_copying_the_substrate(
    registry: SkillRegistry, policy: Policy, skill_context: SkillContext
) -> None:
    agent = _agent(ResearchAgent, registry, policy, [])
    relabelled = agent.for_turn(skill_context)

    assert relabelled.agent == "research"
    assert relabelled.retriever is skill_context.retriever
    assert relabelled.red_flags is skill_context.red_flags
    assert relabelled.tracer is skill_context.tracer
    # Already-correct contexts are returned unchanged rather than needlessly rebuilt.
    assert agent.for_turn(relabelled) is relabelled
