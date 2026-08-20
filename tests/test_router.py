"""The router: the fast path, the parallel path, the deadline, and partial failure.

Partial-failure behaviour is the part worth testing hardest. If a parallel turn failed whenever any
worker failed, the parallel path would be strictly less reliable than the single one -- an odd thing
to then measure as an improvement.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from consilium.agents.base import AgentResult
from consilium.config import RunConfig
from consilium.llm import MockProvider, ScriptedResponse
from consilium.router import Router, RouteResult, Synthesizer
from consilium.router.planner import Planner
from consilium.safety import Policy
from consilium.skills.base import SkillContext
from consilium.trace import BlackboardEvent, MemorySink, RouteEvent, Tracer


class _StubAgent:
    """A `Worker` that answers instantly, slowly, or by raising -- without a provider.

    Implements the router's `Worker` protocol directly. Subclassing `BaseAgent` would drag a
    provider, a registry and a policy into a test about dispatch and deadlines.
    """

    def __init__(self, name: str, *, answer: str = "", delay: float = 0.0, error: str = "") -> None:
        self.name = name
        self._answer = answer or f"{name} says something."
        self._delay = delay
        self._error = error
        self.calls: list[str] = []

    async def answer(
        self, question: str, *, ctx: SkillContext, objective: str | None = None
    ) -> AgentResult:
        self.calls.append(objective or question)
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error:
            raise RuntimeError(self._error)
        return AgentResult(
            agent=self.name,
            answer=self._answer,
            sources=(f"doc-{self.name}",),
            tool_results=(),
            iterations=1,
            forced=False,
        )

    def for_turn(self, ctx: SkillContext) -> SkillContext:
        return ctx


def _plan(*agents: str) -> str:
    return json.dumps(
        {
            "subtasks": [
                {"agent": name, "objective": f"the {name} part", "why": "test"} for name in agents
            ]
        }
    )


def _router(
    policy: Policy,
    *,
    agents: dict[str, _StubAgent],
    planner_reply: str = "",
    merge_reply: str = "Merged answer.",
    config: RunConfig | None = None,
    deadline_seconds: float = 90.0,
) -> Router:
    return Router(
        planner=Planner(
            provider=MockProvider([ScriptedResponse(content=planner_reply)]), policy=policy
        ),
        synthesizer=Synthesizer(provider=MockProvider([ScriptedResponse(content=merge_reply)])),
        agent_factory=lambda name: agents[name],
        context_factory=lambda name: SkillContext(agent=name),
        config=config or RunConfig(name="full"),
        deadline_seconds=deadline_seconds,
    )


async def test_one_subtask_takes_the_fast_path_and_is_not_merged(
    policy: Policy, memory_sink: MemorySink
) -> None:
    agents = {"consultation": _StubAgent("consultation", answer="A direct answer.")}
    router = _router(policy, agents=agents, planner_reply=_plan("consultation"))
    tracer = Tracer(session_id="s", turn_index=0, sink=memory_sink)

    result = await router.handle("q", tracer=tracer)

    assert result.mode == "single"
    assert result.answer == "A direct answer."
    assert result.agents == ("consultation",)
    (route,) = memory_sink.of_type(RouteEvent)
    assert route.mode == "single"
    assert route.fallback is False
    assert [s.subtask_id for s in route.subtasks] == ["1-consultation"]


async def test_two_subtasks_run_on_the_parallel_path_and_are_merged(
    policy: Policy,
    memory_sink: MemorySink,
) -> None:
    agents = {
        "diagnostic": _StubAgent("diagnostic"),
        "research": _StubAgent("research"),
    }
    router = _router(policy, agents=agents, planner_reply=_plan("diagnostic", "research"))
    tracer = Tracer(session_id="s", turn_index=0, sink=memory_sink)

    result = await router.handle("q", tracer=tracer)

    assert result.mode == "parallel"
    assert result.answer == "Merged answer."
    assert result.agents == ("diagnostic", "research")
    assert result.sources == ("doc-diagnostic", "doc-research")
    (route,) = memory_sink.of_type(RouteEvent)
    assert route.mode == "parallel"


async def test_each_worker_receives_only_its_own_objective(policy: Policy) -> None:
    agents = {"diagnostic": _StubAgent("diagnostic"), "research": _StubAgent("research")}
    router = _router(policy, agents=agents, planner_reply=_plan("diagnostic", "research"))

    await router.handle("q")

    assert agents["diagnostic"].calls == ["the diagnostic part"]
    assert agents["research"].calls == ["the research part"]


async def test_the_parallel_workers_actually_overlap(policy: Policy) -> None:
    """Three workers under `gather` must not take turns; that is the whole point of the path."""
    agents = {
        "diagnostic": _StubAgent("diagnostic", delay=0.05),
        "consultation": _StubAgent("consultation", delay=0.05),
        "research": _StubAgent("research", delay=0.05),
    }
    router = _router(
        policy,
        agents=agents,
        planner_reply=_plan("diagnostic", "consultation", "research"),
    )

    loop = asyncio.get_running_loop()
    start = loop.time()
    await router.handle("q")
    elapsed = loop.time() - start

    assert elapsed < 0.12  # three serialized 50ms workers would take 0.15s


async def test_a_failing_worker_is_recorded_and_the_answer_is_synthesized_from_the_rest(
    policy: Policy,
    memory_sink: MemorySink,
) -> None:
    agents = {
        "diagnostic": _StubAgent("diagnostic"),
        "research": _StubAgent("research", error="upstream is down"),
    }
    router = _router(policy, agents=agents, planner_reply=_plan("diagnostic", "research"))
    tracer = Tracer(session_id="s", turn_index=0, sink=memory_sink)

    result = await router.handle("q", tracer=tracer)

    assert result.missing == ("research",)
    assert "research" in result.answer  # the missing perspective is named in the delivered text
    statuses = [e.event for e in memory_sink.of_type(BlackboardEvent) if e.agent == "research"]
    assert statuses == ["assigned", "started", "failed"]


async def test_a_slow_worker_times_out_at_the_shared_deadline_without_losing_the_others(
    policy: Policy,
    memory_sink: MemorySink,
) -> None:
    agents = {
        "diagnostic": _StubAgent("diagnostic", answer="Fast enough."),
        "research": _StubAgent("research", delay=5.0),
    }
    router = _router(
        policy,
        agents=agents,
        planner_reply=_plan("diagnostic", "research"),
        deadline_seconds=0.05,
    )
    tracer = Tracer(session_id="s", turn_index=0, sink=memory_sink)

    result = await router.handle("q", tracer=tracer)

    assert result.missing == ("research",)
    assert result.sources == ("doc-diagnostic",)
    timeouts = [e for e in memory_sink.of_type(BlackboardEvent) if e.event == "timeout"]
    assert [event.agent for event in timeouts] == ["research"]


async def test_every_worker_failing_still_delivers_an_answer(policy: Policy) -> None:
    agents = {
        "diagnostic": _StubAgent("diagnostic", error="a"),
        "research": _StubAgent("research", error="b"),
    }
    router = _router(policy, agents=agents, planner_reply=_plan("diagnostic", "research"))

    result = await router.handle("q")

    assert result.answer
    assert result.missing == ("diagnostic", "research")
    assert result.sources == ()


async def test_a_planner_fallback_is_flagged_on_the_route_event(
    policy: Policy, memory_sink: MemorySink
) -> None:
    agents = {"consultation": _StubAgent("consultation")}
    router = _router(policy, agents=agents, planner_reply="not json")
    tracer = Tracer(session_id="s", turn_index=0, sink=memory_sink)

    result = await router.handle("q", tracer=tracer)

    assert result.fallback is True
    assert result.agents == ("consultation",)
    (route,) = memory_sink.of_type(RouteEvent)
    assert route.fallback is True
    assert route.mode == "single"


@pytest.mark.parametrize("router_mode", ["single", "none"])
async def test_the_unrouted_configs_emit_no_route_event(
    policy: Policy, memory_sink: MemorySink, router_mode: str
) -> None:
    """A route event here would let fallback rate compute to a confident 0% from no planner."""
    agents = {"consultation": _StubAgent("consultation", answer="Control answer.")}
    router = _router(
        policy,
        agents=agents,
        config=RunConfig.model_validate({"name": "control", "router": router_mode}),
    )
    tracer = Tracer(session_id="s", turn_index=0, sink=memory_sink)

    result = await router.handle("q", tracer=tracer)

    assert result.answer == "Control answer."
    assert result.mode == "single"
    assert result.fallback is False
    assert memory_sink.of_type(RouteEvent) == []


async def test_pinning_an_agent_uses_the_same_path_as_the_single_control(
    policy: Policy,
    memory_sink: MemorySink,
) -> None:
    agents = {"diagnostic": _StubAgent("diagnostic", answer="Pinned answer.")}
    router = _router(policy, agents=agents)
    tracer = Tracer(session_id="s", turn_index=0, sink=memory_sink)

    result = await router.handle("q", tracer=tracer, pinned_agent="diagnostic")

    assert result.agents == ("diagnostic",)
    assert result.answer == "Pinned answer."
    assert memory_sink.of_type(RouteEvent) == []


def test_a_nonpositive_deadline_is_refused(policy: Policy) -> None:
    with pytest.raises(ValueError, match="deadline_seconds must be positive"):
        _router(policy, agents={}, deadline_seconds=0)


def test_route_result_exposes_the_completed_agent_results(policy: Policy) -> None:
    empty = RouteResult(
        answer="a",
        sources=(),
        mode="single",
        agents=("consultation",),
        fallback=False,
        records=(),
        missing=(),
    )
    assert empty.results == ()
