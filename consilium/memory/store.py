"""Substrate: where a session's working memory lives.

Two implementations of one protocol, for the same reason the retrieval layer has two of each:
so that "session state is not a process-wide singleton" is a property of the design rather than a
sentence in a README.

``InMemoryStore``        a dict of live :class:`WorkingMemory` objects.  The default.
``SerializedStore``      the same interface over any string key-value backend, so a session can
                         outlive a process and be shared between workers.

Redis is supported as a **backend for the second one**, not as a requirement: ``RedisBackend`` is
fifteen lines and imports ``redis`` inside its constructor, so the package is not a dependency of
this project and nothing here fails to import without it.  Everything above the backend -- the
serialization, the keying, the round trip -- is exercised offline through ``DictBackend``, which is
a real implementation of the same protocol rather than a mock of Redis.

**Keys are namespaced and validated.**  ``session_id`` reaches this module from an HTTP request
body, and it is also used as a directory name by the tracer, so both places validate it against the
same pattern rather than trusting the caller.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Iterator
from typing import Any, Protocol

from consilium.memory.working import WINDOW_EXCHANGES, WorkingMemory

#: Prefix for every key this module writes into a shared backend, so a Redis instance can be shared
#: with something else without a collision.
KEY_PREFIX = "consilium:session:"

#: The same shape ``Tracer`` enforces, for the same reason: a session id becomes a filesystem path
#: there and a cache key here, and one validation is easier to keep honest than two that differ.
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class MemoryStoreError(RuntimeError):
    """Raised when a session cannot be read or written."""


def validate_session_id(session_id: str) -> str:
    if not SESSION_ID_PATTERN.match(session_id):
        raise MemoryStoreError(
            f"session_id must match {SESSION_ID_PATTERN.pattern}; got {session_id!r}"
        )
    return session_id


class MemoryStore(Protocol):
    """Working memory, keyed by session."""

    def get(self, session_id: str) -> WorkingMemory: ...

    def save(self, memory: WorkingMemory) -> None: ...

    def drop(self, session_id: str) -> None: ...

    def sessions(self) -> list[str]: ...


class InMemoryStore:
    """A dict of live sessions.  The default backend.

    Lock-guarded because the API serves concurrent requests: two coroutines on two sessions must not
    race on the dict, and two on the *same* session must get the same object rather than two buffers
    that each remember half the conversation.
    """

    def __init__(self, *, window: int = WINDOW_EXCHANGES) -> None:
        self.window = window
        self._sessions: dict[str, WorkingMemory] = {}
        self._lock = threading.Lock()

    def get(self, session_id: str) -> WorkingMemory:
        validate_session_id(session_id)
        with self._lock:
            memory = self._sessions.get(session_id)
            if memory is None:
                memory = WorkingMemory(session_id, window=self.window)
                self._sessions[session_id] = memory
            return memory

    def save(self, memory: WorkingMemory) -> None:
        """A no-op for live objects; present so the protocol is the same for both backends."""
        with self._lock:
            self._sessions[memory.session_id] = memory

    def drop(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def sessions(self) -> list[str]:
        with self._lock:
            return sorted(self._sessions)


class KeyValueBackend(Protocol):
    """The smallest thing a serialized store needs."""

    def get(self, key: str) -> str | None: ...

    def set(self, key: str, value: str) -> None: ...

    def delete(self, key: str) -> None: ...

    def keys(self, prefix: str) -> list[str]: ...


class DictBackend:
    """An in-process key-value backend.  Real, not a stand-in for Redis."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> str | None:
        with self._lock:
            return self.data.get(key)

    def set(self, key: str, value: str) -> None:
        with self._lock:
            self.data[key] = value

    def delete(self, key: str) -> None:
        with self._lock:
            self.data.pop(key, None)

    def keys(self, prefix: str) -> list[str]:
        with self._lock:
            return sorted(key for key in self.data if key.startswith(prefix))


class RedisBackend:
    """A key-value backend on Redis.

    ``redis`` is imported in the constructor, not at module scope: this project does not depend on
    Redis and must import cleanly without it.  The brief permits Redis as an optional backend, and
    an optional backend that made the package unimportable would not be optional.
    """

    def __init__(self, url: str, *, client: Any | None = None) -> None:
        if client is None:
            try:
                import redis  # deliberately deferred; see the class docstring
            except ImportError as exc:  # pragma: no cover - depends on the environment
                raise MemoryStoreError(
                    "the redis backend needs the `redis` package, which this project does not "
                    "depend on. Install it yourself, or use the default in-process store."
                ) from exc
            client = redis.Redis.from_url(url, decode_responses=True)
        self._client = client

    def get(self, key: str) -> str | None:
        value = self._client.get(key)
        return None if value is None else str(value)

    def set(self, key: str, value: str) -> None:
        self._client.set(key, value)

    def delete(self, key: str) -> None:
        self._client.delete(key)

    def keys(self, prefix: str) -> list[str]:
        return sorted(str(key) for key in self._client.scan_iter(match=f"{prefix}*"))


class SerializedStore:
    """Working memory over a key-value backend, so a session can outlive the process."""

    def __init__(self, backend: KeyValueBackend, *, window: int = WINDOW_EXCHANGES) -> None:
        self.backend = backend
        self.window = window

    def _key(self, session_id: str) -> str:
        return KEY_PREFIX + validate_session_id(session_id)

    def get(self, session_id: str) -> WorkingMemory:
        payload = self.backend.get(self._key(session_id))
        if payload is None:
            return WorkingMemory(session_id, window=self.window)
        try:
            return WorkingMemory.from_json(payload, window=self.window)
        except ValueError as exc:
            raise MemoryStoreError(f"stored session {session_id!r} is unreadable: {exc}") from exc

    def save(self, memory: WorkingMemory) -> None:
        """Persist the session.

        Required after every mutation, unlike :class:`InMemoryStore` where the object is live.  The
        callers go through ``run_turn``, so there is exactly one place that has to remember.
        """
        self.backend.set(self._key(memory.session_id), memory.to_json())

    def drop(self, session_id: str) -> None:
        self.backend.delete(self._key(session_id))

    def sessions(self) -> list[str]:
        return [key.removeprefix(KEY_PREFIX) for key in self.backend.keys(KEY_PREFIX)]

    def __iter__(self) -> Iterator[str]:
        return iter(self.sessions())
