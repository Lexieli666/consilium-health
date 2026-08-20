"""Substrate: hybrid retrieval over the corpus.

The pipeline is: :mod:`corpus` reads notes off disk, :mod:`chunking` splits them, :mod:`embedder`
and :mod:`store` hold the dense half, :mod:`bm25` the lexical half, :mod:`fusion` combines the two,
:mod:`hybrid` sequences all of it and emits the trace event, and :mod:`index` wires it together for
``consilium ingest``.

Two protocols each have two real implementations rather than one implementation and a mock.
``Embedder`` is satisfied by ``BgeEmbedder`` and ``HashEmbedder``; ``VectorStore`` by
``ChromaStore`` and ``NumpyStore``.  That is what makes ``pytest -m "not network"`` pass with no
model download and no ``chromadb``, and it is also what puts code behind the answer to "why Chroma
rather than Milvus or pgvector".
"""

from consilium.retrieval.bm25 import Bm25Index
from consilium.retrieval.chunking import (
    MAX_CHARS,
    MIN_CHARS,
    OVERLAP_CHARS,
    chunk_body,
    chunk_corpus,
    chunk_document,
)
from consilium.retrieval.corpus import (
    DIFFERS_HEADING,
    DISCLAIMER,
    FRONT_MATTER_KEYS,
    CorpusError,
    Document,
    load_corpus,
    load_document,
    parse_document,
)
from consilium.retrieval.embedder import (
    EMBEDDING_DIM,
    BgeEmbedder,
    Embedder,
    HashEmbedder,
    Matrix,
    Vector,
)
from consilium.retrieval.fusion import RRF_K, dedupe_by_doc_id, reciprocal_rank_fusion
from consilium.retrieval.hybrid import (
    CANDIDATE_DEPTH,
    RETURNED_K,
    TRACE_DEPTH,
    HybridRetriever,
)
from consilium.retrieval.index import (
    IngestReport,
    ingest,
    make_embedder,
    make_store,
    open_retriever,
)
from consilium.retrieval.store import ChromaStore, NumpyStore, VectorStore
from consilium.retrieval.tokenize import STOPWORDS, tokenize
from consilium.retrieval.types import CATEGORIES, Category, Chunk, ScoredChunk

__all__ = [
    "CANDIDATE_DEPTH",
    "CATEGORIES",
    "DIFFERS_HEADING",
    "DISCLAIMER",
    "EMBEDDING_DIM",
    "FRONT_MATTER_KEYS",
    "MAX_CHARS",
    "MIN_CHARS",
    "OVERLAP_CHARS",
    "RETURNED_K",
    "RRF_K",
    "STOPWORDS",
    "TRACE_DEPTH",
    "BgeEmbedder",
    "Bm25Index",
    "Category",
    "ChromaStore",
    "Chunk",
    "CorpusError",
    "Document",
    "Embedder",
    "HashEmbedder",
    "HybridRetriever",
    "IngestReport",
    "Matrix",
    "NumpyStore",
    "ScoredChunk",
    "Vector",
    "VectorStore",
    "chunk_body",
    "chunk_corpus",
    "chunk_document",
    "dedupe_by_doc_id",
    "ingest",
    "load_corpus",
    "load_document",
    "make_embedder",
    "make_store",
    "open_retriever",
    "parse_document",
    "reciprocal_rank_fusion",
    "tokenize",
]
