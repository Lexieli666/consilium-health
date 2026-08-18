"""Chunking is measured against the real corpus, not asserted against a hope.

The band the brief fixes (800-1,000 characters, 100 overlap) and the band CLAUDE.md fixes for note
length (2,700-3,500 characters) interact, and the interaction is what determines how many chunks a
note yields and how often a break can land on a paragraph boundary.  Neither number is guessable
from the specification alone, so these tests state what the corpus actually produces.
"""

from __future__ import annotations

import itertools
from collections import Counter

import pytest

from consilium.retrieval import MAX_CHARS, MIN_CHARS, OVERLAP_CHARS, Chunk, Document, chunk_body
from consilium.retrieval.chunking import chunk_document

# --------------------------------------------------------------------------------------------
# Against the real corpus.
# --------------------------------------------------------------------------------------------


def test_no_chunk_exceeds_the_ceiling(corpus_chunks: list[Chunk]) -> None:
    """The only hard bound.  Enforced by construction, asserted because it is the one that matters
    for a context window."""
    oversize = [chunk.chunk_id for chunk in corpus_chunks if len(chunk.text) > MAX_CHARS]
    assert oversize == []


def test_the_corpus_yields_four_chunks_per_note(corpus_chunks: list[Chunk]) -> None:
    """A measured consequence of the two bands, not an independent target.

    It matters that this is above one: a single-chunk corpus would never exercise the per-`doc_id`
    deduplication in RRF fusion, so that step would be untested by every retrieval test here.
    """
    per_document = Counter(chunk.doc_id for chunk in corpus_chunks)

    assert set(per_document.values()) == {4}
    assert len(corpus_chunks) == 4 * len(per_document)


def test_no_chunk_is_a_runt(corpus_chunks: list[Chunk]) -> None:
    """BM25 length normalization favours short documents, so a runt is over-retrieved for what it
    carries and then occupies one of the five slots the model sees.

    The floor asserted here is deliberately below `MIN_CHARS`: dividing a 2,725-character body into
    four pieces cannot put every piece above 800, and claiming otherwise would be a number the
    corpus does not support.  What is asserted is that no chunk is a fragment.
    """
    shortest = min(len(chunk.text) for chunk in corpus_chunks)

    assert shortest > 400, f"shortest chunk is {shortest} characters"


def test_most_chunks_reach_the_band(corpus_chunks: list[Chunk]) -> None:
    """The honest form of "800-1,000 characters" for this corpus: a clear majority, not all."""
    lengths = [len(chunk.text) for chunk in corpus_chunks]
    in_band = sum(1 for length in lengths if MIN_CHARS <= length <= MAX_CHARS)

    assert in_band / len(lengths) > 0.7
    assert sum(lengths) / len(lengths) > MIN_CHARS


def _overlap_length(previous: str, current: str) -> int:
    """How much of ``current``'s opening is lifted verbatim from the end of ``previous``.

    Measured rather than taken from the module's own helper, which would make the test circular.
    Note that the overlap can itself contain a paragraph break, so splitting `current` on the first
    blank line is *not* a way to recover it -- an earlier version of this test did exactly that and
    reported a false failure.
    """
    return max(
        (size for size in range(1, OVERLAP_CHARS + 1) if current.startswith(previous[-size:])),
        default=0,
    )


def test_consecutive_chunks_overlap(corpus_documents: list[Document]) -> None:
    """Every chunk after the first opens with text lifted verbatim from its predecessor."""
    for document in corpus_documents:
        chunks = chunk_body(document.body)
        for previous, current in itertools.pairwise(chunks):
            size = _overlap_length(previous, current)
            assert size > 0, f"{document.doc_id}: a chunk opened with no overlap"
            assert size <= OVERLAP_CHARS


def test_chunks_cover_the_body_in_order(corpus_documents: list[Document]) -> None:
    """No text is dropped between chunks: remove the overlaps and the body reappears."""
    for document in corpus_documents:
        chunks = chunk_body(document.body)
        rebuilt = chunks[0]
        for previous, current in itertools.pairwise(chunks):
            rebuilt += current[_overlap_length(previous, current) :]
        assert _squeeze(rebuilt) == _squeeze(document.body), document.doc_id


def test_no_chunk_ends_on_or_inside_a_heading(corpus_chunks: list[Chunk]) -> None:
    """Both halves of the heading rule, on the real corpus.

    A stranded heading announces content the reader will not find in that chunk; a *truncated*
    heading is worse, and it is what the first version of this rule allowed -- it excluded heading
    positions only from the paragraph-start candidates, and a word boundary inside the heading line
    was still fair game.
    """
    offenders = [
        chunk.chunk_id
        for chunk in corpus_chunks
        if chunk.text.rsplit("\n\n", 1)[-1].lstrip().startswith("#")
    ]
    assert offenders == []


def test_no_break_lands_mid_word(corpus_documents: list[Document]) -> None:
    """A truncated leading or trailing token is a spurious BM25 term."""
    for document in corpus_documents:
        chunks = chunk_body(document.body)
        for previous, current in itertools.pairwise(chunks):
            joint = previous[-1] + current[_overlap_length(previous, current) :][:1]
            assert not (joint[0].isalnum() and len(joint) > 1 and joint[1].isalnum()), (
                f"{document.doc_id}: a break split the word around {joint!r}"
            )


def test_metadata_is_carried_onto_every_chunk(corpus_documents: list[Document]) -> None:
    document = next(d for d in corpus_documents if d.doc_id == "coding-hypertension-i10")
    chunks = chunk_document(document)

    assert [chunk.chunk_index for chunk in chunks] == list(range(len(chunks)))
    assert {chunk.doc_id for chunk in chunks} == {"coding-hypertension-i10"}
    assert {chunk.category for chunk in chunks} == {"coding"}
    assert {chunk.title for chunk in chunks} == {document.title}
    assert {chunk.source for chunk in chunks} == {document.source}
    assert [chunk.chunk_id for chunk in chunks][:2] == [
        "coding-hypertension-i10#0",
        "coding-hypertension-i10#1",
    ]


def test_the_code_that_lexical_retrieval_depends_on_survives_chunking(
    corpus_chunks: list[Chunk],
) -> None:
    """A chunk boundary inside `E11.9` would break the tokenizer's guarantee downstream."""
    for code in ("I10", "E11.9", "J45", "K21"):
        assert any(code in chunk.text for chunk in corpus_chunks), f"{code} vanished in chunking"


# --------------------------------------------------------------------------------------------
# Against constructed input, where the expected split is arithmetic.
# --------------------------------------------------------------------------------------------


def test_a_short_body_is_one_chunk() -> None:
    assert chunk_body("A single short paragraph.") == ["A single short paragraph."]


def test_an_empty_body_yields_no_chunks() -> None:
    assert chunk_body("   \n\n  ") == []


def test_a_paragraph_boundary_is_taken_when_it_sits_inside_the_band() -> None:
    """Two paragraphs that together exceed one chunk split exactly between them."""
    first = "First. " * 100  # 700 characters
    second = "Second. " * 100  # 800 characters
    chunks = chunk_body(f"{first.strip()}\n\n{second.strip()}", max_chars=1000, min_chars=600)

    assert len(chunks) == 2
    assert chunks[0] == first.strip()
    assert chunks[1].endswith(second.strip())


def test_a_paragraph_longer_than_a_chunk_is_split_at_a_sentence_boundary() -> None:
    paragraph = " ".join(f"Sentence number {index} of the paragraph." for index in range(60))
    chunks = chunk_body(paragraph)

    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= MAX_CHARS
        assert chunk.endswith(".")


def test_a_single_sentence_longer_than_a_chunk_is_split_at_a_word_boundary() -> None:
    body = " ".join(["word"] * 800)  # one 4,000-character "sentence"
    chunks = chunk_body(body)

    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= MAX_CHARS
        assert "wordword" not in chunk


def test_text_with_no_whitespace_still_terminates() -> None:
    """The degenerate case: no boundary of any kind exists, so the ceiling is the only guide."""
    chunks = chunk_body("x" * 5000)

    assert len(chunks) > 1
    assert all(len(chunk) <= MAX_CHARS for chunk in chunks)


@pytest.mark.parametrize(
    ("max_chars", "min_chars", "overlap"),
    [(0, 800, 100), (1000, 0, 100), (800, 1000, 100), (1000, 800, 800), (1000, 800, -1)],
)
def test_incoherent_bands_are_rejected(max_chars: int, min_chars: int, overlap: int) -> None:
    with pytest.raises(ValueError, match="need"):
        chunk_body("some text", max_chars=max_chars, min_chars=min_chars, overlap=overlap)


def test_overlap_can_be_disabled() -> None:
    body = " ".join(f"Sentence {index} here." for index in range(200))
    chunks = chunk_body(body, overlap=0)

    assert len(chunks) > 1
    rebuilt = " ".join(chunks)
    assert _squeeze(rebuilt) == _squeeze(body)


def _squeeze(text: str) -> str:
    """Collapse whitespace so a comparison is about content, not about line breaks."""
    return " ".join(text.split())


def test_defaults_are_the_specified_band() -> None:
    assert (MIN_CHARS, MAX_CHARS, OVERLAP_CHARS) == (800, 1000, 100)
