"""Shared fixtures.

No fixture here touches the network, a provider key, or a model download: the whole suite must run
under ``pytest -m "not network"`` on a machine with neither.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from consilium.llm import MockProvider, ScriptedResponse
from consilium.retrieval import (
    Bm25Index,
    Chunk,
    Document,
    HashEmbedder,
    HybridRetriever,
    NumpyStore,
    chunk_corpus,
    load_corpus,
)
from consilium.trace import MemorySink, Tracer

FIXED_TIME = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

#: The real corpus.  Retrieval is tested against it rather than against a handful of invented
#: notes: the properties that matter -- that `I10` survives tokenization, that per-`doc_id` dedup
#: has something to deduplicate, that a category filter narrows a real pool -- are properties of
#: this corpus, and a fixture corpus could satisfy all three while the real one did not.
CORPUS_DIR = Path(__file__).resolve().parents[1] / "data" / "corpus"


@pytest.fixture
def memory_sink() -> MemorySink:
    return MemorySink()


@pytest.fixture
def tracer(memory_sink: MemorySink) -> Iterator[Tracer]:
    with Tracer(
        session_id="test-session",
        turn_index=0,
        sink=memory_sink,
        trace_id="trace-0001",
        clock=lambda: FIXED_TIME,
    ) as instance:
        yield instance


@pytest.fixture
def embedder() -> HashEmbedder:
    return HashEmbedder()


@pytest.fixture
def sample_chunks() -> list[Chunk]:
    return [
        Chunk(
            doc_id="hypertension-overview",
            chunk_index=0,
            text="Hypertension is persistently elevated arterial blood pressure.",
            category="condition",
            title="Hypertension overview",
            source="general clinical reference",
        ),
        Chunk(
            doc_id="hypertension-overview",
            chunk_index=1,
            text="Blood pressure is measured in millimetres of mercury.",
            category="condition",
            title="Hypertension overview",
            source="general clinical reference",
        ),
        Chunk(
            doc_id="icd10-circulatory",
            chunk_index=0,
            text="I10 is the ICD-10 code for essential primary hypertension.",
            category="coding",
            title="ICD-10 circulatory chapter",
            source="general coding reference",
        ),
        Chunk(
            doc_id="diet-sodium",
            chunk_index=0,
            text="Reducing dietary sodium lowers blood pressure for many adults.",
            category="lifestyle",
            title="Dietary sodium",
            source="general clinical reference",
        ),
    ]


@pytest.fixture
def populated_store(sample_chunks: list[Chunk], embedder: HashEmbedder) -> NumpyStore:
    store = NumpyStore()
    store.add(sample_chunks, embedder.embed_documents([chunk.text for chunk in sample_chunks]))
    return store


@pytest.fixture
def mock_provider() -> MockProvider:
    return MockProvider(
        [
            ScriptedResponse(content="A scripted answer about blood pressure."),
            ScriptedResponse(content="A second scripted answer."),
        ]
    )


@pytest.fixture(scope="session")
def corpus_documents() -> list[Document]:
    """Every note in `data/corpus/`, loaded once for the whole session."""
    return load_corpus(CORPUS_DIR)


@pytest.fixture(scope="session")
def corpus_chunks(corpus_documents: list[Document]) -> list[Chunk]:
    return chunk_corpus(corpus_documents)


@pytest.fixture(scope="session")
def corpus_retriever(corpus_chunks: list[Chunk]) -> HybridRetriever:
    """The real pipeline over the real corpus, on the offline seams.

    `HashEmbedder` measures weighted token overlap rather than meaning, so nothing built on this
    fixture is a retrieval *quality* measurement -- those come from `BgeEmbedder` and are reported
    by the eval harness.  What it does establish is that the pipeline is wired correctly end to
    end: filters narrow, fusion combines, dedup collapses, and the trace records what it should.
    """
    embedder = HashEmbedder()
    store = NumpyStore()
    store.add(corpus_chunks, embedder.embed_documents([chunk.text for chunk in corpus_chunks]))
    return HybridRetriever(embedder=embedder, store=store, lexical=Bm25Index(corpus_chunks))
