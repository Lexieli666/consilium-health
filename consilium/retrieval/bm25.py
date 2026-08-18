"""Substrate: the lexical half of hybrid retrieval.

BM25 over the same chunks the dense index holds, using the tokenizer in
:mod:`consilium.retrieval.tokenize` -- the same one the offline hash embedder uses.  That shared
tokenizer is the whole point: the hybrid-retrieval claim in docs/DESIGN.md is that lexical retrieval
carries the coding category because ``I10``, ``E11.9`` and ``SGLT-2`` are rare tokens a dense
retriever embeds poorly, and that claim is only testable if those strings survive tokenization as
single tokens.  ``tests/test_bm25.py`` asserts they do, against the real corpus rather than against
a fixture that could drift from it.

Two behaviours worth stating because both are decisions:

**Category filtering happens after scoring, not by building one index per category.**  IDF is a
corpus-level statistic: how rare a term is, is a fact about the corpus, not about the subset a skill
happens to be filtering to.  Rebuilding the index per category would make ``I10`` common inside
``coding`` and rare outside it, so the same chunk would score differently depending on which skill
asked -- and the recall@5 numbers for filtered and unfiltered skills would stop being comparable.

**A chunk that shares no term with the query is not returned at all.**  Including it would hand RRF
a ranked position for a chunk that matched nothing, and fusion cannot tell a weak hit from a non-hit
once it has been given a rank.

The test for that is token overlap, not a positive score, and the difference is not cosmetic.
Okapi BM25 gives a *negative* IDF to a term carried by more than half the corpus, and ``rank-bm25``
floors it at a fraction of the average IDF, which is itself negative in that case.  Filtering on
``score > 0`` therefore silently drops genuine matches whenever a query term is common -- on a small
or narrow corpus, which is exactly the shape a test fixture has.  Asking "did this chunk contain any
query token" says what is meant and does not depend on the sign of a smoothing term.

``rank-bm25`` is pinned at exactly 0.2.2 and has had no release since 2022; docs/DESIGN.md records
why an exact pin rather than a floor.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from rank_bm25 import BM25Okapi

from consilium.retrieval.tokenize import tokenize
from consilium.retrieval.types import Category, Chunk, ScoredChunk

#: ``rank-bm25``'s own defaults, named here rather than left implicit so that a reader can see what
#: the ranking function actually is without opening the dependency.  k1 controls term-frequency
#: saturation and b controls length normalization; neither was tuned, because tuning them against
#: the golden set that is also used to report recall would be fitting the metric.
K1 = 1.5
B = 0.75


class Bm25Index:
    """An in-memory BM25 index over chunks, with metadata filtering.

    Adding chunks rebuilds the underlying model.  ``rank-bm25`` computes document frequencies at
    construction and exposes no incremental update, and ingestion adds the whole corpus in one call,
    so a rebuild costs nothing in the path that actually runs.  Making that explicit is better than
    an ``add`` that silently degrades to quadratic if someone later calls it in a loop.
    """

    def __init__(
        self,
        chunks: Sequence[Chunk] = (),
        *,
        tokenizer: Callable[[str], list[str]] = tokenize,
        k1: float = K1,
        b: float = B,
    ) -> None:
        self.name = "bm25"
        self._tokenizer = tokenizer
        self._k1 = k1
        self._b = b
        self._chunks: list[Chunk] = []
        self._tokens: list[list[str]] = []
        self._token_sets: list[frozenset[str]] = []
        self._model: BM25Okapi | None = None
        if chunks:
            self.add(chunks)

    def add(self, chunks: Sequence[Chunk]) -> None:
        """Add chunks and rebuild the model."""
        if not chunks:
            return
        self._chunks.extend(chunks)
        self._tokens.extend(self._tokenizer(chunk.text) for chunk in chunks)
        self._token_sets = [frozenset(tokens) for tokens in self._tokens]
        self._rebuild()

    def query(self, text: str, k: int, *, category: Category | None = None) -> list[ScoredChunk]:
        """Top-``k`` chunks by BM25 score, optionally restricted to one category."""
        if k < 1:
            raise ValueError(f"k must be >= 1; got {k}")
        if self._model is None:
            return []

        query_tokens = self._tokenizer(text)
        if not query_tokens:
            return []

        wanted = frozenset(query_tokens)
        scores = self._model.get_scores(query_tokens)
        hits = [
            (index, float(score))
            for index, score in enumerate(scores)
            if self._token_sets[index] & wanted
            and (category is None or self._chunks[index].category == category)
        ]
        # Descending score, then chunk_id ascending, so ties resolve identically on every run.
        hits.sort(key=lambda hit: (-hit[1], self._chunks[hit[0]].chunk_id))
        return [
            ScoredChunk(chunk=self._chunks[index], score=score, retriever=self.name)
            for index, score in hits[:k]
        ]

    def count(self) -> int:
        return len(self._chunks)

    def reset(self) -> None:
        self._chunks = []
        self._tokens = []
        self._token_sets = []
        self._model = None

    def _rebuild(self) -> None:
        # A chunk whose text is entirely stopwords tokenizes to nothing.  rank-bm25 divides by the
        # average document length, so an all-empty corpus would raise ZeroDivisionError; a single
        # placeholder token keeps the arithmetic defined without inventing a match, because that
        # token appears in no query the tokenizer can produce.
        corpus = [tokens or ["\x00empty"] for tokens in self._tokens]
        self._model = BM25Okapi(corpus, k1=self._k1, b=self._b)
