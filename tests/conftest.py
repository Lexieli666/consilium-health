"""Shared fixtures.

No fixture here touches the network, a provider key, or a model download: the whole suite must run
under ``pytest -m "not network"`` on a machine with neither.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from consilium.llm import MockProvider, ScriptedResponse
from consilium.retrieval import Chunk, HashEmbedder, NumpyStore
from consilium.trace import MemorySink, Tracer

FIXED_TIME = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


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
