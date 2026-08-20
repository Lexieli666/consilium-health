"""Interface: the terminal entry point.

Phase 2 landed ``ingest``; Phase 4 adds ``ask`` and ``trace``.  ``chat`` and ``eval`` arrive with
the layers they drive, in Phases 8 and 9; a command that shells out to a module that does not exist
yet is worse than a missing command.

Output here goes through ``typer.echo`` rather than the logger.  The two are not interchangeable and
the distinction is the same one ``consilium/log.py`` draws: structlog writes operational records for
a log aggregator, and this is the answer a person typed a command to receive.  Routing a report
through the logger would put it behind a level filter and wrap it in JSON.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Annotated

import typer

from consilium.agents import AGENT_TYPES
from consilium.config import Settings, get_preset
from consilium.llm.factory import ProviderError
from consilium.log import bind_turn, configure_logging, get_logger
from consilium.retrieval.corpus import CorpusError
from consilium.retrieval.index import EmbedderName, StoreName, ingest, make_embedder, make_store
from consilium.runtime import TurnOutcome, build_runtime, run_turn
from consilium.trace import (
    LLMCallEvent,
    RetrievalEvent,
    RouteEvent,
    SafetyEvent,
    ToolCallEvent,
    TraceError,
    Tracer,
    TurnEvent,
    read_trace,
    trace_path,
)

app = typer.Typer(
    name="consilium",
    help="Multi-agent clinical-information assistant. Educational software; not medical advice.",
    no_args_is_help=True,
    add_completion=False,
)

log = get_logger(__name__)

#: The specialists `--agent` accepts, rendered for help text and error messages.
AGENT_NAMES = ", ".join(sorted(AGENT_TYPES))


@app.callback()
def main() -> None:
    """Group the commands under `consilium <command>`.

    Typer collapses a single-command app into a bare top-level command, which would make
    `consilium ingest` an error today and silently rename every invocation once the second command
    lands.  An explicit callback pins the multi-command shape from the start.
    """


# Named `ingest_corpus` so the function does not shadow the pipeline function it calls; Typer
# takes the command name from the decorator rather than from the identifier.
@app.command(name="ingest")
def ingest_corpus(
    corpus_dir: Annotated[
        Path | None,
        typer.Option("--corpus-dir", help="Directory of corpus notes. Defaults to the setting."),
    ] = None,
    store: Annotated[
        str, typer.Option("--store", help="Vector store: chroma (persistent) or numpy (memory).")
    ] = "chroma",
    embedder: Annotated[
        str, typer.Option("--embedder", help="Embedder: bge (real) or hash (offline, no download).")
    ] = "bge",
    chroma_dir: Annotated[
        Path | None,
        typer.Option("--chroma-dir", help="Where the chroma store persists. Defaults to setting."),
    ] = None,
) -> None:
    """Load, chunk, embed and index the corpus.

    The defaults are the real pipeline -- bge embeddings into a persistent Chroma store -- because
    that is what produces the retrieval numbers.  ``--embedder hash --store numpy`` runs the same
    pipeline with no download and no persistence, which is what the offline test suite uses and
    what makes the two protocol seams checkable by hand.
    """
    settings = Settings.from_env()
    configure_logging(settings.log_level, settings.log_format)

    store_name, embedder_name = _pipeline_names(store, embedder)

    source = corpus_dir or settings.corpus_dir
    destination = chroma_dir or settings.chroma_dir

    try:
        built_embedder = make_embedder(embedder_name)
        built_store = make_store(store_name, path=destination)
    except ImportError as exc:  # the [embeddings] extra is not installed
        raise typer.BadParameter(
            f"{exc}. Install the optional extra with `uv sync --extra embeddings`, or run "
            "`--embedder hash --store numpy` for the offline pipeline."
        ) from exc

    try:
        _, report = ingest(corpus_dir=source, embedder=built_embedder, store=built_store)
    except CorpusError as exc:
        # `log.error`, not `log.exception`: a malformed corpus note is a user error with a precise
        # message, and a stack trace through the loader would bury it.  The exception is still
        # chained onto the Exit for anything reading the cause.
        log.error("ingest.failed", error=str(exc))  # noqa: TRY400
        raise typer.Exit(code=1) from exc

    log.info(
        "ingest.complete",
        documents=report.documents,
        chunks=report.chunks,
        embedder=report.embedder,
        store=report.store,
    )
    typer.echo(report.summary())


@app.command(name="ask")
def ask(
    question: Annotated[str, typer.Argument(help="The question to answer.")],
    agent: Annotated[
        str | None,
        typer.Option(
            "--agent",
            help=f"Pin one specialist and skip routing. One of: {AGENT_NAMES}.",
        ),
    ] = None,
    config_name: Annotated[
        str, typer.Option("--config", help="Run configuration preset, e.g. full or baseline_llm.")
    ] = "full",
    session: Annotated[
        str | None, typer.Option("--session", help="Session id. A random one is used if omitted.")
    ] = None,
    turn_index: Annotated[int, typer.Option("--turn", help="Turn index within the session.")] = 0,
    script: Annotated[
        Path | None,
        typer.Option("--script", help="YAML script for the mock provider (offline demo/tests)."),
    ] = None,
    store: Annotated[
        str, typer.Option("--store", help="Vector store: chroma (persistent) or numpy (memory).")
    ] = "chroma",
    embedder: Annotated[
        str, typer.Option("--embedder", help="Embedder: bge (real) or hash (offline, no download).")
    ] = "bge",
) -> None:
    """Answer one question and write the turn's trace.

    The planner decides which specialists answer. `--agent` pins one and skips routing, which is a
    debugging affordance rather than a mode: no measured run uses it.
    """
    settings = Settings.from_env()
    configure_logging(settings.log_level, settings.log_format)

    if agent is not None and agent not in AGENT_TYPES:
        raise typer.BadParameter(f"--agent must be one of {AGENT_NAMES}; got {agent!r}")
    store_name, embedder_name = _pipeline_names(store, embedder)

    try:
        runtime = build_runtime(
            settings,
            config=get_preset(config_name),
            script=script,
            embedder=embedder_name,
            store=store_name,
        )
    except (CorpusError, ProviderError, KeyError) as exc:
        log.error("ask.setup_failed", error=str(exc))  # noqa: TRY400
        raise typer.Exit(code=1) from exc
    except ImportError as exc:  # the [embeddings] extra is not installed
        raise typer.BadParameter(
            f"{exc}. Install the optional extra with `uv sync --extra embeddings`, or run "
            "`--embedder hash --store numpy` for the offline pipeline."
        ) from exc

    session_id = session or f"cli-{uuid.uuid4().hex[:12]}"
    with Tracer.for_turn(
        session_id=session_id, turn_index=turn_index, runs_dir=settings.runs_dir
    ) as tracer:
        bind_turn(session_id=session_id, trace_id=tracer.trace_id, turn_index=turn_index)
        outcome = asyncio.run(run_turn(runtime, question, tracer=tracer, agent=agent))

    typer.echo(outcome.answer)
    typer.echo("")
    agents = ", ".join(outcome.agents)
    typer.echo(f"route      : {outcome.mode} [{agents}]{_routing_notes(outcome)}")
    typer.echo(f"risk level : {outcome.risk_level}")
    typer.echo(f"sources    : {', '.join(outcome.sources) or 'none'}")
    typer.echo(f"trace      : {trace_path(settings.runs_dir, session_id, turn_index)}")


@app.command(name="trace")
def show_trace(
    session: Annotated[str, typer.Argument(help="Session id, the directory under runs/.")],
    turn_index: Annotated[int, typer.Option("--turn", help="Which turn of the session.")] = 0,
    runs_dir: Annotated[
        Path | None, typer.Option("--runs-dir", help="Where traces live. Defaults to the setting.")
    ] = None,
) -> None:
    """Pretty-print one turn's trace.

    Reads the JSONL back through the same Pydantic models that wrote it, so a malformed record is
    reported with its line number rather than rendered as whatever it happens to contain.  That is
    the point of the command: it is a check on the artifact every reported number is derived from,
    not a log viewer.
    """
    settings = Settings.from_env()
    path = trace_path(runs_dir or settings.runs_dir, session, turn_index)
    if not path.exists():
        typer.echo(f"no trace at {path}", err=True)
        raise typer.Exit(code=1)

    try:
        events = read_trace(path)
    except TraceError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"{path}  ({len(events)} events)")
    for event in events:
        typer.echo(f"  {event.ts.time().isoformat(timespec='milliseconds')}  {_render(event)}")


def _render(event: object) -> str:
    """One line per event, showing the fields a reader actually checks."""
    if isinstance(event, RouteEvent):
        return (
            f"route       mode={event.mode} agents={','.join(event.agents)} "
            f"fallback={event.fallback}"
        )
    if isinstance(event, LLMCallEvent):
        return (
            f"llm_call    caller={event.caller} model={event.model} "
            f"tokens={event.prompt_tokens}+{event.completion_tokens} stop={event.stop_reason}"
        )
    if isinstance(event, ToolCallEvent):
        status = "ok" if event.ok else f"FAILED ({event.error})"
        return (
            f"tool_call   {event.agent}/{event.skill} {status} "
            f"{event.latency_ms:.0f}ms sources={','.join(event.source_doc_ids) or '-'}"
        )
    if isinstance(event, RetrievalEvent):
        top = ", ".join(f"{hit.doc_id}#{hit.chunk_index}" for hit in event.fused_topk[:5])
        return (
            f"retrieval   {event.skill} filter={event.category_filter or '-'} "
            f"k={event.returned_k}\n              top5: {top}"
        )
    if isinstance(event, SafetyEvent):
        return f"safety      {event.event} rule={event.rule} scope={event.scope} {event.detail}"
    if isinstance(event, TurnEvent):
        return (
            f"turn        risk={event.risk_level} wall={event.wall_ms:.0f}ms "
            f"red_flag={event.red_flag_matched} escalated={event.escalation_present_post_repair} "
            f"repaired={event.repair_applied}"
        )
    return f"blackboard  {getattr(event, 'event', '?')} {getattr(event, 'subtask_id', '')}"


def _routing_notes(outcome: TurnOutcome) -> str:
    """Flag a planner fallback or a missing perspective in the CLI output.

    Printed because both are things a reader should not have to open the trace to discover: a
    fallback means the plan was unusable, and a missing perspective means the answer is partial.
    """
    notes = []
    if outcome.fallback:
        notes.append("planner fallback")
    if outcome.missing:
        notes.append(f"missing: {', '.join(outcome.missing)}")
    return f"  ({'; '.join(notes)})" if notes else ""


def _pipeline_names(store: str, embedder: str) -> tuple[StoreName, EmbedderName]:
    """Validate the two pipeline flags shared by `ingest` and `ask`."""
    if store not in ("chroma", "numpy"):
        raise typer.BadParameter(f"--store must be 'chroma' or 'numpy'; got {store!r}")
    if embedder not in ("bge", "hash"):
        raise typer.BadParameter(f"--embedder must be 'bge' or 'hash'; got {embedder!r}")
    store_name: StoreName = "chroma" if store == "chroma" else "numpy"
    embedder_name: EmbedderName = "bge" if embedder == "bge" else "hash"
    return store_name, embedder_name


if __name__ == "__main__":  # pragma: no cover - exercised by the console script
    app()
