"""One set of assertions, run against every implementation of the `VectorStore` protocol.

This is what the second implementation is *for*.  A mock would assert that the code called chromadb
in a particular way; a shared contract asserts that both implementations mean the same thing by
"query", which is the property a swap actually depends on -- and it is what puts code behind the
"why Chroma rather than Milvus or pgvector" answer in docs/DESIGN.md.

`ChromaStore` is skipped where the `[embeddings]` extra is absent, which is CI and is deliberate:
chromadb drags in a dependency tree CI does not install.  The skip is honest about what did not run.
`ChromaStore` still has its conformance to the protocol checked in CI, statically -- `make_store` is
annotated as returning `VectorStore`, so mypy verifies the structural match without the package
being importable.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import numpy as np
import pytest

from consilium.retrieval import (
    Category,
    Chunk,
    HashEmbedder,
    NumpyStore,
    VectorStore,
    make_store,
)

StoreFactory = Callable[[], VectorStore]


@pytest.fixture(params=["numpy", "chroma"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[VectorStore]:
    if request.param == "chroma":
        pytest.importorskip("chromadb", reason="the [embeddings] extra is not installed")
        built = make_store("chroma", path=tmp_path / "chroma")
    else:
        built = make_store("numpy")
    yield built
    built.reset()


def make_chunk(
    doc_id: str, *, category: Category = "condition", index: int = 0, text: str = ""
) -> Chunk:
    return Chunk(
        doc_id=doc_id,
        chunk_index=index,
        text=text or f"text for {doc_id} chunk {index}",
        category=category,
        title=doc_id,
        source="general clinical reference",
    )


def test_an_empty_store_counts_zero_and_retrieves_nothing(store: VectorStore) -> None:
    assert store.count() == 0
    assert store.query(np.zeros(384, dtype=np.float32), k=5) == []


def test_added_chunks_are_counted(store: VectorStore) -> None:
    embedder = HashEmbedder()
    chunks = [make_chunk("condition-a"), make_chunk("condition-b")]

    store.add(chunks, embedder.embed_documents([chunk.text for chunk in chunks]))

    assert store.count() == 2


def test_the_nearest_vector_ranks_first(store: VectorStore) -> None:
    """The one assertion that has to mean the same thing in both implementations: a higher score is
    a closer match.  Chroma reports a distance and `ChromaStore` converts it, which is precisely the
    kind of inversion a shared contract catches and a mock never would."""
    embedder = HashEmbedder()
    chunks = [
        make_chunk("condition-hypertension", text="hypertension elevated blood pressure arterial"),
        make_chunk("condition-asthma", text="asthma wheeze inhaler bronchoconstriction airway"),
    ]
    store.add(chunks, embedder.embed_documents([chunk.text for chunk in chunks]))

    results = store.query(embedder.embed_query("hypertension blood pressure"), k=2)

    assert results[0].chunk.doc_id == "condition-hypertension"
    assert results[0].score > results[1].score


def test_metadata_survives_a_round_trip(store: VectorStore) -> None:
    """Every field the trace and the answer's source list need has to come back out."""
    embedder = HashEmbedder()
    chunk = make_chunk("coding-hypertension-i10", category="coding", index=3)
    store.add([chunk], embedder.embed_documents([chunk.text]))

    (result,) = store.query(embedder.embed_query(chunk.text), k=1)

    assert result.chunk.doc_id == "coding-hypertension-i10"
    assert result.chunk.chunk_index == 3
    assert result.chunk.category == "coding"
    assert result.chunk.title == "coding-hypertension-i10"
    assert result.chunk.source == "general clinical reference"
    assert result.chunk.text == chunk.text
    assert result.chunk.chunk_id == "coding-hypertension-i10#3"


def test_the_category_filter_excludes_other_categories(store: VectorStore) -> None:
    embedder = HashEmbedder()
    chunks = [
        make_chunk("condition-hypertension", category="condition"),
        make_chunk("lifestyle-hypertension-diet", category="lifestyle"),
        make_chunk("coding-hypertension-i10", category="coding"),
    ]
    store.add(chunks, embedder.embed_documents([chunk.text for chunk in chunks]))

    results = store.query(embedder.embed_query("hypertension"), k=5, category="coding")

    assert {result.chunk.category for result in results} == {"coding"}


def test_k_larger_than_the_store_returns_everything(store: VectorStore) -> None:
    embedder = HashEmbedder()
    chunks = [make_chunk("condition-a"), make_chunk("condition-b")]
    store.add(chunks, embedder.embed_documents([chunk.text for chunk in chunks]))

    assert len(store.query(embedder.embed_query("text"), k=50)) == 2


def test_adding_can_be_repeated(store: VectorStore) -> None:
    embedder = HashEmbedder()
    for doc_id in ("condition-a", "condition-b", "condition-c"):
        chunk = make_chunk(doc_id)
        store.add([chunk], embedder.embed_documents([chunk.text]))

    assert store.count() == 3


def test_adding_nothing_is_a_no_op(store: VectorStore) -> None:
    store.add([], np.zeros((0, 384), dtype=np.float32))

    assert store.count() == 0


def test_mismatched_chunk_and_embedding_counts_are_rejected(store: VectorStore) -> None:
    with pytest.raises(ValueError, match="they must match"):
        store.add([make_chunk("condition-a")], np.zeros((2, 384), dtype=np.float32))


def test_invalid_k_is_rejected(store: VectorStore) -> None:
    with pytest.raises(ValueError, match="k must be"):
        store.query(np.zeros(384, dtype=np.float32), k=0)


def test_reset_empties_the_store(store: VectorStore) -> None:
    """Ingestion resets by default: stale chunks from a previous chunking of an edited note look
    like a retrieval-quality problem rather than an ingestion one."""
    embedder = HashEmbedder()
    chunk = make_chunk("condition-a")
    store.add([chunk], embedder.embed_documents([chunk.text]))

    store.reset()

    assert store.count() == 0
    assert store.query(embedder.embed_query("text"), k=5) == []


def test_the_offline_store_is_the_one_the_suite_actually_runs() -> None:
    """States plainly what the skip above means: NumpyStore is what CI exercises."""
    assert isinstance(make_store("numpy"), NumpyStore)


@pytest.mark.parametrize("name", ["milvus", ""])
def test_an_unknown_store_name_is_rejected(name: str) -> None:
    with pytest.raises(ValueError, match="unknown store"):
        make_store(name)  # type: ignore[arg-type]  # the point is the runtime guard


def test_chroma_needs_a_path() -> None:
    with pytest.raises(ValueError, match="needs a path"):
        make_store("chroma")
