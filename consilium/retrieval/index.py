"""Substrate: building a retriever from the corpus on disk.

One function, :func:`ingest`, does the whole pipeline -- load, chunk, embed, index -- and returns
the retriever alongside an :class:`IngestReport` describing what it built.  The report exists so
that ``consilium ingest`` can print counts a reader can check against ``docs/CORPUS.md`` rather than
printing "done".

The two ``make_*`` factories are also where the protocol conformance of the optional
implementations is checked.  Both are annotated as returning the protocol type, so mypy verifies in
CI that ``ChromaStore`` satisfies ``VectorStore`` and ``BgeEmbedder`` satisfies ``Embedder`` --
without ``chromadb`` or ``sentence-transformers`` being installed, because a structural check needs
the source, not the dependency.  That is the seam doing real work in the environment that never has
the extra.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from consilium.retrieval.bm25 import Bm25Index
from consilium.retrieval.chunking import chunk_corpus
from consilium.retrieval.corpus import load_corpus
from consilium.retrieval.embedder import BgeEmbedder, Embedder, HashEmbedder
from consilium.retrieval.hybrid import HybridRetriever
from consilium.retrieval.store import ChromaStore, NumpyStore, VectorStore
from consilium.retrieval.types import Category, Chunk

EmbedderName = Literal["bge", "hash"]
StoreName = Literal["chroma", "numpy"]


class IngestReport(BaseModel):
    """What one ingestion run produced.  Printed by the CLI, asserted by tests."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    corpus_dir: Path
    documents: int
    chunks: int
    chunks_by_category: dict[str, int]
    embedder: str
    store: str

    def summary(self) -> str:
        by_category = ", ".join(
            f"{category}={count}" for category, count in sorted(self.chunks_by_category.items())
        )
        return (
            f"{self.documents} documents -> {self.chunks} chunks ({by_category})\n"
            f"embedder={self.embedder} store={self.store} corpus={self.corpus_dir}"
        )


def make_embedder(name: EmbedderName, **kwargs: object) -> Embedder:
    """Construct an embedder by name.  Annotated as the protocol so mypy checks conformance."""
    if name == "hash":
        return HashEmbedder()
    if name == "bge":
        return BgeEmbedder(**kwargs)  # type: ignore[arg-type]  # forwarded to a typed signature
    raise ValueError(f"unknown embedder {name!r}; expected 'bge' or 'hash'")


def make_store(name: StoreName, *, path: Path | None = None) -> VectorStore:
    """Construct a vector store by name.  Annotated as the protocol so mypy checks conformance."""
    if name == "numpy":
        return NumpyStore()
    if name == "chroma":
        if path is None:
            raise ValueError("the chroma store needs a path to persist to")
        return ChromaStore(path=path)
    raise ValueError(f"unknown store {name!r}; expected 'chroma' or 'numpy'")


def build_chunks(corpus_dir: Path) -> tuple[int, list[Chunk]]:
    """Load and chunk the corpus, returning the document count alongside the chunks."""
    documents = load_corpus(corpus_dir)
    return len(documents), chunk_corpus(documents)


def index_chunks(
    chunks: Sequence[Chunk], *, embedder: Embedder, store: VectorStore, lexical: Bm25Index
) -> None:
    """Embed and index chunks into an already-constructed store and lexical index."""
    if not chunks:
        return
    store.add(chunks, embedder.embed_documents([chunk.text for chunk in chunks]))
    lexical.add(chunks)


def open_retriever(
    *,
    corpus_dir: Path,
    embedder: Embedder,
    store: VectorStore,
    lexical: Bm25Index | None = None,
) -> tuple[HybridRetriever, IngestReport]:
    """Build a retriever, re-embedding only if the store does not already hold the corpus.

    A persistent store outlives the process, so re-embedding 312 chunks on every ``consilium ask``
    would pay the entire ingestion cost for one query.  An in-memory store starts empty, so it is
    always ingested.  The freshness check is chunk count, which catches the case that matters --
    notes added, removed, or rechunked -- and deliberately does not try to detect an edit that
    leaves the count unchanged: ``consilium ingest`` resets the store and is the supported way to
    reload after editing a note.
    """
    document_count, chunks = build_chunks(corpus_dir)
    lexical = lexical if lexical is not None else Bm25Index()

    if store.count() == len(chunks) and chunks:
        lexical.add(chunks)
    else:
        store.reset()
        index_chunks(chunks, embedder=embedder, store=store, lexical=lexical)

    counts: Counter[Category] = Counter(chunk.category for chunk in chunks)
    report = IngestReport(
        corpus_dir=corpus_dir,
        documents=document_count,
        chunks=len(chunks),
        chunks_by_category=dict(counts),
        embedder=embedder.name,
        store=store.name,
    )
    return HybridRetriever(embedder=embedder, store=store, lexical=lexical), report


def ingest(
    *,
    corpus_dir: Path,
    embedder: Embedder,
    store: VectorStore,
    lexical: Bm25Index | None = None,
    reset: bool = True,
) -> tuple[HybridRetriever, IngestReport]:
    """Load, chunk, embed and index the corpus; return the retriever and a report.

    ``reset`` defaults to true because the persistent store outlives the process: re-ingesting a
    corpus whose notes have been edited would otherwise leave the previous chunking of every edited
    note in the index, and stale chunks retrieved alongside fresh ones is a failure that looks like
    a retrieval-quality problem rather than an ingestion one.
    """
    if reset:
        store.reset()

    lexical = lexical if lexical is not None else Bm25Index()
    document_count, chunks = build_chunks(corpus_dir)
    index_chunks(chunks, embedder=embedder, store=store, lexical=lexical)

    counts: Counter[Category] = Counter(chunk.category for chunk in chunks)
    report = IngestReport(
        corpus_dir=corpus_dir,
        documents=document_count,
        chunks=len(chunks),
        chunks_by_category=dict(counts),
        embedder=embedder.name,
        store=store.name,
    )
    return HybridRetriever(embedder=embedder, store=store, lexical=lexical), report
