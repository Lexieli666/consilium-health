"""Router layer: plan, dispatch, merge.

Planner-worker orchestration with a blackboard.  **Not a swarm**: there is a central planner, an
explicit assignment of subtasks to named workers, and a shared board.  Calling that a swarm invites
the reply "so it is a supervisor pattern with extra words", and the reply would be right.

The three routing modes come from ``RunConfig.router`` and exist so the ablation table can be
produced by a flag rather than a code edit:

``planner``  the planner decides.  One subtask runs on the fast path; two or more run concurrently
             and are merged.  This is the only mode that emits a ``route`` event.
``single``   no planner call at all; the default specialist answers.  The ``single_agent_rag``
             control.
``none``     no routing of any kind; the default specialist answers with whatever the config leaves
             it.  The ``baseline_llm`` control.

**A ``route`` event is emitted only when a routing decision was actually made.**  Recording one for
``single`` or ``none`` would let planner-fallback rate compute to a confident 0% for a configuration
that has no planner -- a number that should be n/a, reported as a good result.  The ablation table
already marks routing accuracy n/a for both rows, and the absence of the event is what makes that
fall out of the data rather than out of a special case in the metric code.

**Partial failure is tolerated by design.**  Each worker runs under a shared deadline; one that
fails or times out is recorded on the board and the answer is synthesized from whoever finished,
with the missing perspective named in the delivered text.  The alternative -- failing the turn when
any worker fails -- makes the parallel path strictly less reliable than the single one, which would
be an odd thing to then measure as an improvement.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from consilium.agents.base import AgentResult
from consilium.config import RunConfig
from consilium.llm.base import Message
from consilium.log import get_logger
from consilium.router.blackboard import Blackboard, SubtaskHandle, SubtaskRecord
from consilium.router.plan import Subtask
from consilium.router.planner import FALLBACK_AGENT, FALLBACK_OBJECTIVE, Planner
from consilium.router.synthesizer import ALL_FAILED_ANSWER, Synthesizer, missing_note
from consilium.skills.base import SkillContext
from consilium.trace import RouteMode, Tracer, stopwatch

log = get_logger(__name__)

#: Wall-clock ceiling on the whole parallel phase.  Shared, not per worker: the turn's budget is
#: what the user is waiting on, and three workers each allowed 90 seconds is a 90-second budget only
#: if they all start at once and none of them queues.
DEFAULT_DEADLINE_SECONDS = 90.0


class Worker(Protocol):
    """Everything the router needs from a specialist, and nothing more.

    A protocol rather than ``BaseAgent`` because it states the contract in one place: a worker is
    given an objective and returns a result, and there is no method here through which one worker
    could reach another.  ``BaseAgent`` satisfies it structurally, so nothing changes for the
    production path -- what changes is that the dependency is now the capability rather than the
    class, which is also what lets a test substitute a worker that times out or raises.
    """

    @property
    def name(self) -> str: ...

    def for_turn(self, ctx: SkillContext) -> SkillContext: ...

    async def answer(
        self,
        question: str,
        *,
        ctx: SkillContext,
        history: Sequence[Message] = (),
        objective: str | None = None,
    ) -> AgentResult: ...


#: Builds one specialist by name.  Injected rather than imported, because ``consilium/runtime.py``
#: constructs agents and also constructs the router; importing it here would be a cycle.
AgentFactory = Callable[[str], Worker]

#: Builds the per-turn skill context for one agent.
ContextFactory = Callable[[str], SkillContext]


@dataclass(frozen=True)
class RouteResult:
    """What routing produced, before the turn boundary stamps it into a ``turn`` event."""

    answer: str
    sources: tuple[str, ...]
    mode: RouteMode
    agents: tuple[str, ...]
    fallback: bool
    records: tuple[SubtaskRecord, ...]
    #: Agents whose subtask failed or timed out.  Named in the delivered answer.
    missing: tuple[str, ...]

    @property
    def results(self) -> tuple[AgentResult, ...]:
        return tuple(record.result for record in self.records if record.result is not None)


class Router:
    """Decides who answers, runs them, and merges what comes back."""

    def __init__(
        self,
        *,
        planner: Planner,
        synthesizer: Synthesizer,
        agent_factory: AgentFactory,
        context_factory: ContextFactory,
        config: RunConfig,
        deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
    ) -> None:
        if deadline_seconds <= 0:
            raise ValueError(f"deadline_seconds must be positive; got {deadline_seconds}")
        self.planner = planner
        self.synthesizer = synthesizer
        self.agent_factory = agent_factory
        self.context_factory = context_factory
        self.config = config
        self.deadline_seconds = deadline_seconds

    async def handle(
        self,
        question: str,
        *,
        tracer: Tracer | None = None,
        history: Sequence[Message] = (),
        pinned_agent: str | None = None,
    ) -> RouteResult:
        """Route and answer one question.

        ``history`` is the session's compacted prior turns.  **Every worker of a parallel turn
        receives the same history object**, which is what "sharing across agents within one turn is
        achieved by passing the same instance" means in practice -- and it is also why memory is a
        parameter here rather than something the router owns: the router is per turn, the session is
        not.

        ``pinned_agent`` bypasses routing and runs one named specialist.  It is a debugging
        affordance -- ``consilium ask --agent diagnostic`` -- and is deliberately the same code path
        as ``router="single"``, so that pinning cannot accidentally become a fourth way to run a
        turn with its own behaviour.  No measured run pins an agent.
        """
        if pinned_agent is not None or self.config.router != "planner":
            return await self._unrouted(
                question, tracer=tracer, history=history, agent=pinned_agent or FALLBACK_AGENT
            )

        with stopwatch() as elapsed_ms:
            subtasks, fallback = await self.planner.plan(question, tracer=tracer)
        planning_ms = elapsed_ms()

        mode: RouteMode = "parallel" if len(subtasks) > 1 else "single"
        if tracer is not None:
            tracer.route(
                mode=mode,
                agents=[subtask.agent for subtask in subtasks],
                subtasks=[subtask.to_trace() for subtask in subtasks],
                fallback=fallback,
                latency_ms=planning_ms,
            )

        board = Blackboard(tracer=tracer)
        handles = await board.assign(subtasks)
        await self._dispatch(question, handles, history=history, parallel=mode == "parallel")

        completed = board.completed()
        missing = board.unfinished()
        answer = await self._answer(question, completed, missing, tracer=tracer)

        return RouteResult(
            answer=answer,
            sources=_sources(completed),
            mode=mode,
            agents=tuple(subtask.agent for subtask in subtasks),
            fallback=fallback,
            records=board.records,
            missing=tuple(sorted({record.subtask.agent for record in missing})),
        )

    async def _unrouted(
        self, question: str, *, tracer: Tracer | None, history: Sequence[Message], agent: str
    ) -> RouteResult:
        """`router="single"`, `router="none"`, or a pinned agent: one specialist, no route event."""
        subtask = Subtask(
            subtask_id=f"1-{agent}",
            agent=agent,
            objective=FALLBACK_OBJECTIVE,
            why=f"router={self.config.router}",
        )
        board = Blackboard(tracer=tracer)
        (handle,) = await board.assign([subtask])
        await self._run_one(question, handle, history=history)

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

    async def _dispatch(
        self,
        question: str,
        handles: Sequence[SubtaskHandle],
        *,
        history: Sequence[Message],
        parallel: bool,
    ) -> None:
        if not parallel:
            await self._run_one(question, handles[0], history=history)
            return

        deadline_at = asyncio.get_running_loop().time() + self.deadline_seconds
        # `return_exceptions=True` even though every worker catches its own: a bug in the worker
        # wrapper itself must not cancel the siblings that already succeeded.
        await asyncio.gather(
            *(
                self._run_one(question, handle, history=history, deadline_at=deadline_at)
                for handle in handles
            ),
            return_exceptions=True,
        )

    async def _run_one(
        self,
        question: str,
        handle: SubtaskHandle,
        *,
        history: Sequence[Message] = (),
        deadline_at: float | None = None,
    ) -> None:
        """Run one worker, recording its outcome on the board whatever happens."""
        await handle.started()
        agent = self.agent_factory(handle.agent)
        ctx = agent.for_turn(self.context_factory(handle.agent))

        try:
            if deadline_at is None:
                result = await agent.answer(
                    question, ctx=ctx, history=history, objective=handle.subtask.objective
                )
            else:
                async with asyncio.timeout_at(deadline_at):
                    result = await agent.answer(
                        question,
                        ctx=ctx,
                        history=history,
                        objective=handle.subtask.objective,
                    )
        except TimeoutError:
            log.warning(
                "router.worker_timeout",
                agent=handle.agent,
                subtask=handle.subtask.subtask_id,
            )
            await handle.timed_out(f"exceeded the {self.deadline_seconds:.0f}s turn deadline")
            return
        except Exception as exc:
            log.exception("router.worker_failed", agent=handle.agent)
            await handle.failed(f"{type(exc).__name__}: {exc}")
            return

        await handle.completed(result)

    async def _answer(
        self,
        question: str,
        completed: Sequence[SubtaskRecord],
        missing: Sequence[SubtaskRecord],
        *,
        tracer: Tracer | None,
    ) -> str:
        """The delivered text: one worker's answer, or the merge of several."""
        if len(completed) <= 1 and not missing:
            if completed and completed[0].result:
                return completed[0].result.answer
            return ALL_FAILED_ANSWER
        return await self.synthesizer.merge(
            question, completed=completed, missing=missing, tracer=tracer
        )


def _sources(records: Sequence[SubtaskRecord]) -> tuple[str, ...]:
    """Every cited ``doc_id``, deduplicated, in precedence-ordered first-seen order."""
    from consilium.router.synthesizer import order_by_precedence

    seen: dict[str, None] = {}
    for record in order_by_precedence(records):
        if record.result is not None:
            for doc_id in record.result.sources:
                seen.setdefault(doc_id, None)
    return tuple(seen)
