"""Interface: the terminal entry point.

Phase 2 landed ``ingest``; Phase 4 added ``ask`` and ``trace``; Phase 8 added ``eval``; Phase 9
adds ``chat``.  Each arrived with the layer it drives, because a command that shells out to a module
that does not exist yet is worse than a missing command.

Output here goes through ``typer.echo`` rather than the logger.  The two are not interchangeable and
the distinction is the same one ``consilium/log.py`` draws: structlog writes operational records for
a log aggregator, and this is the answer a person typed a command to receive.  Routing a report
through the logger would put it behind a level filter and wrap it in JSON.
"""

from __future__ import annotations

import asyncio
import shutil
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
from consilium.runtime import Runtime, TurnOutcome, build_runtime, run_turn
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

    _echo_outcome(outcome, trace_path(settings.runs_dir, session_id, turn_index))


#: Printed once when the REPL starts.  The disclaimer is repeated here rather than left to the
#: answers: a user who types a question before reading an answer has still seen it.
CHAT_HEADER = (
    "consilium chat -- educational software, not medical advice. It does not diagnose or treat,\n"
    "and it must not be used for real medical decisions. Type /help for commands, /exit to leave."
)

#: The REPL's own commands.  Deliberately few: this is a way to exercise a multi-turn session by
#: hand, not a shell.
CHAT_COMMANDS = {
    "/exit": "end the session",
    "/quit": "end the session",
    "/help": "show these commands",
    "/session": "show the session id, the turn count, and where the traces are",
}


@app.command(name="chat")
def chat(
    session: Annotated[
        str | None,
        typer.Option("--session", help="Session id. A random one is used if omitted."),
    ] = None,
    config_name: Annotated[
        str, typer.Option("--config", help="Run configuration preset, e.g. full or baseline_llm.")
    ] = "full",
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
    """Hold a multi-turn conversation.

    **One session id for the whole REPL, and one memory path.**  The session's `WorkingMemory` is
    the one `run_turn` fetches from `Runtime.memory` by session id -- the same store the API and the
    evaluation harness use, keyed the same way.  The REPL keeps no conversation state of its own,
    which is what makes the multi-turn golden set an exercise of this code path rather than of a
    second one written for the terminal.

    `--session` also continues the *trace* numbering: the next turn is written after the highest
    turn already on disk for that id, so resuming an id does not append a second turn's events to
    the first turn's file. Conversation history is a different artifact and lives in the memory
    store, which is in-process by default and therefore does not survive leaving the REPL.
    """
    settings = Settings.from_env()
    configure_logging(settings.log_level, settings.log_format)
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
        log.error("chat.setup_failed", error=str(exc))  # noqa: TRY400
        raise typer.Exit(code=1) from exc
    except ImportError as exc:  # the [embeddings] extra is not installed
        raise typer.BadParameter(
            f"{exc}. Install the optional extra with `uv sync --extra embeddings`, or run "
            "`--embedder hash --store numpy` for the offline pipeline."
        ) from exc

    session_id = session or f"chat-{uuid.uuid4().hex[:12]}"
    turn_index = _first_free_turn(settings.runs_dir, session_id)

    typer.echo(CHAT_HEADER)
    typer.echo(f"session    : {session_id}")
    if not runtime.config.memory:
        # Said rather than refused: the preset is valid and the turns still run, but each one starts
        # from nothing, so a reader who typed a follow-up would otherwise read the answer as a
        # memory failure.
        typer.echo(
            f"note       : preset {runtime.config.name!r} runs with memory off; "
            "turns do not see each other."
        )
    typer.echo("")

    while True:
        try:
            question = typer.prompt("you", default="", show_default=False, prompt_suffix=" > ")
        except (typer.Abort, EOFError):  # ctrl-D, or a piped stdin that ran out
            typer.echo("")
            break

        question = question.strip()
        if not question:
            continue
        if question.startswith("/"):
            if question in ("/exit", "/quit"):
                break
            _chat_command(question, runtime, settings, session_id, turn_index)
            continue

        with Tracer.for_turn(
            session_id=session_id, turn_index=turn_index, runs_dir=settings.runs_dir
        ) as tracer:
            bind_turn(session_id=session_id, trace_id=tracer.trace_id, turn_index=turn_index)
            outcome = asyncio.run(run_turn(runtime, question, tracer=tracer))

        typer.echo("")
        _echo_outcome(outcome, trace_path(settings.runs_dir, session_id, turn_index))
        typer.echo("")
        turn_index += 1

    typer.echo(f"session {session_id} ended after {turn_index} turn(s).")


def _chat_command(
    command: str, runtime: Runtime, settings: Settings, session_id: str, turn_index: int
) -> None:
    """Handle one slash command.  Unknown ones are reported, not answered as questions."""
    if command == "/help":
        for name, description in CHAT_COMMANDS.items():
            typer.echo(f"  {name:<10} {description}")
        return
    if command == "/session":
        turns = len(runtime.memory.get(session_id)) if runtime.config.memory else 0
        typer.echo(f"  session    : {session_id}")
        typer.echo(f"  turns      : {turns} recorded, next trace index {turn_index}")
        typer.echo(f"  traces     : {settings.runs_dir / session_id}")
        return
    typer.echo(f"  unknown command {command}; /help lists them")


def _first_free_turn(runs_dir: Path, session_id: str) -> int:
    """The first turn index with no trace file, so resuming a session id does not overwrite one.

    Read from the filesystem rather than from the memory store because the two answer different
    questions: the store knows how much of the conversation this process remembers, and the runs
    directory knows how many turns have ever been written under this id.  A trace sink appends, so
    starting again at 0 would interleave two turns' events in one file.
    """
    directory = Path(runs_dir) / session_id
    if not directory.is_dir():
        return 0
    used = {int(path.stem) for path in directory.glob("*.jsonl") if path.stem.isdigit()}
    return max(used) + 1 if used else 0


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


@app.command(
    name="eval",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    add_help_option=False,
)
def run_eval(ctx: typer.Context) -> None:
    """Run the evaluation sweep. Forwards every argument to `eval/run.py`.

    The harness is imported lazily and only here. It lives at the top level rather than inside the
    package because it depends on the golden set and on a live provider, neither of which belongs in
    an installed wheel -- so an installed copy of `consilium` has this command and not the module
    behind it, and the error says so rather than raising ImportError at startup.

    It requires a live API key and costs money. `--limit` first.
    """
    try:
        from eval.run import cli as eval_cli
    except ImportError as exc:  # pragma: no cover - depends on how the package was installed
        typer.echo(
            "the evaluation harness ships with the repository, not with the wheel. "
            "Run it from a checkout: `python -m eval.run --help`.",
            err=True,
        )
        raise typer.Exit(code=2) from exc

    raise typer.Exit(code=eval_cli(list(ctx.args)))


runs_app = typer.Typer(help="Manage trace files under runs/.", no_args_is_help=True)
app.add_typer(runs_app, name="runs")


@runs_app.command(name="purge")
def purge_runs(
    session: Annotated[
        str | None,
        typer.Option("--session", help="Purge one session. Omit to purge every trace."),
    ] = None,
    runs_dir: Annotated[
        Path | None, typer.Option("--runs-dir", help="Where traces live. Defaults to the setting.")
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", help="Do not prompt.")] = False,
) -> None:
    """Delete trace files.

    Traces hold verbatim user questions and full prompts, so they are the most sensitive artifact
    this project writes even though it forbids real patient data. This command is the retention
    mechanism docs/SAFETY.md documents; without it the retention rule would be a sentence with
    nothing behind it.

    It refuses to delete anything outside the configured runs directory, and it prompts unless
    `--yes` is passed: deleting traces destroys the evidence behind every number computed from them.
    """
    settings = Settings.from_env()
    configure_logging(settings.log_level, settings.log_format)
    root = (runs_dir or settings.runs_dir).resolve()

    if not root.exists():
        typer.echo(f"nothing to purge: {root} does not exist")
        return

    targets = [root / session] if session else sorted(p for p in root.iterdir() if p.is_dir())
    for target in targets:
        # A session id could otherwise walk out of the runs directory.  `Tracer` and the memory
        # store both validate the same pattern; this is the third place the same input is used, so
        # it gets the same check rather than trusting the two upstream ones.
        if not target.resolve().is_relative_to(root):
            raise typer.BadParameter(f"{target} is outside {root}")

    existing = [target for target in targets if target.exists()]
    if not existing:
        typer.echo(f"nothing to purge under {root}")
        return

    files = sum(len(list(target.glob("*.jsonl"))) for target in existing)
    if not yes:
        typer.confirm(
            f"delete {files} trace file(s) in {len(existing)} session(s) under {root}?", abort=True
        )

    for target in existing:
        shutil.rmtree(target)
    log.info("runs.purged", sessions=len(existing), files=files, runs_dir=str(root))
    typer.echo(f"purged {files} trace file(s) from {len(existing)} session(s)")


def _echo_outcome(outcome: TurnOutcome, trace_file: Path) -> None:
    """Print one turn's answer and the four facts that make it checkable.

    Shared by `ask` and `chat` so the two cannot drift into reporting different things about the
    same object -- which document grounded the answer, who wrote it, how urgent the input was, and
    where the evidence is on disk.
    """
    typer.echo(outcome.answer)
    typer.echo("")
    agents = ", ".join(outcome.agents)
    typer.echo(f"route      : {outcome.mode} [{agents}]{_routing_notes(outcome)}")
    typer.echo(f"risk level : {outcome.risk_level}")
    typer.echo(f"sources    : {', '.join(outcome.sources) or 'none'}")
    typer.echo(f"safety     : {_safety_notes(outcome)}")
    typer.echo(f"trace      : {trace_file}")


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


def _safety_notes(outcome: TurnOutcome) -> str:
    """What the guard found and what it did, as two counts rather than one.

    Violations and repairs are reported separately here for the same reason they are two rates in
    docs/EVALUATION.md: one says the model produced non-compliant output, the other says the guard
    had to act, and merging them hides a model getting worse behind a guard that kept working.
    """
    from consilium.safety import violation_rules

    found = violation_rules(outcome.safety.violations)
    if not found and not outcome.safety.repairs:
        return "no violations"
    return (
        f"violations: {', '.join(found) or 'none'}; "
        f"repairs: {', '.join(outcome.safety.repairs) or 'none'}"
    )


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
