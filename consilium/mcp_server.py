"""Interface: the skill registry, served over the Model Context Protocol.

The same seven skills the ReAct loop calls, offered to any MCP host over stdio or streamable HTTP.
That is the whole of it -- **this module is an adapter, not a second implementation.**  It owns no
retrieval, no rule table and no rendering of its own; it maps one protocol onto
:class:`consilium.skills.registry.SkillRegistry` and gets out of the way.  The interview sentence
this is built to earn is that the same registry has three consumers -- the internal ReAct loop, the
HTTP API, and any MCP host -- and a consumer that reimplements half of what it consumes does not
support that sentence.

**The published schemas are the registry's own, not a second derivation of them.**  Every tool's
``inputSchema`` is exactly ``Skill.parameters_schema()``, which is the same call
``SkillRegistry.to_tool_schemas`` makes to build the OpenAI tool definitions the loop offers a
model.  There is no hand-written JSON here and no regeneration step that could drift.

*The rejected alternative was the FastMCP decorator API* (``MCPServer`` in mcp 2.x, which is what
``FastMCP`` was renamed to).  It builds a tool's schema from a Python function signature, so serving
seven schemas the project already holds would mean synthesizing seven function signatures for it to
read back -- and the read-back is lossy: it drops ``description``, ``minLength`` and, because the
skills' argument models are ``extra="forbid"``, ``additionalProperties: false``.  Publishing a
schema that says an unknown key is refused while the code silently ignores it is the drift this
project's "derive, never restate" rule exists to prevent, and MCP callers are untrusted callers.
The decorator API also validates arguments before the handler runs, which would refuse a malformed
call **before** :class:`SkillRegistry` ever saw it and therefore **before any ``tool_call`` event
was written** -- an MCP invocation invisible to the traces, which is the one thing this integration
must not produce.  ``mcp.server.lowlevel.Server`` hands the handler the raw ``(name, arguments)``
and takes a ``types.Tool`` list verbatim, so the registry stays the single validator and the single
emitter, exactly as in the loop.  It carries both transports itself: ``run()`` over the streams
``stdio_server()`` yields, and ``streamable_http_app()`` for the remote one.

**The safety layer wraps every result, and it is the same two objects the turn boundary uses.**
``PolicyValidator.check_output`` then ``OutputRepair.apply``, against the red-flag assessment of the
caller's own arguments -- input-side, as everywhere else in this project.  So an MCP host gets the
disclaimer on every result, gets the escalation banner first when it asked about a red-flag symptom,
and gets a forbidden sentence redacted rather than delivered.  The delivered text is
``SkillResult.to_observation()`` -- the same string the loop feeds the model -- inside that
envelope.  One renderer, not two: a second presentation of a skill result would be a surface nothing
else in the repository exercises.  The consequence is that a wrapped result is no longer a bare JSON
document, and that is the right way round: a host that needed the payload without the envelope would
be asking for the guard to be optional.

**A tool call is a tool call, not a turn, and no ``turn`` event is written.**  The MCP host owns the
conversation; this process sees one skill invocation with no question and no delivered answer.
Writing a ``turn`` event would invent both, and every metric in ``eval/metrics.py`` counts turns by
that event -- so a fabricated one would walk straight into the denominators of numbers this project
publishes.  What is written is the real thing that happened: a ``tool_call`` carrying
``transport="mcp"``, whatever ``retrieval`` events the skill produced, and the ``safety`` events for
what the guard found and did.  One trace file per call, under the MCP session's id, so
``consilium trace <session> --turn <n>`` reads an MCP call the same way it reads a turn.

**No ``mcp`` agent is added to ``data/policy.yaml``, and that is load-bearing rather than
fastidious.**  ``eval/validate.py`` derives the *exclusive* skill grants by reading that file: a
skill is exclusive to an agent when no other agent holds it.  An eighth agent holding all seven
skills would make every grant non-exclusive, which would silently empty the route-document check
that guards the golden set's labels -- a published check turned off by a change to an unrelated
feature.  So the permitted set here is the tool list itself: a skill that is not published cannot be
called, and a name that is not a skill comes back from the registry as ``ok=False`` with a
``tool_call`` event, which is what the loop does with the same input.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any

import anyio
import mcp.types as types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from consilium import __version__
from consilium.log import get_logger
from consilium.runtime import Runtime
from consilium.skills.base import SkillResult
from consilium.trace import ToolTransport, Tracer

log = get_logger(__name__)

#: The server name an MCP host displays, and the transport label recorded on every ``tool_call``.
SERVER_NAME = "consilium-health"
MCP_TRANSPORT: ToolTransport = "mcp"

#: What goes in ``tool_call.agent`` for a call that arrived over MCP.  Deliberately not one of the
#: three specialists: the caller is the host's model, and labelling its calls ``consultation``
#: would put them in a bucket the per-agent tool-use breakdown reports as that agent's work.
MCP_AGENT = "mcp"

#: Session ids for MCP traffic.  Same shape as the CLI's and the API's, so ``runs/`` stays one
#: namespace and ``consilium runs purge --session`` works on an MCP session with no special case.
SESSION_PREFIX = "mcp-"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8001
DEFAULT_PATH = "/mcp"

#: Shown to the host model when it connects.  Carries the disclaimer, because this is the first
#: thing a host is told about the server and the disclaimer belongs everywhere an answer might be
#: built from what this returns.
INSTRUCTIONS = (
    "Seven read-only skills over an educational clinical-reference corpus: retrieval, urgency "
    "assessment against a red-flag table, symptom grouping, lifestyle guidance, ICD-10 code "
    "lookup, guideline lookup, and multi-query research that reports where guidance differs. "
    "Every result cites the corpus notes it came from and carries a safety envelope applied by "
    "the same policy the internal agents run under. "
    "Not medical advice. This is an educational software project. It does not diagnose or treat, "
    "and it must not be used for real medical decisions."
)


class SkillTools:
    """The registry, the safety layer and the trace, behind the two MCP handlers.

    Holds the per-server call counter, which is the ``turn_index`` of the trace file a call is
    written to.  Guarded by an :class:`anyio.Lock` because the streamable-HTTP transport can have
    several calls in flight: two calls that read the same counter would write two calls' events
    into one file, which is the same failure ``consilium/api/app.py`` serializes sessions to avoid.
    """

    def __init__(self, runtime: Runtime, *, session_id: str | None = None) -> None:
        _require_retrieval(runtime)
        self.runtime = runtime
        self.session_id = session_id or f"{SESSION_PREFIX}{uuid.uuid4().hex[:12]}"
        self._calls = 0
        self._lock = anyio.Lock()

    def definitions(self) -> list[types.Tool]:
        """One MCP tool per skill, with the registry's own schema.

        ``readOnlyHint`` and ``openWorldHint`` are stated rather than left unset because both are
        true of every skill here and a host may use them to decide whether to ask a person first:
        each skill reads the corpus and two rule tables and writes nothing, and the corpus is a
        closed set of notes rather than the open web.
        """
        return [
            types.Tool(
                name=skill.name,
                description=skill.description,
                input_schema=skill.parameters_schema(),
                annotations=types.ToolAnnotations(
                    read_only_hint=True, destructive_hint=False, open_world_hint=False
                ),
            )
            for skill in self.runtime.registry
        ]

    async def call(self, name: str, arguments: Mapping[str, Any] | None) -> types.CallToolResult:
        """Run one skill for an MCP caller and return its safety-wrapped result.

        Never raises.  ``SkillRegistry`` converts every failure -- unknown name, arguments that do
        not validate, retrieval switched off, a bug inside the skill -- into ``ok=False`` with a
        ``tool_call`` event, and this method turns that into ``isError`` rather than into a
        protocol-level exception.  That is the same contract the ReAct loop gets, and it is the
        contract MCP wants too: a tool failure the host's model can read and act on beats an
        exception it cannot.
        """
        args = dict(arguments or {})
        async with self._lock:
            turn_index = self._calls
            self._calls += 1

        with Tracer.for_turn(
            session_id=self.session_id,
            turn_index=turn_index,
            runs_dir=self.runtime.settings.runs_dir,
        ) as tracer:
            result = await self.runtime.registry.execute(
                name,
                args,
                self.runtime.context(agent=MCP_AGENT, tracer=tracer, transport=MCP_TRANSPORT),
            )
            text = self._guard(result, args, tracer)

        log.info(
            "mcp.tool_call",
            skill=name,
            ok=result.ok,
            sources=list(result.sources),
            session_id=self.session_id,
            turn_index=turn_index,
        )
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=text)], is_error=not result.ok
        )

    def _guard(self, result: SkillResult, args: Mapping[str, Any], tracer: Tracer) -> str:
        """Apply the output policy to what is about to leave the process.

        The red-flag assessment is of the **caller's arguments**, not of the result, for the reason
        it is input-side everywhere else: escalation is owed because of what was asked, and a result
        that never names the symptom would otherwise pass the check by saying nothing.
        """
        assessment = self.runtime.red_flags.assess(caller_text(args))
        body = result.to_observation()
        violations = self.runtime.validator.check_output(
            body, assessment=assessment, agent=MCP_AGENT, tracer=tracer
        )
        # `post_stream` stays False: nothing here is delivered before the guard runs.  The field is
        # for a repair the caller has already seen the unrepaired version of, and no path in this
        # repository produces one -- see CLAUDE.md section 4 refinement 2.
        return self.runtime.repair.apply(body, violations, agent=MCP_AGENT, tracer=tracer).answer


def caller_text(arguments: Mapping[str, Any]) -> str:
    """Every string the caller supplied, as one block for the red-flag matcher.

    Which argument carries the symptom description differs per skill -- ``symptoms`` for
    ``assess_risk``, ``query`` for ``search_knowledge``, ``question`` plus ``sub_queries`` for
    ``deep_research`` -- and a per-skill mapping would be a second table to keep in step with seven
    argument models.  Taking every string is the version that cannot go stale.  Iteration order does
    not matter: the matcher searches the whole block and the values are joined by a newline, so no
    pattern can span two of them.
    """
    parts: list[str] = []
    for value in arguments.values():
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, Sequence):
            parts.extend(item for item in value if isinstance(item, str))
    return "\n".join(parts)


def build_server(runtime: Runtime, *, session_id: str | None = None) -> Server[dict[str, Any]]:
    """Wire the registry onto an MCP server.

    Takes a built :class:`Runtime` rather than building one, so a test -- or an offline demo --
    supplies the ``HashEmbedder``/``NumpyStore`` seams without going through the environment, and so
    there is one composition root rather than a second one here.  Note what is *not* required: no
    provider is called on this path and no working memory is touched, because a tool call is not a
    turn.  A runtime with ``RunConfig.memory`` off serves perfectly well.
    """
    tools = SkillTools(runtime, session_id=session_id)

    async def on_list_tools(
        ctx: ServerRequestContext[dict[str, Any], Any],
        params: types.PaginatedRequestParams | None,
    ) -> types.ListToolsResult:
        del ctx, params  # no pagination: the list is seven entries and is fixed at startup
        return types.ListToolsResult(tools=tools.definitions())

    async def on_call_tool(
        ctx: ServerRequestContext[dict[str, Any], Any], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        del ctx
        return await tools.call(params.name, params.arguments)

    return Server(
        name=SERVER_NAME,
        version=__version__,
        title="Consilium Health skills",
        instructions=INSTRUCTIONS,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


async def serve_stdio(server: Server[dict[str, Any]]) -> None:
    """Serve on stdin/stdout, the transport desktop hosts launch a server with.

    **Nothing else may write to stdout while this runs.**  The stream carries JSON-RPC frames, so a
    log line in it is a protocol error rather than a cosmetic one; ``consilium mcp-serve`` points
    structlog at stderr before calling this, and this module's own reporting goes through the logger
    rather than through ``typer.echo`` for the same reason.
    """
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def http_app(
    server: Server[dict[str, Any]], *, host: str = DEFAULT_HOST, path: str = DEFAULT_PATH
) -> Any:
    """The streamable-HTTP ASGI app, for remote clients.

    A separate app from ``consilium.api.main:app`` and deliberately so: that one serves answers
    built by the router and the agents, this one serves the seven skills underneath them.  Merging
    them would put a tool surface inside the application whose limitations section says it has no
    authentication, and the two have different audiences anyway -- a browser and an MCP host.

    Returns ``Any`` rather than ``Starlette``, which is what it is.  Naming the type would mean
    importing ``starlette`` here, and this project does not declare it: it arrives through
    ``fastapi``, ``sse-starlette`` and ``mcp``, none of which this module is entitled to speak for.
    The value is constructed by the SDK and handed straight to ``uvicorn``; nothing in this
    repository reads a field off it.  CLAUDE.md section 5 governs adding a dependency, and one is
    not being added for a return annotation.
    """
    return server.streamable_http_app(streamable_http_path=path, host=host)


def _require_retrieval(runtime: Runtime) -> None:
    """Refuse a runtime with retrieval switched off.

    Five of the seven skills are declared ``requires_retrieval=True``, so the registry would refuse
    them on every call and the host would be offered a tool list where most of it errors.  (The two
    that would survive are ``assess_risk`` and ``analyze_symptoms``, which read the rule tables
    rather than the corpus -- so this is a broken server rather than a smaller one.)  The
    retrieval-off preset is ``baseline_llm``, which exists as a row of the ablation table and runs
    through the harness; refusing it here refuses to serve a configuration most of whose results
    would be errors, rather than being a limitation of the server.
    """
    if runtime.retriever is None:
        raise ValueError(
            f"the MCP server needs the retrieval corpus: run configuration {runtime.config.name!r} "
            "has retrieval=False, so every corpus-backed skill would fail on every call"
        )
