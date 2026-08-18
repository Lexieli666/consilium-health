"""Substrate: the vector-store seam.

``ChromaStore`` (persistent, the default for real runs) and ``NumpyStore`` (in-memory, brute-force
cosine, used by every non-network test) implement one protocol.  The second implementation is not
test scaffolding bolted on afterwards: it is what lets "why Chroma rather than Milvus or pgvector"
be answered with code behind it, because swapping the store is a constructor argument.

``ChromaStore`` is imported lazily for the same reason ``BgeEmbedder`` is: ``chromadb`` lives
behind the ``[embeddings]`` extra that CI never installs, and a module-level import would make this
module unimportable in the environment the test suite runs in.  Conformance to the protocol is still
checked in CI without the package present, because :func:`consilium.retrieval.index.make_store` is
annotated as returning a ``VectorStore`` and mypy verifies that statically.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from consilium.retrieval.embedder import Matrix, Vector
from consilium.retrieval.types import CATEGORIES, Category, Chunk, ScoredChunk


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


#: One collection holds the whole corpus; the category filter is a metadata predicate rather than a
#: separate collection per category.  Splitting by category would make a cross-category query
#: (``search_knowledge``, ``deep_research``) a fan-out over five collections whose scores would then
#: need merging -- a second fusion step, on top of the one the design already has.
COLLECTION_NAME = "consilium-corpus"


class ChromaStore:
    """Persistent dense index backed by Chroma.

    Two constructor details are decisions rather than boilerplate:

    ``embedding_function=None``  Chroma otherwise attaches a default ONNX model and embeds text for
    you on ``add``.  This project supplies its own vectors through the ``Embedder`` seam, and an
    implicit second embedder would both download a model and silently make the store's vectors
    disagree with the query vectors.

    ``space="cosine"``  The embedders return L2-normalized vectors, so cosine is the metric the
    scores are meant to be read in.  Chroma reports a *distance*; this class converts it back to a
    similarity so that ``ScoredChunk.score`` means the same thing here as it does in
    :class:`NumpyStore`, which is what lets the two be swapped without changing how a result reads.
    """

    def __init__(
        self,
        *,
        path: Path | str,
        collection_name: str = COLLECTION_NAME,
        client: Any | None = None,
    ) -> None:
        if client is None:
            import chromadb

            Path(path).mkdir(parents=True, exist_ok=True)
            client = chromadb.PersistentClient(path=str(path))

        self.name = "chroma"
        self._client = client
        self._collection_name = collection_name
        self._collection = self._open()

    def _open(self) -> Any:
        try:
            return self._client.get_or_create_collection(
                name=self._collection_name,
                configuration={"hnsw": {"space": "cosine"}},
                embedding_function=None,
            )
        except TypeError:
            # Chroma before the 1.x `configuration` argument.  The pin is `>=1.0,<2`, so this is a
            # fallback for a patch release rather than a supported second API.
            return self._client.get_or_create_collection(
                name=self._collection_name,
                metadata={"hnsw:space": "cosine"},
                embedding_function=None,
            )

    def add(self, chunks: Sequence[Chunk], embeddings: Matrix) -> None:
        if len(chunks) != embeddings.shape[0]:
            raise ValueError(
                f"got {len(chunks)} chunks and {embeddings.shape[0]} embeddings; they must match"
            )
        if not chunks:
            return
        self._collection.upsert(
            ids=[chunk.chunk_id for chunk in chunks],
            embeddings=[vector.tolist() for vector in np.asarray(embeddings, dtype=np.float32)],
            documents=[chunk.text for chunk in chunks],
            metadatas=[
                {
                    "doc_id": chunk.doc_id,
                    "chunk_index": chunk.chunk_index,
                    "category": chunk.category,
                    "title": chunk.title,
                    "source": chunk.source,
                }
                for chunk in chunks
            ],
        )

    def query(
        self, embedding: Vector, k: int, *, category: Category | None = None
    ) -> list[ScoredChunk]:
        if k < 1:
            raise ValueError(f"k must be >= 1; got {k}")
        if self.count() == 0:
            return []

        result = self._collection.query(
            query_embeddings=[np.asarray(embedding, dtype=np.float32).reshape(-1).tolist()],
            n_results=k,
            where=None if category is None else {"category": category},
            include=["documents", "metadatas", "distances"],
        )

        documents = (result.get("documents") or [[]])[0]
        metadatas = (result.get("metadatas") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]

        return [
            ScoredChunk(
                chunk=Chunk(
                    doc_id=str(metadata["doc_id"]),
                    chunk_index=int(metadata["chunk_index"]),
                    text=text,
                    category=_as_category(metadata["category"]),
                    title=str(metadata["title"]),
                    source=str(metadata["source"]),
                ),
                # Chroma reports cosine *distance*; NumpyStore reports cosine similarity.  Convert
                # so that a score means one thing across both implementations of the protocol.
                score=1.0 - float(distance),
                retriever=self.name,
            )
            for text, metadata, distance in zip(documents, metadatas, distances, strict=True)
        ]

    def count(self) -> int:
        count: int = self._collection.count()
        return count

    def reset(self) -> None:
        self._client.delete_collection(name=self._collection_name)
        self._collection = self._open()


def _as_category(value: object) -> Category:
    """Narrow a value read back out of Chroma metadata to the ``Category`` literal.

    Chroma metadata is untyped on the way out, so this is a real check rather than a formality: a
    store written by an older commit whose category vocabulary has since changed would otherwise
    produce a ``Chunk`` that violates its own model.
    """
    if value not in CATEGORIES:
        raise ValueError(f"stored chunk has category {value!r}, which is not one of {CATEGORIES}")
    return value
