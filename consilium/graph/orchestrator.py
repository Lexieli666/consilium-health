"""Graph layer: the LangGraph ``StateGraph`` and the orchestrator the turn boundary calls.

``ORCHESTRATOR=langgraph`` swaps exactly one object in :func:`consilium.runtime.run_turn`, the way
``ORCHESTRATOR=adk`` does: a :class:`LangGraphOrchestrator` with the same ``handle()`` signature and
the same :class:`RouteResult` out.  The red-flag assessment of the input, the output validator, the
deterministic repair, working memory and the ``turn`` event are the same code around it, so a
difference between this run and the legacy run is a difference in how the answer was produced and
in nothing else.

The graph::

    START -> planner --(tool calls)--> retrieve -> synthesizer -> validator --(violations,
             |                                        ^                         |  repairs < max)
             +------(no tool calls)-------------------+-------------------------+
                                                                                 |
                                                                     (clean, or budget spent)
                                                                                 v
                                                                             deliver -> END

**planner** is one model call with the MCP server's seven tools bound.  It replaces two legacy
steps -- the planner that chose specialists and the specialists' own tool selection -- with one
decision: which tools, with which arguments.  There are no specialists in this graph and therefore
no ``route`` event: routing accuracy is reported as not applicable for this orchestrator rather than
as a number computed from an event the graph did not emit.

**retrieve** is LangGraph's :class:`~langgraph.prebuilt.ToolNode` over the MCP tools.  The tools
run in the project's own MCP server (``consilium/graph/mcp_tools.py``), which applies the skill
registry, the per-result safety envelope and the retrieval trace exactly as it does for any other
MCP host.  The turn's ``max_tool_calls`` budget is enforced inside the tools, as the legacy loop
enforces it: a call past the budget is refused with the loop's own observation and is not traced,
because it did not run.

**synthesizer** writes the answer from the observations; it is offered no tools.

**validator** is :class:`consilium.safety.validator.PolicyValidator` -- the same object the turn
boundary uses -- run on the draft.  If it finds a violation and fewer than ``max_repairs`` rounds
have been spent, the edge goes back to the synthesizer with a repair instruction built from the
policy's own rule text; otherwise the draft is delivered as-is and the turn boundary's
deterministic :class:`~consilium.safety.repair.OutputRepair` guarantees the delivered text as it
does on every orchestrator.  **The in-graph validator runs with no tracer.**  Violations per 100
turns and repairs per 100 turns are measured once, at the boundary, on the answer the orchestrator
handed over, so the two orchestrators' rates mean the same thing: what was left for the guard to
do.  For this graph that is "after up to two re-syntheses"; the repair rounds themselves are logged
and visible in LangSmith.

**Session memory is the checkpointer.**  :class:`~langgraph.checkpoint.memory.MemorySaver`, keyed
by ``thread_id = session_id``, holds the thread's prior (question, answer) pairs; ``deliver`` strips
each turn's planner message and tool messages before the checkpoint is taken so the thread does
not accumulate observations.  ``RunConfig.memory=False`` gives every turn a fresh thread id, which
is what ``full_no_memory`` and every golden item need.  The working-memory ``history`` the boundary
passes in is therefore not re-injected: the thread already carries it, and injecting both would
show the model every prior turn twice.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    BaseMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode

from consilium.agents.base import AgentResult
from consilium.agents.loop import EMPTY_ANSWER_FALLBACK
from consilium.config import RunConfig
from consilium.graph.chat_model import ProviderChatModel
from consilium.graph.mcp_tools import McpToolkit, TurnBudget, current_budget
from consilium.graph.prompts import SYNTHESIZER, planner_prompt, repair_instruction
from consilium.llm.base import Message
from consilium.log import get_logger
from consilium.router.blackboard import Blackboard
from consilium.router.plan import Subtask
from consilium.router.router import DEFAULT_DEADLINE_SECONDS, RouteResult, _sources
from consilium.runtime import Runtime
from consilium.safety.red_flags import RedFlagAssessment
from consilium.skills.base import SkillResult
from consilium.trace import Tracer

log = get_logger(__name__)

#: ``AgentResult.agent`` and ``tool_call.agent`` for the graph's single worker.
GRAPH_AGENT = "planner"
GRAPH_OBJECTIVE = "Plan the tool calls, run them over MCP, synthesize, validate."


class GraphState(TypedDict, total=False):
    """What flows through the graph.  Checkpointed, so everything here is plain data."""

    messages: Annotated[list[AnyMessage], add_messages]
    question: str
    #: The synthesizer's latest text, before the deterministic repair at the turn boundary.
    draft: str
    #: Rule ids the in-graph validator found in ``draft``; empty when it passed.
    violations: list[str]
    #: Repair rounds spent this turn.
    repairs: int
    #: Message ids of this turn's planner reply and tool messages, removed by ``deliver``.
    scratch_ids: list[str]


@dataclass
class _Turn:
    """Per-turn objects the nodes need and the checkpoint must not hold."""

    tracer: Tracer | None
    assessment: RedFlagAssessment
    budget: TurnBudget
    repair_rounds: list[list[str]]


_current_turn: ContextVar[_Turn | None] = ContextVar("consilium_graph_turn", default=None)


def _turn() -> _Turn:
    turn = _current_turn.get()
    if turn is None:
        raise RuntimeError("graph nodes must run inside LangGraphOrchestrator.handle()")
    return turn


class ConsiliumGraph:
    """The compiled graph, its checkpointer and its MCP toolkit: built once per runtime."""

    def __init__(self, runtime: Runtime, *, max_repairs: int | None = None) -> None:
        self.runtime = runtime
        self.config: RunConfig = runtime.config
        self.max_repairs = (
            runtime.settings.graph_max_repairs if max_repairs is None else max_repairs
        )
        self.toolkit = McpToolkit(runtime)
        self.checkpointer = MemorySaver()
        self.model = ProviderChatModel(
            provider=runtime.provider,
            tracer_for=lambda: turn.tracer if (turn := _current_turn.get()) else None,
        )
        self._compiled: CompiledStateGraph[Any, Any, Any, Any] | None = None
        self._lock = asyncio.Lock()

    async def compiled(self) -> CompiledStateGraph[Any, Any, Any, Any]:
        """Compile on first use, after the MCP session is open and the tool list is known."""
        async with self._lock:
            tools = await self.toolkit.connect()
            if self._compiled is None:
                self._compiled = self._build(tools)
            return self._compiled

    def _build(self, tools: Sequence[Any]) -> CompiledStateGraph[Any, Any, Any, Any]:
        planner_model = self.model.bind_tools(tools)
        planner_system = planner_prompt(self.config.max_tool_calls)

        async def planner(state: GraphState, config: RunnableConfig) -> GraphState:
            messages: list[BaseMessage] = [SystemMessage(planner_system), *state["messages"]]
            reply = await planner_model.ainvoke(messages, config=config, caller="planner")
            return {"messages": [reply], "scratch_ids": [str(reply.id)]}

        async def synthesizer(state: GraphState, config: RunnableConfig) -> GraphState:
            turn = _turn()
            violations = state.get("violations") or []
            repairs = state.get("repairs", 0)
            transcript = list(state["messages"])
            # A planner that called no tool replied with prose.  That reply is not evidence, and
            # a transcript ending on an assistant turn is a prefill request, which some models
            # refuse; the synthesizer answers the question itself.
            if (
                transcript
                and isinstance(transcript[-1], AIMessage)
                and not transcript[-1].tool_calls
            ):
                transcript.pop()
            messages: list[BaseMessage] = [SystemMessage(SYNTHESIZER), *transcript]
            if violations:
                repairs += 1
                turn.repair_rounds.append(list(violations))
                log.info("graph.repair_round", round=repairs, violations=violations)
                messages.append(AIMessage(state.get("draft", "")))
                messages.append(
                    HumanMessage(
                        repair_instruction(
                            self.runtime.policy, self._violations_of(state.get("draft", ""))
                        )
                    )
                )
            reply = await self.model.ainvoke(messages, config=config, caller="synthesizer")
            draft = (reply.content if isinstance(reply.content, str) else "").strip()
            if not draft:
                log.warning("graph.empty_synthesis")
                draft = EMPTY_ANSWER_FALLBACK
            return {"draft": draft, "repairs": repairs}

        def validator(state: GraphState) -> GraphState:
            found = self._violations_of(state.get("draft", ""))
            return {"violations": sorted({violation.rule for violation in found})}

        def after_validator(state: GraphState) -> Literal["synthesizer", "deliver"]:
            if state.get("violations") and state.get("repairs", 0) < self.max_repairs:
                return "synthesizer"
            return "deliver"

        def deliver(state: GraphState) -> dict[str, Any]:
            # ``RemoveMessage`` is a reducer instruction rather than one of the ``AnyMessage``
            # kinds, which is why this node's return is typed as a plain mapping.
            removals: list[BaseMessage] = [
                RemoveMessage(id=message_id) for message_id in state.get("scratch_ids", [])
            ]
            removals.extend(
                RemoveMessage(id=str(message.id))
                for message in state["messages"]
                if isinstance(message, ToolMessage) and message.id
            )
            return {
                "messages": [*removals, AIMessage(state.get("draft", ""))],
                "scratch_ids": [],
                "violations": [],
            }

        def after_planner(state: GraphState) -> Literal["retrieve", "synthesizer"]:
            last = state["messages"][-1]
            return "retrieve" if isinstance(last, AIMessage) and last.tool_calls else "synthesizer"

        graph: StateGraph[GraphState] = StateGraph(GraphState)
        graph.add_node("planner", planner)
        graph.add_node("retrieve", ToolNode(list(tools), name="retrieve"))
        graph.add_node("synthesizer", synthesizer)
        graph.add_node("validator", validator)
        graph.add_node("deliver", deliver)
        graph.add_edge(START, "planner")
        graph.add_conditional_edges("planner", after_planner)
        graph.add_edge("retrieve", "synthesizer")
        graph.add_edge("synthesizer", "validator")
        graph.add_conditional_edges("validator", after_validator)
        graph.add_edge("deliver", END)
        return graph.compile(checkpointer=self.checkpointer)

    def _violations_of(self, draft: str) -> list[Any]:
        """The boundary's validator on the draft, **untraced** (see the module docstring)."""
        return self.runtime.validator.check_output(
            draft, assessment=_turn().assessment, agent=GRAPH_AGENT, tracer=None
        )

    async def aclose(self) -> None:
        await self.toolkit.aclose()


#: One graph per runtime per process, so the checkpointer and the MCP session outlive a turn.
_GRAPHS: dict[int, ConsiliumGraph] = {}


def graph_for(runtime: Runtime) -> ConsiliumGraph:
    graph = _GRAPHS.get(id(runtime))
    if graph is None:
        graph = ConsiliumGraph(runtime)
        _GRAPHS[id(runtime)] = graph
    return graph


async def close_all() -> None:
    """Close every MCP session this process opened.  Tests and the CLI call it on exit."""
    for key in list(_GRAPHS):
        await _GRAPHS.pop(key).aclose()


class LangGraphOrchestrator:
    """The ``ORCHESTRATOR=langgraph`` counterpart of :class:`consilium.router.router.Router`."""

    def __init__(
        self,
        runtime: Runtime,
        *,
        deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
        graph: ConsiliumGraph | None = None,
    ) -> None:
        if deadline_seconds <= 0:
            raise ValueError(f"deadline_seconds must be positive; got {deadline_seconds}")
        self.runtime = runtime
        self.deadline_seconds = deadline_seconds
        self.graph = graph or graph_for(runtime)

    async def handle(
        self,
        question: str,
        *,
        tracer: Tracer | None = None,
        history: Sequence[Message] = (),
        pinned_agent: str | None = None,
    ) -> RouteResult:
        """Run one turn through the graph and return what the turn boundary expects."""
        del history  # the thread checkpoint carries the session; see the module docstring
        if pinned_agent is not None:
            log.warning("graph.pinned_agent_ignored", agent=pinned_agent)

        session_id = tracer.session_id if tracer is not None else f"graph-{uuid.uuid4().hex[:12]}"
        thread_id = session_id if self.runtime.config.memory else f"{session_id}:{uuid.uuid4().hex}"
        turn = _Turn(
            tracer=tracer,
            assessment=self.runtime.red_flags.assess(question),
            budget=TurnBudget(tracer=tracer, max_tool_calls=self.runtime.config.max_tool_calls),
            repair_rounds=[],
        )

        board = Blackboard(tracer=tracer)
        (handle,) = await board.assign(
            [
                Subtask(
                    subtask_id=f"1-{GRAPH_AGENT}",
                    agent=GRAPH_AGENT,
                    objective=GRAPH_OBJECTIVE,
                    why="orchestrator=langgraph",
                )
            ]
        )
        await handle.started()

        token_turn = _current_turn.set(turn)
        token_budget = current_budget.set(turn.budget)
        try:
            compiled = await self.graph.compiled()
            async with asyncio.timeout(self.deadline_seconds):
                final: dict[str, Any] = await compiled.ainvoke(
                    {
                        "question": question,
                        "messages": [HumanMessage(question)],
                        "repairs": 0,
                        "violations": [],
                        "scratch_ids": [],
                    },
                    config={
                        "configurable": {"thread_id": thread_id},
                        "run_name": "consilium-turn",
                        "metadata": {
                            "session_id": session_id,
                            "trace_id": tracer.trace_id if tracer is not None else None,
                            "orchestrator": "langgraph",
                            "config": self.runtime.config.name,
                        },
                    },
                )
        except TimeoutError:
            log.warning("graph.turn_timeout", deadline_seconds=self.deadline_seconds)
            await handle.timed_out(f"exceeded the {self.deadline_seconds:.0f}s turn deadline")
            return self._result(board, mode_note="timeout")
        except Exception as exc:
            log.exception("graph.turn_failed")
            await handle.failed(f"{type(exc).__name__}: {exc}")
            return self._result(board, mode_note="failed")
        finally:
            current_budget.reset(token_budget)
            _current_turn.reset(token_turn)

        tool_results = tuple(
            SkillResult(skill=skill, ok=ok, sources=sources, data={"args": args})
            if ok
            else SkillResult(
                skill=skill, ok=False, error="tool reported an error", data={"args": args}
            )
            for skill, ok, sources, args in turn.budget.executed
        )
        sources = _dedupe(
            source for _, ok, srcs, _ in turn.budget.executed if ok for source in srcs
        )
        await handle.completed(
            AgentResult(
                agent=GRAPH_AGENT,
                answer=final.get("draft") or EMPTY_ANSWER_FALLBACK,
                sources=sources,
                tool_results=tool_results,
                iterations=1 + final.get("repairs", 0),
                forced=False,
            )
        )
        log.info(
            "graph.turn_done",
            tool_calls=turn.budget.used,
            repair_rounds=len(turn.repair_rounds),
            unresolved=final.get("violations") or [],
        )
        return self._result(board)

    @staticmethod
    def _result(board: Blackboard, *, mode_note: str | None = None) -> RouteResult:
        from consilium.router.synthesizer import ALL_FAILED_ANSWER, missing_note

        completed = board.completed()
        missing = board.unfinished()
        if completed and completed[0].result is not None:
            answer = completed[0].result.answer
        else:
            answer = ALL_FAILED_ANSWER + missing_note(missing, extra=mode_note or "")
        return RouteResult(
            answer=answer,
            sources=_sources(completed),
            mode="single",
            agents=(GRAPH_AGENT,),
            fallback=False,
            records=board.records,
            missing=tuple(record.subtask.agent for record in missing),
        )


def _dedupe(values: Any) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for value in values:
        seen.setdefault(value, None)
    return tuple(seen)
