"""Substrate: the vector-store seam.

``ChromaStore`` (persistent, the default for real runs) and ``NumpyStore`` (in-memory, brute-force
cosine, used by every non-network test) implement one protocol.  The second implementation is not
test scaffolding bolted on afterwards: it is what lets "why Chroma rather than Milvus or pgvector"
be answered with code behind it, because swapping the store is a constructor argument.

``ChromaStore`` arrives in Phase 2 with the ingestion pipeline.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from typing import Protocol

import numpy as np

from consilium.retrieval.embedder import Matrix, Vector
from consilium.retrieval.types import Category, Chunk, ScoredChunk


class VectorStore(Protocol):
    """Dense nearest-neighbour lookup over chunks, with metadata filtering."""

    name: str

    def add(self, chunks: Sequence[Chunk], embeddings: Matrix) -> None: ...

    def query(
        self, embedding: Vector, k: int, *, category: Category | None = None
    ) -> list[ScoredChunk]: ...

    def count(self) -> int: ...

    def reset(self) -> None: ...


class NumpyStore:
    """Brute-force cosine similarity over an in-memory matrix.

    Exact, dependency-light and fast enough for a corpus of this size: 80 notes chunked at ~900
    characters is a few hundred rows, where an approximate index would add a recall parameter to
    tune and a second source of retrieval error to disentangle from the one being measured.
    """

    def __init__(self) -> None:
        self.name = "numpy"
        self._chunks: list[Chunk] = []
        self._matrix: Matrix = np.zeros((0, 0), dtype=np.float32)
        self._lock = threading.Lock()

    def add(self, chunks: Sequence[Chunk], embeddings: Matrix) -> None:
        if len(chunks) != embeddings.shape[0]:
            raise ValueError(
                f"got {len(chunks)} chunks and {embeddings.shape[0]} embeddings; they must match"
            )
        if not chunks:
            return
        vectors = _normalize(np.asarray(embeddings, dtype=np.float32))
        with self._lock:
            if self._matrix.size == 0:
                self._matrix = vectors
            elif self._matrix.shape[1] != vectors.shape[1]:
                raise ValueError(
                    f"embedding dimension {vectors.shape[1]} does not match the "
                    f"{self._matrix.shape[1]} already in the store"
                )
            else:
                self._matrix = np.vstack([self._matrix, vectors])
            self._chunks.extend(chunks)

    def query(
        self, embedding: Vector, k: int, *, category: Category | None = None
    ) -> list[ScoredChunk]:
        if k < 1:
            raise ValueError(f"k must be >= 1; got {k}")
        with self._lock:
            if not self._chunks:
                return []
            matrix = self._matrix
            chunks = list(self._chunks)

        query_vector = np.asarray(embedding, dtype=np.float32).reshape(-1)
        norm = float(np.linalg.norm(query_vector))
        if norm > 0.0:
            query_vector = query_vector / norm

        scores = matrix @ query_vector
        candidates = [
            (index, float(scores[index]))
            for index, chunk in enumerate(chunks)
            if category is None or chunk.category == category
        ]
        # Descending score, then chunk_id ascending, so ties are resolved identically on every run.
        candidates.sort(key=lambda item: (-item[1], chunks[item[0]].chunk_id))
        return [
            ScoredChunk(chunk=chunks[index], score=score, retriever=self.name)
            for index, score in candidates[:k]
        ]

    def count(self) -> int:
        with self._lock:
            return len(self._chunks)

    def reset(self) -> None:
        with self._lock:
            self._chunks = []
            self._matrix = np.zeros((0, 0), dtype=np.float32)


def _normalize(matrix: Matrix) -> Matrix:
    """L2-normalize rows so a dot product is a cosine.  Zero rows are left as zeros."""
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    safe = np.where(norms == 0.0, 1.0, norms)
    normalized: Matrix = (matrix / safe).astype(np.float32)
    return normalized
