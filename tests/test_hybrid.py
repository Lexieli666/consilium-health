"""The retriever's contract with the trace, and with the numbers computed from it.

Everything here is a property the evaluation depends on: the depths are fixed, the trace carries
ranks 6-10 the model never sees, deduplication has already run by the time anything is counted, and
the category filter narrows both retrievers rather than the fused result.
"""

from __future__ import annotations

import pytest

from consilium.retrieval import (
    CANDIDATE_DEPTH,
    RETURNED_K,
    TRACE_DEPTH,
    Bm25Index,
    Chunk,
    HashEmbedder,
    HybridRetriever,
    NumpyStore,
)
from consilium.trace import MemorySink, RetrievalEvent, Tracer


def test_the_depths_are_the_frozen_values() -> None:
    """A retrieval depth that varies per call makes recall@5's denominator vary too."""
    assert (CANDIDATE_DEPTH, TRACE_DEPTH, RETURNED_K) == (20, 10, 5)


def test_search_returns_exactly_the_top_five(corpus_retriever: HybridRetriever) -> None:
    hits = corpus_retriever.search("high blood pressure treatment", skill="search_knowledge")

    assert len(hits) == RETURNED_K


def test_search_cannot_be_asked_for_a_different_k(corpus_retriever: HybridRetriever) -> None:
    """`returned_k` is a property of the retriever, deliberately not an argument to `search`."""
    with pytest.raises(TypeError):
        corpus_retriever.search(  # type: ignore[call-arg]
            "high blood pressure", skill="search_knowledge", k=3
        )


def test_results_hold_one_chunk_per_document(corpus_retriever: HybridRetriever) -> None:
    """Deduplication runs before truncation, so no note can take two of the five slots."""
    hits = corpus_retriever.search("hypertension", skill="search_knowledge")

    doc_ids = [hit.chunk.doc_id for hit in hits]
    assert len(doc_ids) == len(set(doc_ids))


def test_the_trace_carries_ten_hits_while_the_model_sees_five(
    corpus_retriever: HybridRetriever, tracer: Tracer, memory_sink: MemorySink
) -> None:
    """Without ranks 6-10, MRR@10 is uncomputable -- and truncating the artifact to what the model
    saw is the standard way retrieval metrics quietly become unmeasurable."""
    hits = corpus_retriever.search("hypertension", skill="search_knowledge", tracer=tracer)

    (event,) = memory_sink.of_type(RetrievalEvent)
    assert len(event.fused_topk) == TRACE_DEPTH
    assert event.returned_k == RETURNED_K
    assert len(hits) == RETURNED_K
    assert [hit.chunk.doc_id for hit in hits] == [hit.doc_id for hit in event.fused_topk[:5]]


def test_the_traced_ranking_is_already_deduplicated(
    corpus_retriever: HybridRetriever, tracer: Tracer, memory_sink: MemorySink
) -> None:
    """recall@5 and MRR@10 are computed from this list, so the dedup has to have happened first."""
    corpus_retriever.search("diabetes", skill="search_knowledge", tracer=tracer)

    (event,) = memory_sink.of_type(RetrievalEvent)
    doc_ids = [hit.doc_id for hit in event.fused_topk]
    assert len(doc_ids) == len(set(doc_ids))


def test_the_trace_records_the_query_the_skill_and_the_filter(
    corpus_retriever: HybridRetriever, tracer: Tracer, memory_sink: MemorySink
) -> None:
    corpus_retriever.search(
        "what code covers type 2 diabetes",
        skill="lookup_disease_code",
        category="coding",
        tracer=tracer,
    )

    (event,) = memory_sink.of_type(RetrievalEvent)
    assert event.skill == "lookup_disease_code"
    assert event.query == "what code covers type 2 diabetes"
    assert event.category_filter == "coding"
    assert event.latency_ms >= 0


def test_no_filter_is_recorded_as_none(
    corpus_retriever: HybridRetriever, tracer: Tracer, memory_sink: MemorySink
) -> None:
    corpus_retriever.search("anything at all", skill="search_knowledge", tracer=tracer)

    (event,) = memory_sink.of_type(RetrievalEvent)
    assert event.category_filter is None


def test_search_works_without_a_tracer(corpus_retriever: HybridRetriever) -> None:
    """Retrieval is substrate; it must not require a turn to be in progress."""
    assert corpus_retriever.search("asthma", skill="search_knowledge")


def test_the_category_filter_confines_the_result(corpus_retriever: HybridRetriever) -> None:
    """`lookup_disease_code` must never compete with lifestyle prose for a slot."""
    hits = corpus_retriever.search(
        "type 2 diabetes", skill="lookup_disease_code", category="coding"
    )

    assert hits
    assert {hit.chunk.category for hit in hits} == {"coding"}


def test_the_filter_applies_before_fusion_not_after(corpus_retriever: HybridRetriever) -> None:
    """Filtering the fused result would spend both retrievers' 20 candidates on discarded notes,
    so a narrow filter would silently draw from a shallower pool than a broad one."""
    filtered = corpus_retriever.fuse("diet and exercise", category="lifestyle")
    unfiltered_then_filtered = [
        item
        for item in corpus_retriever.fuse("diet and exercise")
        if item.chunk.category == "lifestyle"
    ]

    assert len(filtered) > len(unfiltered_then_filtered)


def test_fusion_uses_both_retrievers(corpus_chunks: list[Chunk]) -> None:
    """A document only one retriever finds still reaches the fused ranking."""
    embedder = HashEmbedder()
    store = NumpyStore()
    store.add(corpus_chunks, embedder.embed_documents([chunk.text for chunk in corpus_chunks]))
    retriever = HybridRetriever(embedder=embedder, store=store, lexical=Bm25Index(corpus_chunks))

    dense_only = {
        item.chunk.doc_id for item in store.query(embedder.embed_query("I10"), CANDIDATE_DEPTH)
    }
    lexical_only = {item.chunk.doc_id for item in retriever.lexical.query("I10", CANDIDATE_DEPTH)}
    fused = {item.chunk.doc_id for item in retriever.fuse("I10")}

    assert fused == dense_only | lexical_only


def test_an_empty_index_retrieves_nothing(tracer: Tracer, memory_sink: MemorySink) -> None:
    retriever = HybridRetriever(embedder=HashEmbedder(), store=NumpyStore(), lexical=Bm25Index())

    assert retriever.search("anything", skill="search_knowledge", tracer=tracer) == []
    (event,) = memory_sink.of_type(RetrievalEvent)
    assert event.fused_topk == []
    assert event.returned_k == 0


def test_returned_k_is_clamped_to_what_was_actually_found(
    corpus_chunks: list[Chunk], tracer: Tracer, memory_sink: MemorySink
) -> None:
    """`returned_k` is what the model saw, so on a thin result it must not claim five."""
    embedder = HashEmbedder()
    store = NumpyStore()
    two = corpus_chunks[:2]
    store.add(two, embedder.embed_documents([chunk.text for chunk in two]))
    retriever = HybridRetriever(embedder=embedder, store=store, lexical=Bm25Index(two))

    hits = retriever.search("asthma", skill="search_knowledge", tracer=tracer)

    (event,) = memory_sink.of_type(RetrievalEvent)
    assert event.returned_k == len(hits) <= 1  # both chunks share a doc_id, so dedup leaves one


@pytest.mark.parametrize(
    ("candidate_depth", "trace_depth", "returned_k"),
    [(20, 10, 0), (20, 10, 11), (20, 25, 5), (5, 10, 5)],
)
def test_incoherent_depths_are_rejected(
    candidate_depth: int, trace_depth: int, returned_k: int
) -> None:
    with pytest.raises(ValueError, match="need 0 < returned_k"):
        HybridRetriever(
            embedder=HashEmbedder(),
            store=NumpyStore(),
            lexical=Bm25Index(),
            candidate_depth=candidate_depth,
            trace_depth=trace_depth,
            returned_k=returned_k,
        )
