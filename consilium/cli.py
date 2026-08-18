"""Interface: the terminal entry point.

Phase 2 lands ``ingest``.  ``ask``, ``chat``, ``eval`` and ``trace`` arrive with the layers they
drive, in Phases 4, 8 and 9; a command that shells out to a module that does not exist yet is worse
than a missing command.

Output here goes through ``typer.echo`` rather than the logger.  The two are not interchangeable and
the distinction is the same one ``consilium/log.py`` draws: structlog writes operational records for
a log aggregator, and this is the answer a person typed a command to receive.  Routing a report
through the logger would put it behind a level filter and wrap it in JSON.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from consilium.config import Settings
from consilium.log import configure_logging, get_logger
from consilium.retrieval.corpus import CorpusError
from consilium.retrieval.index import EmbedderName, StoreName, ingest, make_embedder, make_store

app = typer.Typer(
    name="consilium",
    help="Multi-agent clinical-information assistant. Educational software; not medical advice.",
    no_args_is_help=True,
    add_completion=False,
)

log = get_logger(__name__)


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

    if store not in ("chroma", "numpy"):
        raise typer.BadParameter(f"--store must be 'chroma' or 'numpy'; got {store!r}")
    if embedder not in ("bge", "hash"):
        raise typer.BadParameter(f"--embedder must be 'bge' or 'hash'; got {embedder!r}")
    store_name: StoreName = "chroma" if store == "chroma" else "numpy"
    embedder_name: EmbedderName = "bge" if embedder == "bge" else "hash"

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


if __name__ == "__main__":  # pragma: no cover - exercised by the console script
    app()
