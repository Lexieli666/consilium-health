"""Substrate: Reciprocal Rank Fusion, and the per-``doc_id`` deduplication that follows it.

RRF scores a chunk by ``sum(1 / (k + rank))`` over the rankings that returned it, with ``rank``
1-based and ``k = 60``.  Both halves of that sentence are load-bearing:

**Ranks, not scores.**  BM25 scores are unbounded sums of IDF terms and cosine similarities live in
``[-1, 1]``; they are not on a common scale, and no normalization makes them one.  Min-max scaling
the two into ``[0, 1]`` per query -- the obvious alternative -- makes the top hit of each retriever
score exactly 1.0 whether it was an excellent match or the least bad of a weak field, which is
precisely the case where the two retrievers should disagree and be allowed to.  Fusing ranks throws
away magnitude on purpose: the only claim being combined is "this retriever put it here".

**``k = 60`` is the published constant and is left alone.**  It flattens the difference between the
top few ranks, so a chunk ranked first by one retriever and fifth by the other beats a chunk ranked
first by one and absent from the other.  Tuning it against the golden set would be fitting the
metric that is used to report the result, so it stays at the value the literature uses and
docs/DESIGN.md says so.

**Deduplication to the first chunk per ``doc_id`` runs after fusion, and before anything is
counted.**  A note with two strong chunks would otherwise take two of the five slots the model sees
and two of the positions recall@5 is computed over, so the same document would be both the
retrieval result and its own competition.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence

from consilium.retrieval.types import Chunk, ScoredChunk

#: The constant from the original RRF paper.  Not tuned; see the module docstring.
RRF_K = 60

#: The label ``ScoredChunk.retriever`` carries once a chunk has been through fusion, so a caller
#: reading a trace or a debug dump can tell a fused score from a raw BM25 or cosine one.
FUSED = "rrf"


def reciprocal_rank_fusion(
    rankings: Iterable[Sequence[ScoredChunk]], *, k: int = RRF_K
) -> list[ScoredChunk]:
    """Fuse ranked lists into one ranking by reciprocal rank.

    Ties are broken by ``chunk_id`` so that the fused order is identical on every run; two chunks
    appearing at the same rank in the same single ranking is common, not exotic.
    """
    if k < 1:
        raise ValueError(f"k must be >= 1; got {k}")

    scores: defaultdict[str, float] = defaultdict(float)
    chunks: dict[str, Chunk] = {}

    for ranking in rankings:
        for rank, scored in enumerate(ranking, start=1):
            chunk_id = scored.chunk.chunk_id
            scores[chunk_id] += 1.0 / (k + rank)
            chunks.setdefault(chunk_id, scored.chunk)

    order = sorted(scores, key=lambda chunk_id: (-scores[chunk_id], chunk_id))
    return [
        ScoredChunk(chunk=chunks[chunk_id], score=scores[chunk_id], retriever=FUSED)
        for chunk_id in order
    ]


def dedupe_by_doc_id(scored: Iterable[ScoredChunk]) -> list[ScoredChunk]:
    """Keep the highest-ranked chunk of each document, preserving the input order.

    Order-preserving rather than re-sorting: the caller has already ranked, and "first chunk per
    ``doc_id``" means first *in that ranking*, not lowest ``chunk_index``.
    """
    seen: set[str] = set()
    kept: list[ScoredChunk] = []
    for item in scored:
        if item.chunk.doc_id in seen:
            continue
        seen.add(item.chunk.doc_id)
        kept.append(item)
    return kept
