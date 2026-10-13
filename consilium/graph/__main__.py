"""``python -m consilium.graph "<question>"``: one turn through the LangGraph orchestrator.

The acceptance check for the graph, runnable with a key (``CONSILIUM_PROVIDER=openai``) or without
one (``--mock``, which replays ``consilium/graph/mock_script.yaml`` and needs no network).  It
goes through :func:`consilium.runtime.run_turn` -- the same turn boundary the CLI, the API and the
evaluation harness use -- and then prints the trace it wrote, so that "called at least one MCP tool
and the validator ran" is read off the evidence rather than asserted.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path

from consilium.config import Settings, get_preset
from consilium.graph.tracing import configure_langsmith
from consilium.log import bind_turn, configure_logging
from consilium.runtime import build_runtime, run_turn
from consilium.trace import (
    BlackboardEvent,
    LLMCallEvent,
    RetrievalEvent,
    SafetyEvent,
    ToolCallEvent,
    Tracer,
    TurnEvent,
    read_trace,
    trace_path,
)

MOCK_SCRIPT = Path(__file__).with_name("mock_script.yaml")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m consilium.graph",
        description="Answer one question through the LangGraph orchestrator and show its trace.",
    )
    parser.add_argument("question")
    parser.add_argument("--config", default="full", help="Run configuration preset.")
    parser.add_argument("--session", default=None, help="Session id (thread id). Random if unset.")
    parser.add_argument("--turn", type=int, default=0, help="Turn index within the session.")
    parser.add_argument("--embedder", default=None, choices=["bge", "hash"])
    parser.add_argument("--store", default=None, choices=["chroma", "numpy"])
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Offline: mock provider with the bundled script, hash embedder, numpy store.",
    )
    parser.add_argument("--script", type=Path, default=None, help="A YAML mock script.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    os.environ["ORCHESTRATOR"] = "langgraph"
    if args.mock:
        os.environ["CONSILIUM_PROVIDER"] = "mock"
    settings = Settings.from_env()
    configure_logging(settings.log_level, settings.log_format, stream=sys.stderr)
    langsmith_on = configure_langsmith()

    embedder = args.embedder or ("hash" if args.mock else "bge")
    store = args.store or ("numpy" if args.mock else "chroma")
    script = args.script or (MOCK_SCRIPT if args.mock else None)
    runtime = build_runtime(
        settings,
        config=get_preset(args.config),
        script=script,
        embedder=embedder,  # type: ignore[arg-type]  # validated by argparse choices
        store=store,  # type: ignore[arg-type]
    )

    session_id = args.session or f"graph-{uuid.uuid4().hex[:12]}"
    with Tracer.for_turn(
        session_id=session_id, turn_index=args.turn, runs_dir=settings.runs_dir
    ) as tracer:
        bind_turn(session_id=session_id, trace_id=tracer.trace_id, turn_index=args.turn)
        outcome = asyncio.run(_run(runtime, args.question, tracer))

    path = trace_path(settings.runs_dir, session_id, args.turn)
    print(outcome.answer)
    print()
    print(f"orchestrator : langgraph (mcp={settings.graph_mcp}, provider={settings.provider})")
    print(f"risk level   : {outcome.risk_level}")
    print(f"sources      : {', '.join(outcome.sources) or 'none'}")
    violations = [violation.rule for violation in outcome.safety.violations]
    print(f"safety       : violations={violations} repairs={list(outcome.safety.repairs)}")
    langsmith = f"on, project {os.environ.get('LANGSMITH_PROJECT', '')}" if langsmith_on else "off"
    print(f"langsmith    : {langsmith}")
    print(f"trace        : {path}")
    print()
    print("trace events:")
    for event in read_trace(path):
        print(f"  {_line(event)}")
    return 0


async def _run(runtime, question: str, tracer: Tracer):  # type: ignore[no-untyped-def]
    from consilium.graph import close_all

    try:
        return await run_turn(runtime, question, tracer=tracer)
    finally:
        await close_all()


def _line(event: object) -> str:
    if isinstance(event, LLMCallEvent):
        return (
            f"llm_call    caller={event.caller} model={event.model} "
            f"tokens={event.prompt_tokens}+{event.completion_tokens} "
            f"tools_offered={len(event.tools_offered)} stop={event.stop_reason}"
        )
    if isinstance(event, ToolCallEvent):
        return (
            f"tool_call   skill={event.skill} transport={event.transport} ok={event.ok} "
            f"sources={list(event.source_doc_ids)}"
        )
    if isinstance(event, RetrievalEvent):
        top = [hit.doc_id for hit in event.fused_topk[:5]]
        return f"retrieval   skill={event.skill} query={event.query!r} top5={top}"
    if isinstance(event, SafetyEvent):
        return f"safety      {event.event} rule={event.rule} scope={event.scope}"
    if isinstance(event, BlackboardEvent):
        return f"blackboard  {event.event} {event.subtask_id}"
    if isinstance(event, TurnEvent):
        return (
            f"turn        risk={event.risk_level} wall_ms={event.wall_ms:.0f} "
            f"escalation(pre={event.escalation_present_pre_repair}, "
            f"post={event.escalation_present_post_repair}) repair_applied={event.repair_applied}"
        )
    return f"{getattr(event, 'type', type(event).__name__)}"


if __name__ == "__main__":
    sys.exit(main())
