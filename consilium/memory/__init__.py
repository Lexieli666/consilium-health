"""Substrate: session memory.

``working``   the per-session conversation buffer and **context compaction** -- window, dedup,
              deterministic recap.  Never "entropy management"; nothing here computes an entropy.
``store``     where a session lives: an in-process dict by default, or any key-value backend
              (Redis included, as an optional backend and not a dependency).
``episodic``  cross-session recall in local SQLite over the same ``Embedder`` as retrieval.

Session state is **never** a process-wide singleton.  A ``WorkingMemory`` belongs to one
``session_id`` and is obtained from a ``MemoryStore`` keyed by it, then injected for the duration of
a turn.  A global buffer would let two concurrent API users share a conversation history, and would
make the module untestable without monkeypatching a module global.
"""

from consilium.memory.episodic import (
    BRUTE_FORCE_ROW_CEILING,
    RECALL_K,
    Episode,
    EpisodicMemory,
    EpisodicStore,
    ScoredEpisode,
    SqliteEpisodicStore,
)
from consilium.memory.store import (
    KEY_PREFIX,
    DictBackend,
    InMemoryStore,
    KeyValueBackend,
    MemoryStore,
    MemoryStoreError,
    RedisBackend,
    SerializedStore,
    validate_session_id,
)
from consilium.memory.working import (
    RECAP_PREFIX,
    SOURCES_PREFIX,
    WINDOW_EXCHANGES,
    Exchange,
    Observation,
    SessionState,
    WorkingMemory,
    digest,
)

__all__ = [
    "BRUTE_FORCE_ROW_CEILING",
    "KEY_PREFIX",
    "RECALL_K",
    "RECAP_PREFIX",
    "SOURCES_PREFIX",
    "WINDOW_EXCHANGES",
    "DictBackend",
    "Episode",
    "EpisodicMemory",
    "EpisodicStore",
    "Exchange",
    "InMemoryStore",
    "KeyValueBackend",
    "MemoryStore",
    "MemoryStoreError",
    "Observation",
    "RedisBackend",
    "ScoredEpisode",
    "SerializedStore",
    "SessionState",
    "SqliteEpisodicStore",
    "WorkingMemory",
    "digest",
    "validate_session_id",
]
