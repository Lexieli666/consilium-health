"""Substrate: splitting a document body into retrievable chunks.

800-1,000 characters with a 100-character overlap, preferring paragraph boundaries.  The three
numbers come from the brief; what needs stating is what "where possible" means, because that is
where a chunker actually makes its decisions.

**Packing greedily to 1,000 characters leaves a runt, and the runt is the defect worth designing
against.**  The obvious algorithm -- accumulate paragraphs until the next one would overflow, then
break -- was written first and measured on this corpus.  It produced final chunks as short as 156
characters.  A 156-character chunk is worse than a wasted slot: BM25 length normalization actively
*favours* short documents, so a runt is over-retrieved relative to what it contains, and once
retrieved it consumes one of the five slots the model sees while carrying a fragment of a sentence.

**So the document is divided evenly instead of filled greedily.**  The chunker takes the fewest
chunks the body needs, ``ceil(len / max_content)``, and then places each break at the point that
divides the *remaining* text evenly among the *remaining* chunks.  Recomputing the ideal position
after every break is what keeps a break that had to move from accumulating into a short tail: an
early break makes the next target correspondingly later, so error is absorbed rather than pushed to
the end.

**Each break is then snapped to the best real boundary inside a window, and the window is the size
band itself.**  Its upper end is the largest chunk that still fits; its lower end is whichever is
larger of the band's floor and the smallest chunk that still leaves the remaining chunks able to
hold the rest.  Inside that window the chunker prefers a paragraph boundary, then a sentence
boundary, then a word boundary, taking whichever candidate of the best available kind is closest to
the ideal position.

The window has to be the band and not merely a feasibility bound, which the first version got wrong.
Feasibility alone leaves the first break free to range from character 31 to character 898, so the
rule "prefer a paragraph boundary" would happily pull it 288 characters off target to reach one, and
it produced a 393-character opening chunk.  Preference between kinds may only operate over positions
that are all acceptable on size; otherwise the preference silently overrides the band.

**The band and the note-length band are coupled, and the coupling is tight.**  A 3,474-character
body divided into chunks of at most 898 characters of new content needs four of them, which is a 97%
fill: there is no room left to reach for a paragraph boundary, so most breaks in this corpus land on
sentence boundaries instead.  That is a real consequence of the two bands the brief fixes, not a
defect of the chunker, and ``tests/test_chunking.py`` measures the boundary mix on the real corpus
rather than asserting a hope.

**A heading is never split and never ends a chunk.**  Every candidate position from the start of a
heading line through the blank line after it is excluded, for all three kinds of boundary at once.
Excluding them only from the paragraph-start set was the first attempt and it left the real defect
in place: word boundaries exist *inside* a heading too, so a break landed mid-heading and produced a
chunk ending ``## What raises immediate``.  A heading fragment reads as relevant to an embedder and
is useless to a reader, and a heading stranded whole at the end of a chunk announces content that
lives in the next one.

**The overlap is a prefix carried forward, snapped to a word boundary.**  The next chunk opens with
the tail of the previous one, so a claim split across a break is recoverable from either side.
Snapping forward to the next word start means the overlap can be a few characters shorter than
requested and never begins mid-word: a truncated leading token is a spurious BM25 term.

The disclaimer is already gone by the time text arrives here -- :mod:`consilium.retrieval.corpus`
strips it -- for the reason given in that module: identical in every note, it is zero-IDF to BM25
and a constant offset on every dense vector.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from typing import Final

from consilium.retrieval.corpus import Document
from consilium.retrieval.types import Chunk

#: The band from the brief.  ``MAX_CHARS`` and ``OVERLAP_CHARS`` are enforced by construction;
#: ``MIN_CHARS`` is the floor the corpus's own 2,700-3,500 character note band is sized to reach,
#: and ``tests/test_chunking.py`` measures how close the real corpus actually comes to it.
MAX_CHARS: Final = 1000
MIN_CHARS: Final = 800
OVERLAP_CHARS: Final = 100

_SEPARATOR: Final = "\n\n"

_PARAGRAPH_SPLIT_RE: Final = re.compile(r"\n\s*\n")
_HEADING_LINE_RE: Final = re.compile(r"(?m)^#{1,6} .*$")
_WHITESPACE_RE: Final = re.compile(r"\s+")
_WORD_START_RE: Final = re.compile(r"\S")

#: A sentence boundary is terminal punctuation, whitespace, then something that can open a sentence.
#: The lookahead is what keeps ``E11.9`` and ``I25.10`` intact: an ICD-10 code has no whitespace
#: after its decimal point, so it never presents as a boundary in the first place, and requiring an
#: opening character after the space stops ``vs. the`` and similar from splitting.
_SENTENCE_END_RE: Final = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"“(\[])")


def chunk_document(document: Document) -> list[Chunk]:
    """Chunk one document, carrying its metadata onto every chunk.

    ``chunk_index`` is the position within the document and is half of the trace's ``FusedHit``, so
    it is assigned here, in one place, rather than by whatever happens to be enumerating chunks.
    """
    return [
        Chunk(
            doc_id=document.doc_id,
            chunk_index=index,
            text=text,
            category=document.category,
            title=document.title,
            source=document.source,
        )
        for index, text in enumerate(chunk_body(document.body))
    ]


def chunk_corpus(documents: Iterable[Document]) -> list[Chunk]:
    """Chunk many documents, preserving the order they arrive in."""
    return [chunk for document in documents for chunk in chunk_document(document)]


def chunk_body(
    body: str,
    *,
    max_chars: int = MAX_CHARS,
    min_chars: int = MIN_CHARS,
    overlap: int = OVERLAP_CHARS,
) -> list[str]:
    """Split ``body`` into overlapping chunks per this module's docstring."""
    if not 0 < min_chars <= max_chars:
        raise ValueError(f"need 0 < min_chars <= max_chars; got {min_chars} and {max_chars}")
    if not 0 <= overlap < min_chars:
        raise ValueError(f"need 0 <= overlap < min_chars; got {overlap} and {min_chars}")

    text = body.strip()
    if not text:
        return []

    # Every chunk after the first opens with the overlap prefix and a separator, so the new content
    # one chunk may carry is smaller than the chunk itself.  Budgeting against the smaller number
    # for every chunk -- including the first, which has no prefix -- keeps the arithmetic in one
    # place at the cost of one slightly-shorter opening chunk.
    max_content = max_chars - overlap - len(_SEPARATOR)
    count = max(1, math.ceil(len(text) / max_content))

    # The lower bound on a chunk is the band's floor, or the even share when the body is too short
    # to give every chunk the floor.  Holding early chunks at an unreachable floor would not rescue
    # the band; it would push the whole shortfall onto the last chunk and recreate the runt.  The
    # even share is never unreachable, so this needs no special case.
    min_content = max(1, min(min_chars - overlap - len(_SEPARATOR), len(text) // count))

    pieces = [
        text[start:end].strip()
        for start, end in _spans(
            text, count=count, max_content=max_content, min_content=min_content
        )
    ]

    chunks: list[str] = []
    for piece in pieces:
        seed = _overlap_prefix(chunks[-1], overlap) if chunks else ""
        chunks.append(f"{seed}{_SEPARATOR}{piece}" if seed else piece)
    return chunks


def _spans(text: str, *, count: int, max_content: int, min_content: int) -> list[tuple[int, int]]:
    """Offsets of ``count`` consecutive pieces of ``text``, broken at the best real boundaries."""
    candidates = _boundaries(text)
    spans: list[tuple[int, int]] = []
    start = 0

    for remaining in range(count, 1, -1):
        highest = min(len(text), start + max_content)
        # Three lower bounds, and the binding one is whichever is largest: at least one character
        # of progress, enough left over for the remaining chunks to hold the rest, and the band's
        # floor.  The floor yields to the other two -- a body that cannot fill every chunk to the
        # floor still has to be chunked -- which is what `min(..., highest)` expresses.
        lowest = max(
            start + 1,
            len(text) - (remaining - 1) * max_content,
            min(start + min_content, highest),
        )
        ideal = start + round((len(text) - start) / remaining)
        cut = _snap(ideal, lowest, highest, candidates)
        spans.append((start, cut))
        start = cut

    spans.append((start, len(text)))
    return spans


#: Break candidates grouped by preference, most preferred first.
type _Boundaries = tuple[tuple[int, ...], ...]


def _boundaries(text: str) -> _Boundaries:
    """Collect break candidates, most preferred kind first, minus the ones a heading forbids."""
    forbidden = _heading_spans(text)

    def allowed(position: int) -> bool:
        return not any(start < position <= end for start, end in forbidden)

    kinds = (
        tuple(match.end() for match in _PARAGRAPH_SPLIT_RE.finditer(text)),
        tuple(match.end() for match in _SENTENCE_END_RE.finditer(text)),
        tuple(match.end() for match in _WHITESPACE_RE.finditer(text)),
    )
    return tuple(tuple(filter(allowed, kind)) for kind in kinds)


def _heading_spans(text: str) -> tuple[tuple[int, int], ...]:
    """Half-open regions no break may fall inside: each heading line and the blank line after it.

    A break at the *start* of a heading is fine -- the next chunk then opens with it -- so the
    region begins one past the heading's first character.  It runs to just past the paragraph break
    that follows, which is what stops a heading being stranded at the end of a chunk.
    """
    return tuple(
        (match.start(), match.end() + len(_SEPARATOR)) for match in _HEADING_LINE_RE.finditer(text)
    )


def _snap(ideal: int, lowest: int, highest: int, candidates: _Boundaries) -> int:
    """The candidate of the most preferred kind inside ``[lowest, highest]``, closest to ``ideal``.

    Falls back to ``highest`` when no boundary of any kind is available, which happens only for text
    with no whitespace in the window at all.
    """
    if lowest > highest:  # pragma: no cover - defensive; count is chosen so this cannot happen
        return highest
    for kind in candidates:
        inside = [position for position in kind if lowest <= position <= highest]
        if inside:
            return min(inside, key=lambda position: (abs(position - ideal), position))
    return highest


def _overlap_prefix(chunk: str, overlap: int) -> str:
    """The tail of ``chunk``, snapped forward to a word start, to open the next chunk."""
    if overlap <= 0 or not chunk:
        return ""
    tail = chunk[-overlap:]
    if len(chunk) > overlap and not chunk[-overlap - 1].isspace():
        match = _WORD_START_RE.search(tail, _first_space(tail))
        tail = tail[match.start() :] if match else ""
    return tail.strip()


def _first_space(text: str) -> int:
    """Index of the first whitespace character, or the end of the string if there is none."""
    for index, character in enumerate(text):
        if character.isspace():
            return index
    return len(text)
