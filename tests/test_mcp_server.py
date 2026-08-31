"""The MCP server: schema fidelity, the safety envelope, and what lands in the trace.

The contract Part B of the plan asks for is that **each MCP tool's schema round-trips through an SDK
client** -- not that the server has a tool of that name, and not that a schema exists somewhere in
the process.  So the assertions here go through `mcp.Client`, which speaks the protocol, and compare
what comes back against `Skill.parameters_schema()`, which is the registry's own definition and the
same call that builds the OpenAI tool definitions the ReAct loop offers a model.  Two derivations
that agree today would drift; one derivation observed through a client is what the test is for.

`Client(server)` connects in-process over the SDK's own in-memory streams -- the "mock transport"
the acceptance criterion names -- and one test additionally launches `consilium mcp-serve` as a
subprocess and talks to it over real pipes, because an in-process client cannot show that the
console command wires the transport up at all.  Neither touches the network, and neither needs a
provider: an MCP host brings its own model, so nothing on this path calls one.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters
from mcp.types import CallToolResult
from typer.testing import CliRunner

from consilium.cli import app as cli_app
from consilium.config import get_preset
from consilium.llm import MockProvider
from consilium.llm.base import LLMProvider
from consilium.mcp_server import MCP_AGENT, SkillTools, build_server, caller_text, http_app
from consilium.runtime import Runtime
from consilium.safety import escalation_present
from consilium.skills import SKILL_NAMES, SkillRegistry
from consilium.trace import (
    SafetyEvent,
    ToolCallEvent,
    TraceEvent,
    TurnEvent,
    read_trace,
    trace_path,
)
from tests.conftest import ROOT_DIR

#: How long the subprocess test waits for `consilium mcp-serve` to load the corpus and answer.
#: Generous, because the child indexes 312 chunks before it reads its first frame; the point of the
#: timeout is that a hung child fails the suite instead of hanging it.
STDIO_TIMEOUT_SECONDS = 120.0

#: A question the red-flag table matches, phrased with a pattern string on purpose -- this is a test
#: of the safety envelope, not of the matcher, and the matcher's own coverage is measured elsewhere.
RED_FLAG_QUESTION = "crushing chest pain radiating to my left arm"


@pytest.fixture
def session_id() -> str:
    """A fresh trace session per test, so one test's call indices cannot collide with another's."""
    return f"mcp-test-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def mcp_runtime(offline_runtime: Callable[[LLMProvider], Runtime]) -> Runtime:
    """A runtime on the offline seams. No provider is contacted on this path; one is required
    only because `Runtime` holds one for the layers this server does not use."""
    return offline_runtime(MockProvider([]))


def _events[EventT: TraceEvent](
    runtime: Runtime, session_id: str, index: int, kind: type[EventT]
) -> list[EventT]:
    """One call's trace, filtered to one event class."""
    path = trace_path(runtime.settings.runs_dir, session_id, index)
    return [event for event in read_trace(path) if isinstance(event, kind)]


# ----------------------------------------------------------------------------------------------
# The contract: seven tools, and each schema is the registry's own.
# ----------------------------------------------------------------------------------------------


async def test_every_skill_is_published_as_exactly_one_tool(
    mcp_runtime: Runtime, session_id: str
) -> None:
    async with Client(build_server(mcp_runtime, session_id=session_id)) as client:
        listed = await client.list_tools()

    assert {tool.name for tool in listed.tools} == set(SKILL_NAMES)
    assert len(listed.tools) == len(SKILL_NAMES)


async def test_each_tool_schema_round_trips_through_an_sdk_client(
    mcp_runtime: Runtime, session_id: str
) -> None:
    """Part B's acceptance criterion, asserted per tool against the registry's own schema."""
    async with Client(build_server(mcp_runtime, session_id=session_id)) as client:
        listed = await client.list_tools()

    for tool in listed.tools:
        skill = mcp_runtime.registry.get(tool.name)
        assert tool.input_schema == skill.parameters_schema()
        assert tool.description == skill.description


async def test_the_published_schema_is_not_a_lossy_regeneration(
    mcp_runtime: Runtime, session_id: str
) -> None:
    """The three properties a signature-derived schema loses, checked on the wire.

    Equality against `parameters_schema()` above would pass if both sides were regenerated the same
    lossy way, because the comparison would be a schema against itself. These are the specific
    things the FastMCP decorator path drops -- and `additionalProperties: false` is the one that
    matters most, because publishing it while silently ignoring unknown keys would be a promise to
    an untrusted caller that the code does not keep.
    """
    async with Client(build_server(mcp_runtime, session_id=session_id)) as client:
        listed = await client.list_tools()

    by_name = {tool.name: tool.input_schema for tool in listed.tools}
    assert all(schema["additionalProperties"] is False for schema in by_name.values())
    query = by_name["search_knowledge"]["properties"]["query"]
    assert query["minLength"] == 1
    assert query["description"] == "What to look up, in natural language."


async def test_tools_are_advertised_as_read_only_over_a_closed_corpus(
    mcp_runtime: Runtime, session_id: str
) -> None:
    async with Client(build_server(mcp_runtime, session_id=session_id)) as client:
        listed = await client.list_tools()

    for tool in listed.tools:
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.open_world_hint is False


# ----------------------------------------------------------------------------------------------
# What an invocation writes: the same tool_call event, marked with its transport.
# ----------------------------------------------------------------------------------------------


async def test_an_invocation_emits_the_tool_call_event_marked_mcp(
    mcp_runtime: Runtime, session_id: str
) -> None:
    async with Client(build_server(mcp_runtime, session_id=session_id)) as client:
        result = await client.call_tool("search_knowledge", {"query": "what is hypertension"})

    assert result.is_error is False
    calls = _events(mcp_runtime, session_id, 0, ToolCallEvent)
    assert len(calls) == 1
    call = calls[0]
    assert call.transport == "mcp"
    assert call.agent == MCP_AGENT
    assert call.skill == "search_knowledge"
    assert call.ok is True
    assert call.source_doc_ids


async def test_no_turn_event_is_written_because_a_tool_call_is_not_a_turn(
    mcp_runtime: Runtime, session_id: str
) -> None:
    """`eval/metrics.py` counts turns by this event, so a fabricated one enters its denominators."""
    async with Client(build_server(mcp_runtime, session_id=session_id)) as client:
        await client.call_tool("search_knowledge", {"query": "what is hypertension"})

    assert _events(mcp_runtime, session_id, 0, TurnEvent) == []


async def test_each_call_gets_its_own_trace_file_even_when_they_overlap(
    mcp_runtime: Runtime, session_id: str
) -> None:
    """Two calls in flight must not derive the same index and interleave into one file."""
    async with Client(build_server(mcp_runtime, session_id=session_id)) as client:
        await asyncio.gather(
            *(client.call_tool("search_knowledge", {"query": f"question {n}"}) for n in range(4))
        )

    skills = [
        call.skill
        for index in range(4)
        for call in _events(mcp_runtime, session_id, index, ToolCallEvent)
    ]
    assert skills == ["search_knowledge"] * 4


# ----------------------------------------------------------------------------------------------
# The safety envelope, which is the same validator and the same repair the turn boundary runs.
# ----------------------------------------------------------------------------------------------


async def test_every_result_carries_the_disclaimer(mcp_runtime: Runtime, session_id: str) -> None:
    disclaimer = mcp_runtime.policy.output.required_element("disclaimer").normalized

    async with Client(build_server(mcp_runtime, session_id=session_id)) as client:
        ok = await client.call_tool("search_knowledge", {"query": "what is hypertension"})
        failed = await client.call_tool("search_knowledge", {"query": ""})

    for result in (ok, failed):
        assert disclaimer in _text(result)


async def test_a_red_flag_argument_escalates_even_when_the_skill_failed(
    mcp_runtime: Runtime, session_id: str
) -> None:
    """The banner is decided from the caller's arguments, so a failed call escalates too.

    Deliberately the failure path: it is the case where nothing in the payload could have escalated
    on its own, so the seek-care instruction can only have come from the guard.
    """
    banner = mcp_runtime.policy.output.escalation.text

    async with Client(build_server(mcp_runtime, session_id=session_id)) as client:
        result = await client.call_tool(
            "assess_risk", {"symptoms": RED_FLAG_QUESTION, "unexpected": 1}
        )

    assert result.is_error is True
    assert _text(result).startswith(banner)
    repairs = [
        event.rule
        for event in _events(mcp_runtime, session_id, 0, SafetyEvent)
        if event.event == "repair"
    ]
    assert "escalation_required" in repairs


@pytest.mark.parametrize(
    "skill,arguments",
    [
        ("assess_risk", {"symptoms": RED_FLAG_QUESTION}),
        ("search_knowledge", {"query": RED_FLAG_QUESTION}),
        ("lookup_disease_code", {"condition": RED_FLAG_QUESTION}),
    ],
)
async def test_a_red_flag_argument_always_reaches_the_caller_escalating(
    mcp_runtime: Runtime, session_id: str, skill: str, arguments: dict[str, object]
) -> None:
    """The invariant, whichever half of the guard supplied it.

    Asserted as `escalation_present` on the delivered text rather than as "the banner is there",
    because a result that already tells the caller to seek care needs no banner -- that is why the
    turn event carries three escalation fields instead of counting repairs.
    """
    async with Client(build_server(mcp_runtime, session_id=session_id)) as client:
        result = await client.call_tool(skill, arguments)

    assert escalation_present(_text(result))


async def test_a_repair_is_never_marked_post_stream(mcp_runtime: Runtime, session_id: str) -> None:
    """Nothing is delivered before the guard runs here, so the flag stays False on this path too."""
    async with Client(build_server(mcp_runtime, session_id=session_id)) as client:
        await client.call_tool("assess_risk", {"symptoms": RED_FLAG_QUESTION})

    events = _events(mcp_runtime, session_id, 0, SafetyEvent)
    assert events
    assert all(not event.post_stream for event in events)


# ----------------------------------------------------------------------------------------------
# Failure: a skill never raises into the protocol, exactly as it never raises into the loop.
# ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "skill,arguments,expected",
    [
        ("search_knowledge", {"query": ""}, "invalid arguments"),
        ("search_knowledge", {"unexpected": "x"}, "invalid arguments"),
        ("no_such_skill", {}, "unknown skill"),
    ],
)
async def test_a_bad_call_is_a_traced_failure_and_not_an_exception(
    mcp_runtime: Runtime,
    session_id: str,
    skill: str,
    arguments: dict[str, object],
    expected: str,
) -> None:
    """Refused *by the registry*, which is what puts the refusal in the trace.

    A transport that validated arguments before the handler ran would reject these earlier and emit
    no `tool_call` at all, which would be an MCP invocation invisible to every metric.
    """
    async with Client(build_server(mcp_runtime, session_id=session_id)) as client:
        result = await client.call_tool(skill, arguments)

    assert result.is_error is True
    assert expected in _text(result)

    calls = _events(mcp_runtime, session_id, 0, ToolCallEvent)
    assert len(calls) == 1
    assert calls[0].ok is False
    assert calls[0].transport == "mcp"


def test_the_server_refuses_a_runtime_with_retrieval_switched_off(mcp_runtime: Runtime) -> None:
    """`baseline_llm` is an ablation row, not a servable configuration: nothing would work."""
    with pytest.raises(ValueError, match="retrieval=False"):
        build_server(replace(mcp_runtime, config=get_preset("baseline_llm"), retriever=None))


def test_caller_text_collects_every_string_argument() -> None:
    """Which argument holds the symptom differs per skill, so the matcher is given all of them."""
    assert caller_text({"question": "a", "sub_queries": ["b", "c"], "limit": 3}) == "a\nb\nc"
    assert caller_text({}) == ""


def test_the_session_id_is_minted_when_one_is_not_supplied(mcp_runtime: Runtime) -> None:
    tools = SkillTools(mcp_runtime)
    assert tools.session_id.startswith("mcp-")
    assert tools.session_id != SkillTools(mcp_runtime).session_id


# ----------------------------------------------------------------------------------------------
# The second transport, and the command that starts either one.
# ----------------------------------------------------------------------------------------------


def test_the_streamable_http_app_mounts_the_endpoint(mcp_runtime: Runtime, session_id: str) -> None:
    """The remote transport, asserted as an ASGI app with the endpoint on it.

    Not driven end to end here: the streamable-HTTP session manager needs its own lifespan and a
    running server, which `uvicorn` provides in `consilium mcp-serve --transport http` and an
    in-process test would have to reimplement. What is checkable without that is the wiring, and
    the protocol itself is already asserted over stdio and over the in-memory streams.
    """
    app = http_app(build_server(mcp_runtime, session_id=session_id), path="/mcp")

    assert any(getattr(route, "path", None) == "/mcp" for route in app.routes)


def test_mcp_serve_refuses_an_unknown_transport() -> None:
    """Refused before the corpus is loaded, so a typo costs a second rather than a startup."""
    result = CliRunner().invoke(cli_app, ["mcp-serve", "--transport", "grpc"])

    assert result.exit_code != 0
    assert "--transport must be" in result.output


# ----------------------------------------------------------------------------------------------
# Over real pipes: `consilium mcp-serve` as a host would launch it.
# ----------------------------------------------------------------------------------------------


@pytest.fixture
def stdio_env(tmp_path: Path) -> dict[str, str]:
    """The child's environment: the repository's data, a temporary runs directory, no provider.

    `CONSILIUM_PROVIDER=mock` is set because `build_runtime` builds one whether or not this path
    uses it, and a developer's real key must not decide whether the test passes.
    """
    return {
        **os.environ,
        "CONSILIUM_PROVIDER": "mock",
        "CONSILIUM_RUNS_DIR": str(tmp_path / "runs"),
        "CONSILIUM_CHROMA_DIR": str(tmp_path / "chroma"),
        "CONSILIUM_EPISODIC_DB": str(tmp_path / "episodic.db"),
        "CONSILIUM_LOG_LEVEL": "WARNING",
    }


async def test_schemas_round_trip_over_a_real_stdio_transport(
    stdio_env: dict[str, str], registry: SkillRegistry
) -> None:
    """The same assertion as above, through the console command and two pipes.

    This is the half an in-process client cannot show: that `consilium mcp-serve` starts, speaks
    the protocol on stdin/stdout, and keeps its logs out of the stream it shares with JSON-RPC.
    """
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "consilium.cli",
            "mcp-serve",
            "--transport",
            "stdio",
            "--embedder",
            "hash",
            "--store",
            "numpy",
            "--session",
            "mcp-stdio-test",
        ],
        env=stdio_env,
        cwd=str(ROOT_DIR),
    )

    async def talk() -> None:
        async with Client(parameters) as client:
            listed = await client.list_tools()
            assert {tool.name for tool in listed.tools} == set(SKILL_NAMES)
            for tool in listed.tools:
                assert tool.input_schema == registry.get(tool.name).parameters_schema()

            result = await client.call_tool("lookup_disease_code", {"condition": "hypertension"})
            assert result.is_error is False
            assert "I10" in _text(result)

    await asyncio.wait_for(talk(), timeout=STDIO_TIMEOUT_SECONDS)


def _text(result: CallToolResult) -> str:
    """The text of a single-block `CallToolResult`, which is all this server ever returns."""
    assert len(result.content) == 1
    block = result.content[0]
    assert block.type == "text"
    return block.text
