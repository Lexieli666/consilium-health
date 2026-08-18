"""BgeEmbedder is the embedder every measured retrieval number comes from.

Every test here is marked `network`: constructing one downloads BAAI/bge-small-en-v1.5.  They are
excluded by `addopts` and never run in CI, which is the honest arrangement -- the offline suite
exercises the `Embedder` protocol through `HashEmbedder`, and `HashEmbedder` measures token overlap
rather than meaning, so it can establish that the pipeline is wired correctly and nothing about
retrieval quality.

The one unmarked test below injects a stub object into the constructor.  That is *not* the offline
seam and it asserts nothing about bge; it exercises this project's own dimension guard, which exists
because a store written by a 768-dimensional model and queried by a 384-dimensional one fails in a
way that looks like bad retrieval rather than like a bug.
"""

from __future__ import annotations

import numpy as np
import pytest

from consilium.retrieval import EMBEDDING_DIM, BgeEmbedder
from consilium.retrieval.embedder import BGE_MODEL, QUERY_PROMPT_NAME


class _WrongDimensionModel:
    """Not a fake sentence-transformer: the only method called before the guard trips."""

    def get_sentence_embedding_dimension(self) -> int:
        return 768


def test_a_model_of_the_wrong_dimension_is_rejected() -> None:
    with pytest.raises(ValueError, match="384"):
        BgeEmbedder(model=_WrongDimensionModel())


def test_the_configured_model_is_the_one_the_brief_names() -> None:
    assert BGE_MODEL == "BAAI/bge-small-en-v1.5"
    assert EMBEDDING_DIM == 384


def test_the_query_prompt_comes_from_the_model_not_from_a_literal() -> None:
    """bge's query instruction has changed between model revisions.

    Applying `prompt_name="query"` uses whatever string the model publishes; hard-coding
    "Represent this sentence for searching relevant passages: " silently degrades retrieval the
    first time that string changes, which is the worst failure mode for a measured system because
    it never raises.
    """
    assert QUERY_PROMPT_NAME == "query"


@pytest.mark.network
def test_embeddings_are_unit_norm_and_the_right_shape() -> None:
    embedder = BgeEmbedder()

    matrix = embedder.embed_documents(["hypertension is elevated blood pressure", "asthma"])

    assert matrix.shape == (2, EMBEDDING_DIM)
    assert matrix.dtype == np.float32
    assert np.allclose(np.linalg.norm(matrix, axis=1), 1.0, atol=1e-5)


@pytest.mark.network
def test_a_query_embedding_differs_from_the_same_text_embedded_as_a_document() -> None:
    """The observable consequence of `prompt_name="query"`: if the prompt were not applied, these
    two vectors would be identical, and the test would be asserting nothing."""
    embedder = BgeEmbedder()
    text = "what is the treatment for high blood pressure"

    as_document = embedder.embed_documents([text])[0]
    as_query = embedder.embed_query(text)

    assert not np.allclose(as_document, as_query)


@pytest.mark.network
def test_semantic_similarity_beats_lexical_overlap() -> None:
    """The capability HashEmbedder cannot have, stated as a test rather than as a claim."""
    embedder = BgeEmbedder()

    query = embedder.embed_query("my blood pressure is too high")
    related = embedder.embed_documents(["hypertension is a chronic cardiovascular condition"])[0]
    unrelated = embedder.embed_documents(["the pressure in a high altitude cabin is regulated"])[0]

    assert float(query @ related) > float(query @ unrelated)


@pytest.mark.network
def test_embedding_no_documents_gives_an_empty_matrix() -> None:
    assert BgeEmbedder().embed_documents([]).shape == (0, EMBEDDING_DIM)
