"""Substrate: hybrid retrieval over the corpus.

Phase 1 lands the two protocol seams the offline test rule depends on -- ``Embedder`` with
``HashEmbedder``, and ``VectorStore`` with ``NumpyStore`` -- plus the tokenizer that BM25 and the
hash embedder share.  Chunking, ``BgeEmbedder``, ``ChromaStore``, BM25 and RRF fusion arrive in
Phase 2.
"""

from consilium.retrieval.embedder import EMBEDDING_DIM, Embedder, HashEmbedder, Matrix, Vector
from consilium.retrieval.store import NumpyStore, VectorStore
from consilium.retrieval.tokenize import STOPWORDS, tokenize
from consilium.retrieval.types import CATEGORIES, Category, Chunk, ScoredChunk

__all__ = [
    "CATEGORIES",
    "EMBEDDING_DIM",
    "STOPWORDS",
    "Category",
    "Chunk",
    "Embedder",
    "HashEmbedder",
    "Matrix",
    "NumpyStore",
    "ScoredChunk",
    "Vector",
    "VectorStore",
    "tokenize",
]
