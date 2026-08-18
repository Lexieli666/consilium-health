"""Substrate: hybrid retrieval -- dense plus lexical, fused by RRF.

The corpus mixes prose with codes and guideline names, and the two need different retrievers.  A
dense retriever handles "what should I eat if my blood pressure is high" and embeds ``E11.9`` as
noise; BM25 finds ``E11.9`` exactly and cannot connect "high blood pressure" to a note that says
"hypertension" throughout.  Running both and fusing the ranks is the whole design; docs/DESIGN.md
records the rejected alternatives.

**The depths are fixed at construction and cannot be overridden per call, on purpose.**  Recall@5 is
a headline number, and a retrieval depth that varies per call makes its denominator vary too -- the
measurement stops being comparable across skills and across ablation presets, in a way that is
invisible in the results table.  So ``returned_k`` is a property of the retriever, not an argument
to :meth:`HybridRetriever.search`.

**The pipeline order is fixed and each step's position is load-bearing:**

1. Each retriever returns its own top ``candidate_depth`` (20), filtered to the requested category.
2. RRF fuses the two rankings at ``k=60``.
3. Deduplicate to the first chunk per ``doc_id``.  Before truncation, so that a note with two strong
   chunks cannot take two of the ten positions MRR@10 is computed over.
4. Truncate to ``trace_depth`` (10) and record that in the ``retrieval`` event.  The trace carries
   ranks 6-10 even though the model never sees them, because MRR@10 is uncomputable without them.
5. Return the first ``returned_k`` (5) to the caller.

The category filter is applied by each retriever rather than after fusion.  Filtering afterwards
would spend both retrievers' 20 candidates on documents that are then discarded, so a narrow filter
like ``coding`` would silently retrieve from a shallower pool than a broad one.
"""

from __future__ import annotations

from consilium.retrieval.bm25 import Bm25Index
from consilium.retrieval.embedder import Embedder
from consilium.retrieval.fusion import RRF_K, dedupe_by_doc_id, reciprocal_rank_fusion
from consilium.retrieval.store import VectorStore
from consilium.retrieval.types import Category, ScoredChunk
from consilium.trace import FusedHit, Tracer, stopwatch

#: Each retriever contributes this many candidates to the fusion.  Deeper than the ten that reach
#: the trace so that a document ranked poorly by one retriever and well by the other can still be
#: rescued by fusion -- which is the entire reason for running two retrievers.
CANDIDATE_DEPTH = 20

#: Recorded in the trace.  MRR@10 is computed from exactly this.
TRACE_DEPTH = 10

#: Returned to the model.  Fixed for every measured run; see the module docstring.
RETURNED_K = 5


class HybridRetriever:
    """Dense and lexical retrieval over the same chunks, fused by reciprocal rank."""

    def __init__(
        self,
        *,
        embedder: Embedder,
        store: VectorStore,
        lexical: Bm25Index,
        candidate_depth: int = CANDIDATE_DEPTH,
        rrf_k: int = RRF_K,
        trace_depth: int = TRACE_DEPTH,
        returned_k: int = RETURNED_K,
    ) -> None:
        if not 0 < returned_k <= trace_depth <= candidate_depth:
            raise ValueError(
                "need 0 < returned_k <= trace_depth <= candidate_depth; got "
                f"{returned_k}, {trace_depth}, {candidate_depth}"
            )
        self.embedder = embedder
        self.store = store
        self.lexical = lexical
        self.candidate_depth = candidate_depth
        self.rrf_k = rrf_k
        self.trace_depth = trace_depth
        self.returned_k = returned_k

    def search(
        self,
        query: str,
        *,
        skill: str,
        category: Category | None = None,
        tracer: Tracer | None = None,
    ) -> list[ScoredChunk]:
        """Retrieve for ``query``, emitting one ``retrieval`` trace event.

        ``skill`` labels the event.  It is required rather than defaulted because the metric
        breakdown in docs/EVALUATION.md is per skill, and a retrieval attributed to the wrong skill
        is worse than one attributed to none.
        """
        with stopwatch() as elapsed_ms:
            fused = self.fuse(query, category=category)
            top = fused[: self.trace_depth]

        if tracer is not None:
            tracer.retrieval(
                skill=skill,
                query=query,
                category_filter=category,
                fused_topk=[
                    FusedHit(
                        doc_id=scored.chunk.doc_id,
                        chunk_index=scored.chunk.chunk_index,
                        rrf_score=scored.score,
                    )
                    for scored in top
                ],
                returned_k=min(self.returned_k, len(top)),
                latency_ms=elapsed_ms(),
            )

        return top[: self.returned_k]

    def fuse(self, query: str, *, category: Category | None = None) -> list[ScoredChunk]:
        """The ranking itself: both retrievers, fused and deduplicated, untruncated.

        Separated from :meth:`search` so that fusion can be tested against a hand-computed expected
        ranking without a tracer, and so that the truncation points stay visible in one place.
        """
        dense = self.store.query(
            self.embedder.embed_query(query), self.candidate_depth, category=category
        )
        lexical = self.lexical.query(query, self.candidate_depth, category=category)
        return dedupe_by_doc_id(reciprocal_rank_fusion([dense, lexical], k=self.rrf_k))
