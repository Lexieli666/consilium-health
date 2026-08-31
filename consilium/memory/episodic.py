"""Substrate: cross-session recall in local SQLite, over the same ``Embedder`` as retrieval.

One structured summary per completed session -- ``question``, ``key_findings``, ``risk_level``,
``sources`` -- with its embedding stored as a float32 blob, retrieved by brute-force cosine over
every row, top 3.

**Why brute force, and where it stops being acceptable.**  SQLite has no vector index, so a query
reads every stored vector.  At 384 dimensions a row's vector is 1,536 bytes, so 10,000 sessions is
about 15 MB read per query and a 10,000x384 matmul -- single-digit milliseconds, which is nothing
next to one LLM round trip.  The honest limit is roughly **10,000 rows**: past that the full-table
scan dominates the query and the answer is an index (``sqlite-vec``, pgvector, or a real vector
store behind the same :class:`EpisodicStore` protocol), not a faster loop.  A portfolio project does
not reach 10,000 sessions, so the index would be complexity bought for a load that does not exist --
but the number is written down here so that "we did not need one" is a measured claim rather than a
shrug.

**The same ``Embedder`` as retrieval**, not a second model.  Two embedding models would mean two
downloads, two dimensions to keep in step, and a second thing to explain; and ``HashEmbedder`` keeps
the whole module runnable offline for free.

**The connection is closed deterministically, and ``with`` on the store is what does it.**
``SqliteEpisodicStore`` holds one ``sqlite3.Connection`` for its lifetime, and a connection that is
only ever collected raises ``ResourceWarning`` at whatever unrelated moment the garbage collector
happens to run.  Under ``filterwarnings = ["error"]`` that surfaces as a failure in a test that has
nothing to do with this module, which is exactly what it did on Python 3.13.  So the store is a
context manager, and ``with`` on **the store** is deliberately not ``with`` on the connection:
``sqlite3.Connection``'s own context manager commits or rolls back a transaction and leaves the
connection open, which is the trap this exists to keep a caller out of.  ``EpisodicStore`` -- the
protocol -- still names only ``close()``, because a hosted implementation should not have to
implement two spellings of one idea.

**Recall is off in every measured run.**  ``docs/EVALUATION.md`` states this and why: the golden
set's items are independent questions, so cross-session recall would let item N answer from item
N-1's stored summary, contaminating faithfulness, recall@5 and the ablation together.  The effect of
episodic memory on answer quality is therefore reported as ``not measured``, which is the truthful
label for a component deliberately disabled while measuring.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Protocol, cast, get_args

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from consilium.log import get_logger
from consilium.retrieval.embedder import EMBEDDING_DIM, Embedder, Vector
from consilium.trace import RiskLevel

log = get_logger(__name__)

#: How many past sessions a recall returns.
RECALL_K = 3

#: The row count past which a full-table scan per query stops being a reasonable answer.  See the
#: module docstring; stated as a constant so the number in the documentation has a single source.
BRUTE_FORCE_ROW_CEILING = 10_000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes (
    session_id   TEXT PRIMARY KEY,
    question     TEXT NOT NULL,
    key_findings TEXT NOT NULL,
    risk_level   TEXT,
    sources      TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    embedding    BLOB NOT NULL
);
"""


class Episode(BaseModel):
    """One completed session, as it is remembered."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str
    question: str
    key_findings: str
    risk_level: RiskLevel | None = None
    sources: tuple[str, ...] = ()
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class ScoredEpisode:
    """A recalled episode and its cosine similarity to the query."""

    episode: Episode
    score: float


class EpisodicStore(Protocol):
    """Where episodes are kept.  A hosted memory service could satisfy this; none is required."""

    def upsert(self, episode: Episode, embedding: Vector) -> None: ...

    def search(self, embedding: Vector, k: int) -> list[ScoredEpisode]: ...

    def count(self) -> int: ...

    def close(self) -> None: ...


class SqliteEpisodicStore:
    """Local SQLite with the embedding as a float32 blob."""

    def __init__(self, path: Path | str, *, dim: int = EMBEDDING_DIM) -> None:
        self.path = Path(path)
        self.dim = dim
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        # `check_same_thread=False` because skills run on worker threads; every method below holds
        # the connection for the length of one statement and SQLite serializes them itself.
        self._connection = sqlite3.connect(str(path), check_same_thread=False)
        self._connection.execute(_SCHEMA)
        self._connection.commit()

    def upsert(self, episode: Episode, embedding: Vector) -> None:
        """Insert or replace the row for this session.

        Upsert rather than insert because there is no session-end signal in a CLI or a stateless
        HTTP API: the row is rewritten at the end of each turn, so the table holds exactly one row
        per session, which is what "one summary per completed session" means in practice.
        """
        vector = _as_float32(embedding, self.dim)
        self._connection.execute(
            "INSERT INTO episodes (session_id, question, key_findings, risk_level, sources, "
            "created_at, embedding) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET question=excluded.question, "
            "key_findings=excluded.key_findings, risk_level=excluded.risk_level, "
            "sources=excluded.sources, created_at=excluded.created_at, "
            "embedding=excluded.embedding",
            (
                episode.session_id,
                episode.question,
                episode.key_findings,
                episode.risk_level,
                "\n".join(episode.sources),
                episode.created_at.isoformat(),
                vector.tobytes(),
            ),
        )
        self._connection.commit()

    def search(self, embedding: Vector, k: int) -> list[ScoredEpisode]:
        rows = self._connection.execute(
            "SELECT session_id, question, key_findings, risk_level, sources, created_at, embedding "
            "FROM episodes"
        ).fetchall()
        if not rows:
            return []
        if len(rows) > BRUTE_FORCE_ROW_CEILING:
            log.warning(
                "episodic.brute_force_scan_large", rows=len(rows), ceiling=BRUTE_FORCE_ROW_CEILING
            )

        matrix = np.vstack([np.frombuffer(row[6], dtype=np.float32) for row in rows])
        scores = _normalize(matrix) @ _unit(_as_float32(embedding, self.dim))
        order = np.argsort(-scores, kind="stable")[:k]
        return [
            ScoredEpisode(episode=_row_to_episode(rows[index]), score=float(scores[index]))
            for index in order
        ]

    def count(self) -> int:
        (total,) = self._connection.execute("SELECT COUNT(*) FROM episodes").fetchone()
        return int(total)

    def clear(self) -> None:
        self._connection.execute("DELETE FROM episodes")
        self._connection.commit()

    def close(self) -> None:
        """Close the connection.  Safe to call twice; ``sqlite3.Connection.close`` is idempotent."""
        self._connection.close()

    def __enter__(self) -> SqliteEpisodicStore:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


class EpisodicMemory:
    """Remembering and recalling sessions, with the embedder attached."""

    def __init__(
        self, store: EpisodicStore, embedder: Embedder, *, recall_enabled: bool = False
    ) -> None:
        self.store = store
        self.embedder = embedder
        #: Off by default.  See the module docstring: recall across a golden set whose items are
        #: independent questions would let item N answer from item N-1.
        self.recall_enabled = recall_enabled

    def remember(
        self,
        *,
        session_id: str,
        question: str,
        key_findings: str,
        risk_level: RiskLevel | None = None,
        sources: Sequence[str] = (),
    ) -> Episode:
        episode = Episode(
            session_id=session_id,
            question=question,
            key_findings=key_findings,
            risk_level=risk_level,
            sources=tuple(sources),
        )
        self.store.upsert(episode, self.embedder.embed_query(f"{question}\n{key_findings}"))
        return episode

    def recall(self, question: str, *, k: int = RECALL_K) -> list[ScoredEpisode]:
        """Past sessions similar to ``question``.  Empty when recall is disabled."""
        if not self.recall_enabled:
            return []
        return self.store.search(self.embedder.embed_query(question), k)

    def close(self) -> None:
        self.store.close()

    def __enter__(self) -> EpisodicMemory:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def _row_to_episode(row: tuple[object, ...]) -> Episode:
    sources = str(row[4])
    return Episode(
        session_id=str(row[0]),
        question=str(row[1]),
        key_findings=str(row[2]),
        risk_level=_risk_level(row[3]),
        sources=tuple(sources.split("\n")) if sources else (),
        created_at=datetime.fromisoformat(str(row[5])),
    )


def _risk_level(value: object) -> RiskLevel | None:
    """Narrow a stored string back onto the literal, refusing anything the schema does not allow."""
    if value is None:
        return None
    text = str(value)
    if text not in get_args(RiskLevel):
        raise ValueError(f"stored risk level {text!r} is not one of {get_args(RiskLevel)}")
    return cast(RiskLevel, text)


def _as_float32(embedding: Vector, dim: int) -> np.ndarray:
    vector = np.asarray(embedding, dtype=np.float32).reshape(-1)
    if vector.shape[0] != dim:
        raise ValueError(f"expected a {dim}-dimensional embedding; got {vector.shape[0]}")
    return vector


def _unit(vector: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vector))
    return vector if norm == 0.0 else vector / norm


def _normalize(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    normalized: np.ndarray = matrix / norms
    return normalized
