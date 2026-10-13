"""ADK layer: the root agent, and the orchestrator the turn boundary calls.

``ORCHESTRATOR=adk`` swaps exactly one object in :func:`consilium.runtime.run_turn`: the
:class:`consilium.router.router.Router` becomes an :class:`AdkOrchestrator` with the same
``handle()`` signature and the same :class:`RouteResult` out.  The red-flag assessment, the output
validator, the repair, working memory and the ``turn`` event are the same code on both paths, so a
difference between the two runs in docs/gcp-migration-eval.md is a difference in the routing layer
and the model, and nothing else.

**One root agent, delegating to five sub-agents.**  :class:`ConsiliumRootAgent` is an ADK
``BaseAgent`` whose ``sub_agents`` are the planner, the three specialists and the synthesizer
(``consilium/adk/agents.py``).  Its ``_run_async_impl`` is the delegation: run the planner, parse
its plan with the legacy :meth:`Planner.parse` (so an unusable plan is the same counted fallback),
run the planned specialists concurrently under the shared deadline on the same
:class:`Blackboard`, and run the synthesizer only when two or more completed -- the legacy
``Router``'s rules, now driving ADK agents.  Each sub-agent runs in its own nested ``Runner`` and
in-memory session, the mechanism ADK's own ``AgentTool`` uses: it is what gives each specialist
its own objective and an isolated transcript, which a shared session with branch filtering does
not give cleanly.

**Why the plan is parsed by code rather than left to ``transfer_to_agent``.**  LLM-controlled
transfer hands the conversation to *one* sub-agent.  The architecture under evaluation fans out to
up to three concurrently and merges by fixed precedence, and routing accuracy is a reported metric
computed from the ``route`` event -- which needs an explicit plan to record.  So the planner plans
and the root dispatches, which is also the legacy behaviour and is the only way the two runs stay
comparable.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass
from typing import override

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.adk.models.base_llm import BaseLlm
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import PrivateAttr

from consilium.adk.agents import (
    WorkerScratch,
    _NoProvider,
    build_planner,
    build_specialist,
    build_synthesizer,
)
from consilium.agents.base import AgentResult
from consilium.agents.loop import EMPTY_ANSWER_FALLBACK
from consilium.config import RunConfig
from consilium.llm.base import Message
from consilium.log import get_logger
from consilium.router.blackboard import Blackboard, SubtaskHandle, SubtaskRecord
from consilium.router.plan import Subtask
from consilium.router.planner import FALLBACK_AGENT, FALLBACK_OBJECTIVE, Planner
from consilium.router.router import DEFAULT_DEADLINE_SECONDS, ContextFactory, RouteResult, _sources
from consilium.router.synthesizer import (
    ALL_FAILED_ANSWER,
    Synthesizer,
    concatenate,
    missing_note,
    order_by_precedence,
)
from consilium.safety.policy import Policy
from consilium.safety.validator import PolicyValidator
from consilium.skills.registry import SkillRegistry
from consilium.trace import RouteMode, Tracer, stopwatch

log = get_logger(__name__)

APP_NAME = "consilium-health"


@dataclass
class _TurnState:
    """Everything the root agent needs for one turn, and where it leaves the result.

    A plain dataclass held in a pydantic private attribute rather than declared as agent fields:
    ADK agents are pydantic models, and the result carries ``AgentResult`` and ``SkillResult``
    objects that have no business being validated or serialized by the agent tree.
    """

    question: str
    history: list[Message]
    deadline_seconds: float
    tracer: Tracer | None
    config: RunConfig
    policy: Policy
    worker_scratch: dict[str, WorkerScratch]
    pinned_agent: str | None = None
    result: RouteResult | None = None


class ConsiliumRootAgent(BaseAgent):
    """The root of the ADK agent tree: plans, delegates, merges."""

    _turn: _TurnState = PrivateAttr()

    @property
    def turn(self) -> _TurnState:
        return self._turn

    def _sub_agent(self, name: str) -> BaseAgent:
        for agent in self.sub_agents:
            if agent.name == name:
                return agent
        raise KeyError(name)

    @override
    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        result = await self._orchestrate()
        self._turn.result = result
        yield Event(
            invocation_id=ctx.invocation_id,
            author=self.name,
            content=types.Content(role="model", parts=[types.Part(text=result.answer)]),
        )

    async def _orchestrate(self) -> RouteResult:
        if self._turn.pinned_agent is not None or self._turn.config.router != "planner":
            return await self._unrouted(self._turn.pinned_agent or FALLBACK_AGENT)

        with stopwatch() as elapsed_ms:
            subtasks, fallback = await self._plan()
        planning_ms = elapsed_ms()

        mode: RouteMode = "parallel" if len(subtasks) > 1 else "single"
        if self._turn.tracer is not None:
            self._turn.tracer.route(
                mode=mode,
                agents=[subtask.agent for subtask in subtasks],
                subtasks=[subtask.to_trace() for subtask in subtasks],
                fallback=fallback,
                latency_ms=planning_ms,
            )

        board = Blackboard(tracer=self._turn.tracer)
        handles = await board.assign(subtasks)
        if mode == "parallel":
            deadline_at = asyncio.get_running_loop().time() + self._turn.deadline_seconds
            await asyncio.gather(
                *(self._run_worker(handle, deadline_at=deadline_at) for handle in handles),
                return_exceptions=True,
            )
        else:
            await self._run_worker(handles[0])

        completed = board.completed()
        missing = board.unfinished()
        answer = await self._merge(completed, missing)
        return RouteResult(
            answer=answer,
            sources=_sources(completed),
            mode=mode,
            agents=tuple(subtask.agent for subtask in subtasks),
            fallback=fallback,
            records=board.records,
            missing=tuple(sorted({record.subtask.agent for record in missing})),
        )

    async def _unrouted(self, agent: str) -> RouteResult:
        subtask = Subtask(
            subtask_id=f"1-{agent}",
            agent=agent,
            objective=FALLBACK_OBJECTIVE,
            why=f"router={self._turn.config.router}",
        )
        board = Blackboard(tracer=self._turn.tracer)
        (handle,) = await board.assign([subtask])
        await self._run_worker(handle)
        completed = board.completed()
        missing = board.unfinished()
        answer = (
            completed[0].result.answer if completed and completed[0].result else ALL_FAILED_ANSWER
        ) + missing_note(missing)
        return RouteResult(
            answer=answer,
            sources=_sources(completed),
            mode="single",
            agents=(agent,),
            fallback=False,
            records=board.records,
            missing=tuple(record.subtask.agent for record in missing),
        )

    async def _plan(self) -> tuple[list[Subtask], bool]:
        """One planner run, parsed by the legacy parser.  Never raises."""
        # parse() and fallback() only; the provider is never called.
        parser = Planner(provider=_NoProvider(), policy=self._turn.policy)
        try:
            reply = await _run_agent(self._sub_agent("planner"), self._turn.question)
        except Exception as exc:  # a model outage must not end the turn
            log.exception("adk.planner_failed")
            return parser.fallback(f"{type(exc).__name__}: {exc}"), True
        parsed = parser.parse(reply)
        if parsed is None:
            return parser.fallback("unusable plan"), True
        return parsed, False

    async def _run_worker(self, handle: SubtaskHandle, *, deadline_at: float | None = None) -> None:
        """Run one specialist, recording its outcome on the board whatever happens."""
        await handle.started()
        agent = self._sub_agent(handle.agent)
        scratch = self._turn.worker_scratch[handle.agent]
        prompt = f"{handle.subtask.objective}\n\nUser's question: {self._turn.question}"
        try:
            if deadline_at is None:
                answer = await _run_agent(agent, prompt, history=self._turn.history)
            else:
                async with asyncio.timeout_at(deadline_at):
                    answer = await _run_agent(agent, prompt, history=self._turn.history)
        except TimeoutError:
            log.warning("adk.worker_timeout", agent=handle.agent, subtask=handle.subtask.subtask_id)
            await handle.timed_out(f"exceeded the {self._turn.deadline_seconds:.0f}s turn deadline")
            return
        except Exception as exc:
            log.exception("adk.worker_failed", agent=handle.agent)
            await handle.failed(f"{type(exc).__name__}: {exc}")
            return

        if not answer:
            log.warning("adk.empty_answer", agent=handle.agent)
        await handle.completed(
            AgentResult(
                agent=handle.agent,
                answer=answer or EMPTY_ANSWER_FALLBACK,
                sources=scratch.sources,
                tool_results=tuple(scratch.tool_results),
                iterations=scratch.model_calls,
                forced=scratch.forced,
            )
        )

    async def _merge(
        self, completed: Sequence[SubtaskRecord], missing: Sequence[SubtaskRecord]
    ) -> str:
        """The delivered text: one worker's answer, or the synthesizer's merge of several."""
        if len(completed) <= 1 and not missing:
            if completed and completed[0].result:
                return completed[0].result.answer
            return ALL_FAILED_ANSWER

        ordered = order_by_precedence(completed)
        if not ordered:
            return ALL_FAILED_ANSWER + missing_note(missing)
        if len(ordered) == 1:
            return (ordered[0].result.answer if ordered[0].result else "") + missing_note(missing)

        # The legacy synthesizer's user message, verbatim: same sections, same precedence labels.
        synthesizer = Synthesizer(provider=_NoProvider())
        messages = synthesizer.messages(self._turn.question, ordered, missing)
        user_text = messages[-1].content or ""
        try:
            merged = (await _run_agent(self._sub_agent("synthesizer"), user_text)).strip()
        except Exception as exc:
            log.exception("adk.synthesizer_failed")
            return concatenate(ordered) + missing_note(missing, extra=f"merge failed: {exc}")
        if not merged:
            log.warning("adk.synthesizer_empty")
            return concatenate(ordered) + missing_note(missing)
        return merged + missing_note(missing)


async def _run_agent(agent: BaseAgent, text: str, *, history: Sequence[Message] = ()) -> str:
    """Run one sub-agent to completion in its own session and return its final text.

    The nested-``Runner`` mechanism ADK's ``AgentTool`` uses.  ``history`` -- the session's
    compacted prior turns -- is appended to the session as events before the new message, so a
    specialist sees the conversation the way the legacy loop shows it.
    """
    session_service = InMemorySessionService()
    runner = Runner(app_name=APP_NAME, agent=agent, session_service=session_service)
    session = await session_service.create_session(app_name=APP_NAME, user_id="turn")
    for message in history:
        if not message.content:
            continue
        role = "user" if message.role == "user" else "model"
        author = "user" if role == "user" else agent.name
        await session_service.append_event(
            session,
            Event(
                invocation_id=f"history-{uuid.uuid4().hex[:8]}",
                author=author,
                content=types.Content(role=role, parts=[types.Part(text=message.content)]),
            ),
        )

    final: list[str] = []
    async for event in runner.run_async(
        user_id="turn",
        session_id=session.id,
        new_message=types.Content(role="user", parts=[types.Part(text=text)]),
    ):
        if event.author != agent.name or event.content is None or not event.content.parts:
            continue
        if event.is_final_response():
            final.extend(part.text for part in event.content.parts if part.text)
    return "".join(final).strip()


class AdkOrchestrator:
    """The ``ORCHESTRATOR=adk`` counterpart of :class:`consilium.router.router.Router`."""

    def __init__(
        self,
        *,
        model: str | BaseLlm,
        registry: SkillRegistry,
        policy: Policy,
        validator: PolicyValidator | None,
        context_factory: ContextFactory,
        config: RunConfig,
        deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
    ) -> None:
        if deadline_seconds <= 0:
            raise ValueError(f"deadline_seconds must be positive; got {deadline_seconds}")
        self.model = model
        self.registry = registry
        self.policy = policy
        self.validator = validator
        self.context_factory = context_factory
        self.config = config
        self.deadline_seconds = deadline_seconds

    def build_root(
        self,
        question: str,
        *,
        tracer: Tracer | None = None,
        history: Sequence[Message] = (),
        pinned_agent: str | None = None,
    ) -> ConsiliumRootAgent:
        """The agent tree for one turn: root -> planner, specialists, synthesizer.

        Built per turn because every callback closes over the turn's tracer and scratch; the
        objects are small pydantic models and the corpus, index and tables behind the tools are
        shared by reference.
        """
        model_name = self.model if isinstance(self.model, str) else self.model.model
        planner_scratch = WorkerScratch(
            caller="planner", model=model_name, tool_budget=0, iteration_budget=1
        )
        synthesizer_scratch = WorkerScratch(
            caller="synthesizer", model=model_name, tool_budget=0, iteration_budget=1
        )
        worker_scratch: dict[str, WorkerScratch] = {}
        specialists: list[BaseAgent] = []
        for name in sorted(self.policy):
            context = self.context_factory(name)
            scratch = WorkerScratch(
                caller=f"agent:{name}",
                model=model_name,
                tool_budget=self.config.max_tool_calls,
                iteration_budget=self.config.max_iterations,
            )
            worker_scratch[name] = scratch
            specialists.append(
                build_specialist(
                    name,
                    model=self.model,
                    registry=self.registry,
                    policy=self.policy,
                    validator=self.validator,
                    config=self.config,
                    context=context,
                    scratch=scratch,
                )
            )

        root = ConsiliumRootAgent(
            name="consilium",
            description="Consilium Health: plans, delegates to specialists, merges by precedence.",
            sub_agents=[
                build_planner(
                    model=self.model, policy=self.policy, scratch=planner_scratch, tracer=tracer
                ),
                *specialists,
                build_synthesizer(model=self.model, scratch=synthesizer_scratch, tracer=tracer),
            ],
        )
        root._turn = _TurnState(
            question=question,
            history=list(history),
            deadline_seconds=self.deadline_seconds,
            tracer=tracer,
            config=self.config,
            policy=self.policy,
            worker_scratch=worker_scratch,
            pinned_agent=pinned_agent,
        )
        return root

    async def handle(
        self,
        question: str,
        *,
        tracer: Tracer | None = None,
        history: Sequence[Message] = (),
        pinned_agent: str | None = None,
    ) -> RouteResult:
        """Route and answer one question through the ADK agent tree."""
        root = self.build_root(question, tracer=tracer, history=history, pinned_agent=pinned_agent)
        session_service = InMemorySessionService()
        runner = Runner(app_name=APP_NAME, agent=root, session_service=session_service)
        session = await session_service.create_session(app_name=APP_NAME, user_id="turn")
        async for _ in runner.run_async(
            user_id="turn",
            session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text=question)]),
        ):
            pass
        if root.turn.result is None:  # pragma: no cover - the root always sets it
            raise RuntimeError("the ADK root agent finished without a result")
        return root.turn.result
