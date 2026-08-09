"""Substrate: the embedding seam.

Two implementations of one protocol, which is what makes the offline rule real rather than mocked:

``BgeEmbedder``   BAAI/bge-small-en-v1.5 via sentence-transformers.  Installed only with the
                  ``[embeddings]`` extra, used for every measured retrieval number.  Arrives in
                  Phase 2.
``HashEmbedder``  Deterministic seeded hashing into the same 384 dimensions.  No download, no
                  network, identical output on every machine and every run.  Used by every test
                  that is not marked ``network``.

``HashEmbedder`` is a *lexical* embedder wearing a dense interface: it measures weighted token
overlap, not meaning.  It exists so the pipeline can be tested end to end offline, and it must never
be the source of a retrieval quality number.  Saying that plainly is the difference between a
defensible test seam and a quietly misleading benchmark.
"""

from __future__ import annotations

import hashlib
import math
from collections import Counter
from collections.abc import Sequence
from typing import Protocol

import numpy as np
import numpy.typing as npt

from consilium.retrieval.tokenize import tokenize

#: bge-small-en-v1.5 is 384-dimensional.  The offline embedder matches it so that a store written
#: by one is dimensionally interchangeable with the other and a test cannot accidentally pass
#: because the two never met.
EMBEDDING_DIM = 384

Vector = npt.NDArray[np.float32]
Matrix = npt.NDArray[np.float32]


class Embedder(Protocol):
    """Turns text into unit-norm float32 vectors."""

    dim: int
    name: str

    def embed_documents(self, texts: Sequence[str]) -> Matrix: ...

    def embed_query(self, text: str) -> Vector: ...


class HashEmbedder:
    """Deterministic hashing embedder: signed feature hashing over the shared tokenizer.

    Each token is keyed-hashed to one of ``dim`` buckets and a sign, and contributes its
    sublinear term frequency ``1 + log(tf)`` to that bucket.  The vector is then L2-normalized, so
    the dot product used by :class:`~consilium.retrieval.store.NumpyStore` is a cosine.

    ``hashlib.blake2b`` with an explicit key is used rather than Python's built-in ``hash()``,
    which is salted per process: a per-process embedding would make a persisted index unreadable
    by the next run and would make "deterministic" false in exactly the way that is hardest to
    notice.
    """

    def __init__(self, *, dim: int = EMBEDDING_DIM, seed: int = 20260808) -> None:
        if dim < 1:
            raise ValueError(f"dim must be >= 1; got {dim}")
        self.dim = dim
        self.name = f"hash-{dim}"
        self._key = seed.to_bytes(8, "big")

    def _bucket(self, token: str) -> tuple[int, float]:
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8, key=self._key).digest()
        value = int.from_bytes(digest, "big")
        index = value % self.dim
        sign = 1.0 if value >> 63 else -1.0
        return index, sign

    def _embed_one(self, text: str) -> Vector:
        vector = np.zeros(self.dim, dtype=np.float32)
        counts = Counter(tokenize(text))
        for token, count in counts.items():
            index, sign = self._bucket(token)
            vector[index] += sign * (1.0 + math.log(count))
        norm = float(np.linalg.norm(vector))
        if norm > 0.0:
            vector /= norm
        return vector

    def embed_documents(self, texts: Sequence[str]) -> Matrix:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        return np.vstack([self._embed_one(text) for text in texts]).astype(np.float32)

    def embed_query(self, text: str) -> Vector:
        return self._embed_one(text)
