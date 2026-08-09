"""HashEmbedder is what makes ``pytest -m "not network"`` a real offline suite rather than a mocked
one, so determinism across processes and machines is the property under test.
"""

from __future__ import annotations

import numpy as np
import pytest

from consilium.retrieval import EMBEDDING_DIM, HashEmbedder


def test_dimension_matches_the_real_model(embedder: HashEmbedder) -> None:
    """bge-small-en-v1.5 is 384-dim; the offline embedder matches so the seams are swappable."""
    assert embedder.dim == EMBEDDING_DIM == 384
    assert embedder.embed_query("hypertension").shape == (384,)
    assert embedder.embed_documents(["a", "b"]).shape == (2, 384)


def test_embeddings_are_unit_norm_float32(embedder: HashEmbedder) -> None:
    vector = embedder.embed_query("elevated blood pressure in adults")

    assert vector.dtype == np.float32
    assert float(np.linalg.norm(vector)) == pytest.approx(1.0, abs=1e-6)


def test_identical_text_gives_an_identical_vector_across_instances() -> None:
    """Built-in hash() is salted per process; a per-process embedding would break persistence."""
    first = HashEmbedder().embed_query("ICD-10 code E11.9")
    second = HashEmbedder().embed_query("ICD-10 code E11.9")

    assert np.array_equal(first, second)


def test_a_different_seed_gives_a_different_vector() -> None:
    default = HashEmbedder().embed_query("chest pain")
    reseeded = HashEmbedder(seed=1).embed_query("chest pain")

    assert not np.array_equal(default, reseeded)


def test_token_overlap_beats_unrelated_text(embedder: HashEmbedder) -> None:
    query = embedder.embed_query("dietary sodium and blood pressure")
    related = embedder.embed_query("reducing dietary sodium lowers blood pressure")
    unrelated = embedder.embed_query("ICD-10 chapter structure for coding")

    assert float(query @ related) > float(query @ unrelated)


def test_empty_text_gives_a_zero_vector_not_a_nan(embedder: HashEmbedder) -> None:
    vector = embedder.embed_query("")

    assert not np.isnan(vector).any()
    assert float(np.linalg.norm(vector)) == 0.0


def test_embedding_no_documents_gives_an_empty_matrix(embedder: HashEmbedder) -> None:
    matrix = embedder.embed_documents([])

    assert matrix.shape == (0, EMBEDDING_DIM)


def test_invalid_dimension_is_rejected() -> None:
    with pytest.raises(ValueError, match="dim"):
        HashEmbedder(dim=0)
