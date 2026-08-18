"""BM25 is the half of hybrid retrieval that carries the coding category.

The tokenizer tests in `tests/test_tokenize.py` assert that `I10`, `E11.9` and `SGLT-2` survive
tokenization of a hand-written string.  These assert the same three tokens survive *the real
corpus*, all the way through chunking and indexing, and that querying one retrieves the note that
owns it.  A tokenizer test that passes on a fixture while the corpus writes the term differently
would leave the hybrid-retrieval claim resting on nothing.
"""

from __future__ import annotations

import pytest

from consilium.retrieval import Bm25Index, Chunk, tokenize
from consilium.retrieval.bm25 import K1, B

CODE_TOKENS = ("i10", "e11.9", "sglt-2")


def test_the_three_code_tokens_survive_the_real_corpus(corpus_chunks: list[Chunk]) -> None:
    """The precondition for everything else in this file."""
    vocabulary = {token for chunk in corpus_chunks for token in tokenize(chunk.text)}

    missing = [token for token in CODE_TOKENS if token not in vocabulary]
    assert missing == [], f"the corpus never produces {missing} as tokens"


@pytest.mark.parametrize(
    ("query", "expected_doc_id"),
    [
        ("I10", "coding-hypertension-i10"),
        ("E11.9", "coding-type-2-diabetes-e11"),
        ("SGLT-2", "guideline-type-2-diabetes-first-line-therapy"),
    ],
)
def test_querying_a_code_token_retrieves_the_note_that_owns_it(
    corpus_chunks: list[Chunk], query: str, expected_doc_id: str
) -> None:
    """The claim itself: a bare rare token is a lexical query, and lexical retrieval answers it."""
    index = Bm25Index(corpus_chunks)

    hits = index.query(query, k=5)

    assert expected_doc_id in {hit.chunk.doc_id for hit in hits}


def test_a_code_token_is_not_split_into_its_parts(corpus_chunks: list[Chunk]) -> None:
    """`E11.9` must not tokenize to `e`, `11`, `9`, which would make the query match every note
    containing a number."""
    index = Bm25Index(corpus_chunks)

    hits = index.query("E11.9", k=10)

    assert hits, "E11.9 retrieved nothing at all"
    assert all(hit.chunk.category == "coding" for hit in hits)


def test_category_filter_narrows_the_result(corpus_chunks: list[Chunk]) -> None:
    index = Bm25Index(corpus_chunks)

    unfiltered = index.query("hypertension blood pressure", k=10)
    filtered = index.query("hypertension blood pressure", k=10, category="lifestyle")

    assert {hit.chunk.category for hit in unfiltered} != {"lifestyle"}
    assert {hit.chunk.category for hit in filtered} == {"lifestyle"}


def test_scores_do_not_depend_on_the_category_filter(corpus_chunks: list[Chunk]) -> None:
    """IDF is a corpus-level statistic, so filtering must not change what a chunk scores.

    A per-category index would make `I10` common inside `coding` and rare outside it, and the
    recall@5 of a filtered skill would stop being comparable with that of an unfiltered one.
    """
    index = Bm25Index(corpus_chunks)
    query = "essential hypertension code"

    unfiltered = {hit.chunk.chunk_id: hit.score for hit in index.query(query, k=len(corpus_chunks))}
    filtered = index.query(query, k=len(corpus_chunks), category="coding")

    assert filtered
    for hit in filtered:
        assert hit.score == pytest.approx(unfiltered[hit.chunk.chunk_id])


def test_chunks_sharing_no_term_with_the_query_are_not_returned(
    corpus_chunks: list[Chunk],
) -> None:
    """A non-matching chunk is a non-hit, and giving it a rank would let RRF award it credit."""
    index = Bm25Index(corpus_chunks)

    hits = index.query("I10", k=len(corpus_chunks))

    assert 0 < len(hits) < len(corpus_chunks)
    assert all("i10" in tokenize(hit.chunk.text) for hit in hits)


def test_a_match_is_returned_even_where_okapi_scores_it_negatively() -> None:
    """A term in every document gets a negative IDF, and `score > 0` would drop a real match.

    Two documents that both contain the query term is the smallest case that reproduces it; a
    narrow category filter over a small corpus is the realistic one.
    """
    chunks = [
        Chunk(
            doc_id=doc_id,
            chunk_index=0,
            text="Hypertension is persistently elevated arterial blood pressure.",
            category="condition",
            title=doc_id,
            source="general clinical reference",
        )
        for doc_id in ("aaa", "bbb")
    ]

    hits = Bm25Index(chunks).query("hypertension", k=2)

    assert [hit.chunk.doc_id for hit in hits] == ["aaa", "bbb"]


def test_ranking_is_deterministic(corpus_chunks: list[Chunk]) -> None:
    index = Bm25Index(corpus_chunks)

    first = [hit.chunk.chunk_id for hit in index.query("asthma inhaler", k=10)]
    second = [hit.chunk.chunk_id for hit in index.query("asthma inhaler", k=10)]

    assert first == second


def test_ties_break_by_chunk_id(sample_chunks: list[Chunk]) -> None:
    """Two chunks with identical text score identically; the order must still be fixed."""
    duplicated = [
        sample_chunks[0].model_copy(update={"doc_id": "bbb"}),
        sample_chunks[0].model_copy(update={"doc_id": "aaa"}),
    ]
    index = Bm25Index(duplicated)

    hits = index.query("hypertension", k=2)

    assert [hit.chunk.doc_id for hit in hits] == ["aaa", "bbb"]


def test_an_empty_index_returns_nothing() -> None:
    assert Bm25Index().query("anything", k=5) == []
    assert Bm25Index().count() == 0


def test_a_query_of_only_stopwords_returns_nothing(corpus_chunks: list[Chunk]) -> None:
    """Tokenizing "what is the" yields nothing, and scoring an empty query is meaningless."""
    assert Bm25Index(corpus_chunks).query("what is the", k=5) == []


def test_adding_nothing_is_a_no_op(corpus_chunks: list[Chunk]) -> None:
    index = Bm25Index(corpus_chunks[:10])
    index.add([])

    assert index.count() == 10


def test_adding_extends_the_index(corpus_chunks: list[Chunk]) -> None:
    index = Bm25Index(corpus_chunks[:10])
    index.add(corpus_chunks[10:20])

    assert index.count() == 20


def test_reset_empties_the_index(corpus_chunks: list[Chunk]) -> None:
    index = Bm25Index(corpus_chunks)
    index.reset()

    assert index.count() == 0
    assert index.query("hypertension", k=5) == []


def test_invalid_k_is_rejected(corpus_chunks: list[Chunk]) -> None:
    with pytest.raises(ValueError, match="k must be"):
        Bm25Index(corpus_chunks).query("hypertension", k=0)


def test_a_chunk_that_tokenizes_to_nothing_does_not_break_the_index() -> None:
    """rank-bm25 divides by the average document length, so an all-empty corpus would raise."""
    empty = Chunk(
        doc_id="condition-empty",
        chunk_index=0,
        text="the of and to",
        category="condition",
        title="Empty",
        source="general clinical reference",
    )
    index = Bm25Index([empty])

    assert index.query("hypertension", k=5) == []


def test_the_ranking_parameters_are_the_library_defaults() -> None:
    """Neither was tuned: tuning them against the golden set would be fitting the metric."""
    assert (K1, B) == (1.5, 0.75)
