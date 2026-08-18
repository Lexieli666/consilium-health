"""RRF is tested against a hand-computed ranking, not against itself.

The point of fusing ranks rather than scores is that the arithmetic is simple enough to check by
hand.  If these expected values had been produced by running the function, the test would assert
only that the function is deterministic.
"""

from __future__ import annotations

import pytest

from consilium.retrieval import Chunk, ScoredChunk, dedupe_by_doc_id, reciprocal_rank_fusion
from consilium.retrieval.fusion import FUSED, RRF_K


def scored(doc_id: str, chunk_index: int = 0, score: float = 1.0) -> ScoredChunk:
    return ScoredChunk(
        chunk=Chunk(
            doc_id=doc_id,
            chunk_index=chunk_index,
            text=f"text of {doc_id}#{chunk_index}",
            category="condition",
            title=doc_id,
            source="general clinical reference",
        ),
        score=score,
        retriever="test",
    )


def test_fused_scores_match_the_hand_computed_arithmetic() -> None:
    """Two rankings, three documents, k=60.

    dense   = [a, b, c]
    lexical = [c, a, d]

    a: 1/61 + 1/62 = 0.0163934 + 0.0161290 = 0.0325224   <- ranked 1 and 2
    c: 1/63 + 1/61 = 0.0158730 + 0.0163934 = 0.0322664   <- ranked 3 and 1
    b: 1/62                                = 0.0161290   <- one ranking only
    d: 1/63                                = 0.0158730   <- one ranking only
    """
    dense = [scored("a"), scored("b"), scored("c")]
    lexical = [scored("c"), scored("a"), scored("d")]

    fused = reciprocal_rank_fusion([dense, lexical])

    assert [item.chunk.doc_id for item in fused] == ["a", "c", "b", "d"]
    assert fused[0].score == pytest.approx(1 / 61 + 1 / 62)
    assert fused[1].score == pytest.approx(1 / 63 + 1 / 61)
    assert fused[2].score == pytest.approx(1 / 62)
    assert fused[3].score == pytest.approx(1 / 63)


def test_a_document_ranked_well_by_both_beats_one_ranked_first_by_only_one() -> None:
    """This is the property the whole design rests on, and k=60 is what produces it.

    a: 1/62 + 1/62 = 0.0322581   ranked second by both
    b: 1/61        = 0.0163934   ranked first by one, absent from the other
    """
    dense = [scored("b"), scored("a")]
    lexical = [scored("c"), scored("a")]

    fused = reciprocal_rank_fusion([dense, lexical])

    assert fused[0].chunk.doc_id == "a"
    assert fused[0].score == pytest.approx(2 / 62)


def test_magnitude_is_discarded_and_only_rank_survives() -> None:
    """A BM25 score of 40 and a cosine of 0.4 are not on a common scale; RRF never sees either."""
    huge = [scored("a", score=4000.0), scored("b", score=0.001)]
    tiny = [scored("a", score=0.004), scored("b", score=0.001)]

    assert [item.score for item in reciprocal_rank_fusion([huge])] == [
        pytest.approx(1 / 61),
        pytest.approx(1 / 62),
    ]
    assert [item.chunk.doc_id for item in reciprocal_rank_fusion([huge])] == [
        item.chunk.doc_id for item in reciprocal_rank_fusion([tiny])
    ]


def test_ties_break_by_chunk_id() -> None:
    """Equal fused scores are common, not exotic: any two documents seen once at the same rank."""
    fused = reciprocal_rank_fusion([[scored("z")], [scored("a")]])

    assert [item.chunk.doc_id for item in fused] == ["a", "z"]
    assert fused[0].score == pytest.approx(fused[1].score)


def test_fused_results_are_labelled_as_fused() -> None:
    """So that a trace or a debug dump cannot be misread as carrying a raw BM25 or cosine score."""
    fused = reciprocal_rank_fusion([[scored("a")]])

    assert fused[0].retriever == FUSED


def test_fusing_nothing_yields_nothing() -> None:
    assert reciprocal_rank_fusion([]) == []
    assert reciprocal_rank_fusion([[], []]) == []


def test_an_invalid_k_is_rejected() -> None:
    with pytest.raises(ValueError, match="k must be"):
        reciprocal_rank_fusion([[scored("a")]], k=0)


def test_k_is_the_published_constant() -> None:
    """Not tuned against the golden set, which would be fitting the metric being reported."""
    assert RRF_K == 60


def test_dedupe_keeps_the_highest_ranked_chunk_of_each_document() -> None:
    ranked = [scored("a", 3), scored("b", 0), scored("a", 1), scored("a", 0), scored("b", 2)]

    kept = dedupe_by_doc_id(ranked)

    assert [(item.chunk.doc_id, item.chunk.chunk_index) for item in kept] == [("a", 3), ("b", 0)]


def test_dedupe_preserves_ranking_order_rather_than_chunk_index_order() -> None:
    """ "First chunk per doc_id" means first *in this ranking*, not lowest chunk_index."""
    ranked = [scored("a", 7), scored("a", 0)]

    assert [item.chunk.chunk_index for item in dedupe_by_doc_id(ranked)] == [7]


def test_dedupe_runs_before_truncation_so_a_strong_note_cannot_take_two_slots() -> None:
    """Truncating first would leave the top-5 holding one document twice."""
    ranked = [scored("a", index) for index in range(5)] + [scored("b"), scored("c")]

    kept = dedupe_by_doc_id(ranked)[:5]

    assert [item.chunk.doc_id for item in kept] == ["a", "b", "c"]


def test_dedupe_of_nothing_is_nothing() -> None:
    assert dedupe_by_doc_id([]) == []
