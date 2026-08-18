"""The ingest pipeline and the `consilium ingest` command.

The counts asserted here are the ones `docs/CORPUS.md` publishes, so this file is what keeps that
document honest: if a note is added or the chunking changes, the documented numbers fail here rather
than quietly becoming wrong.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from consilium.cli import app
from consilium.retrieval import (
    Bm25Index,
    HashEmbedder,
    NumpyStore,
    ingest,
    make_embedder,
    make_store,
)
from consilium.retrieval.corpus import CorpusError

CORPUS_DIR = Path(__file__).resolve().parents[1] / "data" / "corpus"

runner = CliRunner()


def test_ingesting_the_corpus_produces_the_documented_counts() -> None:
    retriever, report = ingest(corpus_dir=CORPUS_DIR, embedder=HashEmbedder(), store=NumpyStore())

    assert report.documents == 78
    assert report.chunks == 312
    assert set(report.chunks_by_category) == {
        "coding",
        "condition",
        "guideline",
        "lifestyle",
        "red_flag",
    }
    assert sum(report.chunks_by_category.values()) == report.chunks
    assert retriever.store.count() == report.chunks
    assert retriever.lexical.count() == report.chunks


def test_the_retriever_it_returns_actually_retrieves() -> None:
    retriever, _ = ingest(corpus_dir=CORPUS_DIR, embedder=HashEmbedder(), store=NumpyStore())

    hits = retriever.search(
        "essential hypertension", skill="lookup_disease_code", category="coding"
    )

    assert hits
    assert {hit.chunk.category for hit in hits} == {"coding"}


def test_re_ingesting_resets_rather_than_duplicating() -> None:
    """A persistent store outlives the process; leaving the previous chunking of an edited note in
    the index produces stale hits that read as a retrieval-quality problem."""
    store = NumpyStore()
    lexical = Bm25Index()

    _, first = ingest(
        corpus_dir=CORPUS_DIR, embedder=HashEmbedder(), store=store, lexical=Bm25Index()
    )
    _, second = ingest(corpus_dir=CORPUS_DIR, embedder=HashEmbedder(), store=store, lexical=lexical)

    assert first.chunks == second.chunks
    assert store.count() == second.chunks


def test_the_report_summarizes_what_was_built() -> None:
    _, report = ingest(corpus_dir=CORPUS_DIR, embedder=HashEmbedder(), store=NumpyStore())

    summary = report.summary()

    assert "78 documents -> 312 chunks" in summary
    assert "embedder=hash-384" in summary
    assert "store=numpy" in summary


def test_ingesting_a_bad_corpus_raises_rather_than_indexing_half_of_it(tmp_path: Path) -> None:
    (tmp_path / "condition-broken.md").write_text("no front matter here\n", encoding="utf-8")

    with pytest.raises(CorpusError):
        ingest(corpus_dir=tmp_path, embedder=HashEmbedder(), store=NumpyStore())


def test_make_embedder_returns_the_offline_implementation_by_name() -> None:
    assert isinstance(make_embedder("hash"), HashEmbedder)


@pytest.mark.parametrize("name", ["word2vec", ""])
def test_an_unknown_embedder_name_is_rejected(name: str) -> None:
    with pytest.raises(ValueError, match="unknown embedder"):
        make_embedder(name)  # type: ignore[arg-type]  # the point is the runtime guard


# --------------------------------------------------------------------------------------------
# The CLI.
# --------------------------------------------------------------------------------------------


def test_the_cli_ingests_offline() -> None:
    """`--embedder hash --store numpy` is the whole pipeline with no download and no persistence."""
    result = runner.invoke(app, ["ingest", "--embedder", "hash", "--store", "numpy"])

    assert result.exit_code == 0, result.output
    assert "78 documents -> 312 chunks" in result.output


def test_the_cli_accepts_a_corpus_directory(tmp_path: Path) -> None:
    note = (CORPUS_DIR / "condition-asthma.md").read_text(encoding="utf-8")
    (tmp_path / "condition-asthma.md").write_text(note, encoding="utf-8")

    result = runner.invoke(
        app,
        ["ingest", "--embedder", "hash", "--store", "numpy", "--corpus-dir", str(tmp_path)],
    )

    assert result.exit_code == 0, result.output
    assert "1 documents -> 4 chunks" in result.output


def test_the_cli_reports_a_bad_corpus_and_exits_nonzero(tmp_path: Path) -> None:
    (tmp_path / "condition-broken.md").write_text("not a note\n", encoding="utf-8")

    result = runner.invoke(
        app,
        ["ingest", "--embedder", "hash", "--store", "numpy", "--corpus-dir", str(tmp_path)],
    )

    assert result.exit_code == 1


@pytest.mark.parametrize(("flag", "value"), [("--store", "milvus"), ("--embedder", "word2vec")])
def test_the_cli_rejects_an_unknown_backend(flag: str, value: str) -> None:
    result = runner.invoke(app, ["ingest", flag, value])

    assert result.exit_code != 0


def test_the_cli_lists_ingest_as_a_subcommand() -> None:
    """Typer collapses a single-command app into a bare command; the callback pins the shape."""
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "ingest" in result.output


def test_make_store_numpy_needs_no_path() -> None:
    assert isinstance(make_store("numpy"), NumpyStore)
