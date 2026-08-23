"""Shared fixtures.

No fixture here touches the network, a provider key, or a model download: the whole suite must run
under ``pytest -m "not network"`` on a machine with neither.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from consilium.config import Settings, get_preset
from consilium.llm import MockProvider, ScriptedResponse
from consilium.llm.base import LLMProvider
from consilium.memory import InMemoryStore
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
from consilium.runtime import Runtime, build_runtime
from consilium.safety import Policy, RedFlagTable
from consilium.skills import SkillContext, SkillRegistry, SymptomSystemMap
from consilium.trace import MemorySink, Tracer

FIXED_TIME = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

#: The real corpus.  Retrieval is tested against it rather than against a handful of invented
#: notes: the properties that matter -- that `I10` survives tokenization, that per-`doc_id` dedup
#: has something to deduplicate, that a category filter narrows a real pool -- are properties of
#: this corpus, and a fixture corpus could satisfy all three while the real one did not.
ROOT_DIR = Path(__file__).resolve().parents[1]
CORPUS_DIR = ROOT_DIR / "data" / "corpus"
RED_FLAGS_PATH = ROOT_DIR / "data" / "red_flags.yaml"
SYMPTOM_SYSTEMS_PATH = ROOT_DIR / "data" / "symptom_systems.yaml"
POLICY_PATH = ROOT_DIR / "data" / "policy.yaml"


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


@pytest.fixture(scope="session")
def red_flag_table() -> RedFlagTable:
    return RedFlagTable.from_yaml(RED_FLAGS_PATH)


@pytest.fixture(scope="session")
def symptom_map() -> SymptomSystemMap:
    return SymptomSystemMap.from_yaml(SYMPTOM_SYSTEMS_PATH)


@pytest.fixture(scope="session")
def registry() -> SkillRegistry:
    return SkillRegistry.discover()


@pytest.fixture
def skill_context(
    corpus_retriever: HybridRetriever,
    corpus_documents: list[Document],
    red_flag_table: RedFlagTable,
    symptom_map: SymptomSystemMap,
    tracer: Tracer,
) -> SkillContext:
    """A fully wired context: the real corpus, the real tables, a tracer into a memory sink.

    Fully wired rather than minimal because the failure this catches is a skill that works against
    invented data and returns nothing against the corpus it will actually run on.
    """
    return SkillContext(
        retriever=corpus_retriever,
        red_flags=red_flag_table,
        symptoms=symptom_map,
        documents={document.doc_id: document for document in corpus_documents},
        tracer=tracer,
        agent="consultation",
    )


@pytest.fixture(scope="session")
def policy() -> Policy:
    return Policy.from_yaml(POLICY_PATH)


# ----------------------------------------------------------------------------------------------
# The HTTP API.
#
# Building a `Runtime` loads the corpus and indexes 312 chunks, which is a second of work that the
# API tests have no interest in repeating.  It is built once for the session on the offline seams,
# and each test takes a copy with its own provider and its own empty `MemoryStore` -- a fresh store
# per test, because a shared one would let one test's session be visible to the next, which is the
# exact failure the concurrency test exists to detect.
# ----------------------------------------------------------------------------------------------


@pytest.fixture(scope="session")
def api_settings(tmp_path_factory: pytest.TempPathFactory) -> Settings:
    """Settings pointing at the repository's data and a temporary runs directory."""
    root = tmp_path_factory.mktemp("api")
    return Settings(
        provider="mock",
        root_dir=ROOT_DIR,
        runs_dir=root / "runs",
        data_dir=ROOT_DIR / "data",
        corpus_dir=CORPUS_DIR,
        chroma_dir=root / "chroma",
        episodic_db_path=root / "episodic.db",
    )


@pytest.fixture(scope="session")
def _runtime_template(api_settings: Settings) -> Runtime:
    return build_runtime(
        api_settings,
        config=get_preset("full"),
        provider=MockProvider([]),
        embedder="hash",
        store="numpy",
    )


@pytest.fixture
def offline_runtime(_runtime_template: Runtime) -> Callable[[LLMProvider], Runtime]:
    """A factory: one runtime per test, with the given provider and an empty memory store."""

    def build(provider: LLMProvider) -> Runtime:
        return replace(_runtime_template, provider=provider, memory=InMemoryStore())

    return build
