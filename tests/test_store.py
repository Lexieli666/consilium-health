"""NumpyStore is the offline half of the VectorStore seam.  Ordering and metadata filtering are
tested against hand-constructed vectors so the expected ranking is arithmetic, not a model output.
"""

from __future__ import annotations

import numpy as np
import pytest

from consilium.retrieval import Chunk, HashEmbedder, NumpyStore


def make_chunk(doc_id: str, category: str = "condition", index: int = 0) -> Chunk:
    return Chunk(
        doc_id=doc_id,
        chunk_index=index,
        text=f"text for {doc_id}",
        category=category,
        title=doc_id,
        source="general clinical reference",
    )


def test_ranking_is_by_cosine_similarity() -> None:
    store = NumpyStore()
    chunks = [make_chunk("near"), make_chunk("middle"), make_chunk("far")]
    embeddings = np.array(
        [
            [1.0, 0.0, 0.0],  # cosine 1.00 with the query
            [1.0, 1.0, 0.0],  # cosine 0.71
            [0.0, 1.0, 0.0],  # cosine 0.00
        ],
        dtype=np.float32,
    )
    store.add(chunks, embeddings)

    results = store.query(np.array([1.0, 0.0, 0.0], dtype=np.float32), k=3)

    assert [result.chunk.doc_id for result in results] == ["near", "middle", "far"]
    assert results[0].score == pytest.approx(1.0)
    assert results[1].score == pytest.approx(0.7071, abs=1e-4)
    assert results[2].score == pytest.approx(0.0, abs=1e-6)
    assert results[0].retriever == "numpy"


def test_ties_break_deterministically_by_chunk_id() -> None:
    store = NumpyStore()
    chunks = [make_chunk("zeta"), make_chunk("alpha")]
    store.add(chunks, np.array([[1.0, 0.0], [1.0, 0.0]], dtype=np.float32))

    results = store.query(np.array([1.0, 0.0], dtype=np.float32), k=2)

    assert [result.chunk.doc_id for result in results] == ["alpha", "zeta"]


def test_category_filter_excludes_other_categories(populated_store: NumpyStore) -> None:
    query = HashEmbedder().embed_query("blood pressure")

    coding_only = populated_store.query(query, k=5, category="coding")

    assert [result.chunk.doc_id for result in coding_only] == ["icd10-circulatory"]


def test_k_larger_than_the_corpus_returns_everything(populated_store: NumpyStore) -> None:
    results = populated_store.query(HashEmbedder().embed_query("anything"), k=99)

    assert len(results) == populated_store.count() == 4


def test_query_on_an_empty_store_returns_nothing() -> None:
    assert NumpyStore().query(np.array([1.0], dtype=np.float32), k=5) == []


def test_reset_empties_the_store(populated_store: NumpyStore) -> None:
    populated_store.reset()

    assert populated_store.count() == 0


def test_adding_can_be_repeated(populated_store: NumpyStore, embedder: HashEmbedder) -> None:
    populated_store.add([make_chunk("extra")], embedder.embed_documents(["extra"]))

    assert populated_store.count() == 5


def test_mismatched_chunk_and_embedding_counts_are_rejected(embedder: HashEmbedder) -> None:
    with pytest.raises(ValueError, match="must match"):
        NumpyStore().add([make_chunk("a"), make_chunk("b")], embedder.embed_documents(["a"]))


def test_mismatched_dimension_is_rejected() -> None:
    store = NumpyStore()
    store.add([make_chunk("a")], np.array([[1.0, 0.0]], dtype=np.float32))

    with pytest.raises(ValueError, match="dimension"):
        store.add([make_chunk("b")], np.array([[1.0, 0.0, 0.0]], dtype=np.float32))


def test_invalid_k_is_rejected(populated_store: NumpyStore) -> None:
    with pytest.raises(ValueError, match="k must be"):
        populated_store.query(HashEmbedder().embed_query("x"), k=0)


def test_chunk_id_is_stable() -> None:
    assert make_chunk("hypertension-overview", index=2).chunk_id == "hypertension-overview#2"
