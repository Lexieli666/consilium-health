"""Graph layer: the project's MCP server, loaded as LangChain tools.

**Why this file exists instead of ``langchain-mcp-adapters``.**  The adapter package (0.3.2, and
``main`` on 2026-10-13) declares ``mcp<2.0`` and imports ``mcp.server.fastmcp`` and
``mcp.shared.context.RequestContext``, none of which exist in mcp 2.x.  ``consilium/mcp_server.py``
is written against the mcp 2.x lowlevel ``Server`` API and ``pyproject.toml`` pins ``mcp>=2.1,<3``,
so the two cannot be installed into one environment and the adapter fails at import time under the
version this project runs.  What follows does what ``langchain_mcp_adapters.tools.load_mcp_tools``
does -- one :class:`~langchain_core.tools.StructuredTool` per MCP tool, the server's own
``inputSchema`` as the argument schema, ``call_tool`` behind a coroutine, the text content back --
on the mcp 2.x :class:`mcp.Client`.  When the adapter package supports mcp 2.x this module is the
one thing to delete.

**Three ways to reach the server, chosen by ``CONSILIUM_GRAPH_MCP``.**  ``inprocess`` hands
:func:`consilium.mcp_server.build_server` to the SDK client, which speaks JSON-RPC to it over the
SDK's in-memory streams -- the same "mock transport" the server's own contract tests use -- so the
graph shares the index the runtime already built.  ``stdio`` launches ``consilium mcp-serve`` as a
child process over pipes.  A URL connects to a running ``--transport http`` server.  The protocol
and the server code are identical on all three; what differs is who owns the trace files.

**Trace forwarding, and why the eval needs it.**  The MCP server writes each call's ``tool_call``
and ``retrieval`` events to its own trace file, ``runs/<server session>/<n>.jsonl``, because to the
server a tool call is not a turn (``consilium/mcp_server.py`` says why).  The evaluation harness
computes recall@5 and tool calls per turn from the events of the *turn's* tracer, in memory.  So
after every call this client reads the server-side file for that call and re-emits its
``tool_call`` and ``retrieval`` events through the turn's tracer, marked ``transport="mcp"``.  The
server's ``safety`` events are deliberately not forwarded: they describe the envelope around one
tool result, and the turn-level violation and repair rates are measured once, at the turn
boundary, on both orchestrators.  Forwarding needs the server's runs directory, which the
``inprocess`` and ``stdio`` modes control and a remote URL does not -- over HTTP the turn trace
carries no retrieval events and the harness reports retrieval as unmeasured, which is the honest
outcome rather than a hidden zero.
"""

from __future__ import annotations

import asyncio
import shlex
import sys
import uuid
from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from mcp import Client, StdioServerParameters
from mcp.types import CallToolResult

from consilium.agents.loop import BUDGET_EXHAUSTED_OBSERVATION
from consilium.log import get_logger
from consilium.runtime import Runtime
from consilium.trace import (
    RetrievalEvent,
    ToolCallEvent,
    TraceError,
    Tracer,
    read_trace,
    trace_path,
)

log = get_logger(__name__)

#: ``tool_call.agent`` for calls the graph's planner requested.  Not one of the three specialists,
#: for the reason ``consilium/mcp_server.py`` gives for ``MCP_AGENT``: the caller is the graph.
GRAPH_AGENT = "planner"


@dataclass
class TurnBudget:
    """Per-turn tool-call budget, enforced by the tools themselves.

    The legacy loop enforces ``max_tool_calls`` by refusing calls beyond the budget *without* a
    ``tool_call`` event, because they were not executed.  ``ToolNode`` executes every call the
    planner emitted, so the refusal has to live inside the tool: a call past the budget returns the
    loop's ``BUDGET_EXHAUSTED_OBSERVATION`` and never reaches the server.
    """

    tracer: Tracer | None
    max_tool_calls: int
    used: int = 0
    #: ``(skill, ok, sources, args)`` of every executed call, in completion order, for the
    #: ``AgentResult`` the turn boundary reads ``tool_results`` and ``sources`` from.
    executed: list[tuple[str, bool, tuple[str, ...], dict[str, Any]]] = field(default_factory=list)


#: The budget of the turn in flight.  A context variable rather than graph state because the
#: tracer it carries must not be checkpointed, and asyncio copies the context into every task
#: LangGraph and ``ToolNode`` spawn, so a tool coroutine sees the turn that called it.
current_budget: ContextVar[TurnBudget | None] = ContextVar("consilium_graph_budget", default=None)


@dataclass
class _Forwarder:
    """Reads the server-side trace of each call and re-emits it into the turn's tracer."""

    runs_dir: Path
    session_id: str
    _scan_from: int = 0
    _loaded: set[int] = field(default_factory=set)
    _pending: dict[int, list[ToolCallEvent | RetrievalEvent]] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def forward(
        self, skill: str, args: Mapping[str, Any], tracer: Tracer | None
    ) -> list[str]:
        """Forward the events of the call that ran ``skill(args)``; return its source doc ids."""
        async with self._lock:
            self._scan()
            index = self._match(skill, args)
            if index is None:
                log.warning("graph.mcp_trace_missing", skill=skill, session_id=self.session_id)
                return []
            events = self._pending.pop(index)
        sources: list[str] = []
        for event in events:
            if isinstance(event, ToolCallEvent):
                sources = list(event.source_doc_ids)
                if tracer is not None:
                    tracer.tool_call(
                        agent=GRAPH_AGENT,
                        skill=event.skill,
                        args=event.args,
                        ok=event.ok,
                        error=event.error,
                        latency_ms=event.latency_ms,
                        source_doc_ids=event.source_doc_ids,
                        transport="mcp",
                    )
            elif tracer is not None:
                tracer.retrieval(
                    skill=event.skill,
                    query=event.query,
                    category_filter=event.category_filter,
                    fused_topk=event.fused_topk,
                    returned_k=event.returned_k,
                    latency_ms=event.latency_ms,
                )
        return sources

    #: How far past the first unwritten call index the scan looks.  Concurrent calls finish out
    #: of order, so call ``n+1``'s file can exist before call ``n``'s; the window only has to
    #: exceed the number of calls that can be in flight at once, which the tool budget bounds.
    LOOKAHEAD = 16

    def _scan(self) -> None:
        """Load every server-side trace file written since the last scan, gaps allowed."""
        for index in range(self._scan_from, self._scan_from + self.LOOKAHEAD):
            if index in self._loaded:
                continue
            path = trace_path(self.runs_dir, self.session_id, index)
            if not path.exists():
                continue
            # The server flushes line by line, so a concurrent call's file can exist before its
            # ``tool_call`` record (written after the skill ran, after any ``retrieval``) is in
            # it.  A file without one is a call still running: leave it for the next scan.
            try:
                events = [
                    event
                    for event in read_trace(path)
                    if isinstance(event, ToolCallEvent | RetrievalEvent)
                ]
            except TraceError:  # a line still being written
                continue
            if not any(isinstance(event, ToolCallEvent) for event in events):
                continue
            self._loaded.add(index)
            self._pending[index] = events
        while self._scan_from in self._loaded:
            self._loaded.discard(self._scan_from)
            self._scan_from += 1

    def _match(self, skill: str, args: Mapping[str, Any]) -> int | None:
        """The lowest unconsumed call whose ``tool_call`` event is this skill with these args.

        Matched on content rather than on position because the server numbers calls in the order
        it *received* them and ``ToolNode`` runs a planner's several calls concurrently.
        """
        wanted = dict(args)
        for index in sorted(self._pending):
            for event in self._pending[index]:
                if (
                    isinstance(event, ToolCallEvent)
                    and event.skill == skill
                    and event.args == wanted
                ):
                    return index
        return None


class McpToolkit:
    """The project's MCP server as LangChain tools, with the turn trace kept whole.

    One toolkit per process: the connection is opened once and reused across turns, which is what
    an MCP host does with a server it launched.  ``connect()`` is idempotent per event loop; a new
    loop (each pytest function, say) gets a new connection.
    """

    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime
        self.settings = runtime.settings
        self.session_id = f"mcp-graph-{uuid.uuid4().hex[:12]}"
        self._client: Client | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._keeper: asyncio.Task[None] | None = None
        self._closing: asyncio.Event | None = None
        self._tools: list[BaseTool] = []
        self._forwarder: _Forwarder | None = None

    @property
    def mode(self) -> str:
        target = self.settings.graph_mcp
        if target in ("inprocess", "stdio"):
            return target
        if target.startswith(("http://", "https://")):
            return "http"
        raise ValueError(
            f"CONSILIUM_GRAPH_MCP must be 'inprocess', 'stdio' or an http(s):// URL; got {target!r}"
        )

    def _target(self) -> Any:
        """What :class:`mcp.Client` connects to, per mode."""
        mode = self.mode
        if mode == "inprocess":
            from consilium.mcp_server import build_server

            return build_server(self.runtime, session_id=self.session_id)
        if mode == "stdio":
            extra = shlex.split(self.settings.graph_mcp_args)
            return StdioServerParameters(
                command=sys.executable,
                args=[
                    "-m",
                    "consilium.cli",
                    "mcp-serve",
                    "--transport",
                    "stdio",
                    "--session",
                    self.session_id,
                    "--config",
                    self.runtime.config.name,
                    *extra,
                ],
                env=_child_env(self.settings.runs_dir),
            )
        return self.settings.graph_mcp

    async def connect(self) -> list[BaseTool]:
        """Open the MCP session (once per event loop) and return the tools."""
        loop = asyncio.get_running_loop()
        if self._client is not None and self._loop is loop:
            return self._tools
        if self._client is not None:
            await self.aclose()

        # The SDK client is an anyio context manager and must be entered and exited by the same
        # task.  A turn runs in whatever task called it and the session outlives the turn, so the
        # session is held open by a task of its own and `aclose()` asks that task to leave.
        opened: asyncio.Future[Client] = loop.create_future()
        closing = asyncio.Event()
        self._keeper = loop.create_task(self._keep(opened, closing), name="consilium-mcp-session")
        client = await opened
        self._client = client
        self._closing = closing
        self._loop = loop
        self._forwarder = (
            None
            if self.mode == "http"
            else _Forwarder(runs_dir=self.settings.runs_dir, session_id=self.session_id)
        )
        listed = await client.list_tools()
        self._tools = [
            self._to_tool(tool.name, tool.description or "", tool.input_schema)
            for tool in listed.tools
        ]
        log.info(
            "graph.mcp_connected",
            mode=self.mode,
            tools=[tool.name for tool in self._tools],
            session_id=self.session_id,
        )
        return self._tools

    async def _keep(self, opened: asyncio.Future[Client], closing: asyncio.Event) -> None:
        try:
            async with Client(self._target()) as client:
                opened.set_result(client)
                await closing.wait()
        except BaseException as exc:
            if not opened.done():
                opened.set_exception(exc)
                return
            log.warning("graph.mcp_session_ended", error=f"{type(exc).__name__}: {exc}")

    async def aclose(self) -> None:
        if self._client is None:
            return
        keeper, closing = self._keeper, self._closing
        self._client, self._loop, self._keeper, self._closing = None, None, None, None
        if closing is not None:
            closing.set()
        if keeper is not None and self._same_loop(keeper):
            await keeper

    @staticmethod
    def _same_loop(task: asyncio.Task[None]) -> bool:
        """Whether ``task`` belongs to the running loop; a dead loop's task cannot be awaited."""
        try:
            return task.get_loop() is asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - no running loop
            return False

    async def get_tools(self) -> list[BaseTool]:
        """The adapter package's name for it, kept so the call site reads the same."""
        return await self.connect()

    def _to_tool(self, name: str, description: str, schema: dict[str, Any]) -> BaseTool:
        async def call(**arguments: Any) -> str:
            return await self.call(name, arguments)

        return StructuredTool.from_function(
            coroutine=call, name=name, description=description, args_schema=schema
        )

    async def call(self, name: str, arguments: Mapping[str, Any]) -> str:
        """One MCP ``call_tool``, under the turn's budget, with its trace forwarded."""
        budget = current_budget.get()
        if budget is not None:
            if budget.used >= budget.max_tool_calls:
                log.info("graph.tool_budget_exhausted", skill=name)
                return BUDGET_EXHAUSTED_OBSERVATION
            budget.used += 1

        if self._client is None:  # pragma: no cover - ToolNode only runs after connect()
            raise RuntimeError("McpToolkit.connect() must run before a tool is called")
        result = await self._client.call_tool(name, dict(arguments))
        if not isinstance(result, CallToolResult):  # pragma: no cover - this server never elicits
            raise TypeError(f"unexpected MCP result for {name}: {type(result).__name__}")
        text = "\n".join(block.text for block in result.content if block.type == "text")

        sources: list[str] = []
        if self._forwarder is not None:
            tracer = budget.tracer if budget is not None else None
            sources = await self._forwarder.forward(name, arguments, tracer)
        if budget is not None:
            budget.executed.append((name, not result.is_error, tuple(sources), dict(arguments)))
        return text


def tool_names(tools: Sequence[BaseTool]) -> list[str]:
    return [tool.name for tool in tools]


def _child_env(runs_dir: Path) -> dict[str, str]:
    """The ``stdio`` child's environment: this process's, with the runs directory pinned.

    Pinned so that the child's trace files land where the forwarder reads them.  ``.env`` is read
    by the child itself through ``Settings.from_env``; nothing secret is passed here that was not
    already in this process's environment.
    """
    import os

    return {**os.environ, "CONSILIUM_RUNS_DIR": str(runs_dir)}
